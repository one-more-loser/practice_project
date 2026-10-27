"""JSON归档的最小受控SQL：每请求隔离SQLite，非PostgreSQL方言。"""
import hashlib
import json
import os
import re
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path

from sqlalchemy import select

from ops_agent import db
from ops_agent.domain import validate_snapshot
from ops_agent.errors import AppError
from ops_agent.service import digest, parse_time

SQL = {
    'payment_minor': 'SELECT COALESCE(SUM(paid_minor), 0) AS payment_minor FROM ops_order',
    'paid_order_count': 'SELECT COUNT(*) AS paid_order_count FROM ops_order',
    'paid_units': 'SELECT COALESCE(SUM(qty), 0) AS paid_units FROM ops_order_item WHERE sku_id = :sku_id',
}
LIMITS = {'timeout_ms': 1000, 'max_orders': 20000, 'max_items': 100000,
          'max_snapshot_bytes': 8*1024*1024, 'max_sql_bytes': 4096, 'max_rows': 1,
          'max_result_bytes': 4096, 'concurrency_per_process': 2, 'sqlite_max_bytes': 33554432}
SCHEMA = {
    'profile': 'operations_store_min_sqlite', 'schema_revision': 'OPS-QUERY-SQLITE-MIN-v1',
    'metric_revision': 'OPS-METRIC-PAYMENT-MIN-v1',
    'calculation_revision': 'OPS-CALC-PAYMENT-MIN-v1',
    'validator_revision': 'OPS-SQL-SMALL-GRAMMAR-v1',
    'dialect': 'sqlite_exact_integer_sum_v1',
    'tables': {'ops_order': {'order_id': 'TEXT PRIMARY KEY', 'paid_minor': 'exact integer TEXT'},
               'ops_order_item': {'order_id': 'TEXT', 'line_no': 'INTEGER', 'sku_id': 'TEXT',
                                  'qty': 'exact integer TEXT', 'primary_key': ['order_id', 'line_no']}},
    'scope': 'server-bound store and exact archive; paid cohort prefiltered before SQL',
    'visibility': 'start_at<=paid_at<end_at AND paid_at<=min(as_of,snapshot.as_of,now)',
    'parameters': {'required': ['start_at', 'end_at', 'as_of'], 'optional': ['channel', 'sku_id'],
                   'rules': 'sku_id required only for paid_units; channel matches normalized channel'},
    'grammar': 'only three SELECT forms, case and whitespace variations permitted',
    'sql_examples': SQL, 'limits': LIMITS,
    'unsupported': ['refunds', 'cost', 'join', 'group_by', 'CTE', 'subquery', 'literal filters',
                    'PostgreSQL training baseline equivalence'],
}
SCHEMA['metric_digest'] = digest({'revision': SCHEMA['metric_revision'], 'sql': SQL,
                                 'visibility': SCHEMA['visibility'], 'zero_paid_counted': True})
SCHEMA['schema_digest'] = digest(SCHEMA)
SLOTS = threading.BoundedSemaphore(2)
AUDIT_LOCK = threading.Lock()
# 运维保护不改变SQL语法/指标/摘要；到限失败关闭，不擅自删除历史审计。
MAX_AUDIT_BYTES = 20 * 1024 * 1024
MAX_AUDIT_FILES = 1000


@contextmanager
def audit_file_lock(directory):
    """同目录跨进程互斥；线程锁另由调用方持有，不修改系统设置。"""
    with (directory / '.audit.lock').open('a+b') as stream:
        if stream.tell() == 0:
            stream.write(b'\0')
            stream.flush()
        stream.seek(0)
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def schema():
    return deepcopy(SCHEMA)


def query_ref():
    return 'opsq_' + uuid.uuid4().hex


def reject(message='SQL或参数超出最小受控语法'):
    raise AppError('query_rejected', message, 422)


def shape(value, required, optional=()):
    if not isinstance(value, dict) or not set(required) <= value.keys():
        reject()
    if value.keys() - set(required) - set(optional):
        reject()


def normalize(sql):
    # 不删除注释或标点；多语句/额外token不会被规约成合法语句。
    return re.sub(r'\s+', ' ', sql.strip()).lower()


class ExactSum:
    """SQLite原生SUM可能溢出；精确Python整数累加，返回整数字符串。"""
    def __init__(self):
        self.total = 0

    def step(self, value):
        self.total += int(value)

    def finalize(self):
        return str(self.total)


class Queries:
    def __init__(self, database, clock):
        self.database, self.clock = database, clock
        self.audit_dir = Path(os.environ.get('OPS_QUERY_AUDIT_DIR', str(
            Path(__file__).resolve().parents[3] / 'data' / 'query-audit')))

    def audit(self, request_id, ref, body, principal, outcome, elapsed_ms, result=None):
        event = {key: SCHEMA[key] for key in ('schema_revision', 'schema_digest',
                  'metric_revision', 'metric_digest', 'validator_revision')}
        event.update(request_id=request_id, query_ref=ref, outcome=outcome,
                     elapsed_ms=round(elapsed_ms, 3),
                     sql_digest=hashlib.sha256(str(body.get('sql', '')).encode()).hexdigest())
        if principal is not None:
            event.update(subject_digest=digest([principal.store_id, principal.actor_ref]),
                         auth_version=principal.auth_version,
                         capability_revision=principal.capability_revision)
        if result:
            event.update(snapshot_digest=result['snapshot']['digest'],
                         snapshot_ref_digest=digest(result['snapshot']),
                         context_digest=digest([result['window'], result['visible_at']]),
                         row_count=result['row_count'], truncated=result['truncated'])
        try:
            with AUDIT_LOCK:
                self.audit_dir.mkdir(parents=True, exist_ok=True)
                path = self.audit_dir / (self.clock().strftime('%Y-%m-%d') + '.jsonl')
                payload = (json.dumps(event, ensure_ascii=False) + '\n').encode('utf-8')
                with audit_file_lock(self.audit_dir):
                    total, count = 0, 0
                    for entry in self.audit_dir.glob('*.jsonl'):
                        count += 1
                        if count > MAX_AUDIT_FILES or entry.is_symlink():
                            raise OSError('audit directory capacity or ownership')
                        total += entry.stat().st_size
                    if (total + len(payload) > MAX_AUDIT_BYTES or
                            count >= MAX_AUDIT_FILES and not path.exists()):
                        raise OSError('audit capacity reached')
                    with path.open('ab') as stream:
                        stream.write(payload)
                        stream.flush()
                        os.fsync(stream.fileno())
        except OSError as exc:
            raise AppError('query_audit_unavailable', '查询审计不可用，停止返回结果', 503) from exc

    def execute(self, p, body, request_id, ref):
        p.require('metrics.read')
        shape(body, ('schema_revision', 'schema_digest', 'metric_revision', 'metric_id',
                     'snapshot', 'parameters', 'sql', 'parameter_names'))
        for field in ('schema_revision', 'schema_digest', 'metric_revision'):
            if body[field] != SCHEMA[field]:
                raise AppError('query_version_conflict', 'SQL契约版本或摘要不匹配', 409)
        metric, sql = body['metric_id'], body['sql']
        if not isinstance(metric, str) or metric not in SQL:
            reject()
        if (not isinstance(sql, str) or not sql.isascii()
                or len(sql.encode()) > LIMITS['max_sql_bytes']
                or normalize(sql) != normalize(SQL[metric])):
            reject()
        if body['parameter_names'] != (['sku_id'] if metric == 'paid_units' else []):
            reject()
        source_ref, params = body['snapshot'], body['parameters']
        shape(source_ref, ('snapshot_id', 'revision'), ('digest',))
        if (not isinstance(source_ref['snapshot_id'], str)
                or not 1 <= len(source_ref['snapshot_id']) <= 80
                or type(source_ref['revision']) is not int
                or not 1 <= source_ref['revision'] <= 9223372036854775807):
            reject()
        shape(params, ('start_at', 'end_at', 'as_of'), ('sku_id', 'channel'))
        if (metric == 'paid_units') != ('sku_id' in params):
            reject('仅paid_units须指定SKU，金额和订单数不接受SKU隐式改口径')
        for name in ('sku_id', 'channel'):
            if name in params and (not isinstance(params[name], str)
                                   or not 1 <= len(params[name]) <= 200):
                reject()
        start, end, as_of = (parse_time(params[n]) for n in ('start_at', 'end_at', 'as_of'))
        if start >= end:
            reject('时间窗须非空，采用[start_at,end_at)')
        if not SLOTS.acquire(blocking=False):
            raise AppError('query_busy', '查询并发已满', 429)
        try:
            return self._execute(p, body, start, end, as_of, request_id, ref)
        finally:
            SLOTS.release()

    def _execute(self, p, body, start, end, as_of, request_id, ref):
        began = time.monotonic()
        deadline = began + LIMITS['timeout_ms']/1000

        def budget():
            if time.monotonic() >= deadline:
                raise AppError('query_timeout', '查询超过资源预算', 408)

        source_ref, params, metric = body['snapshot'], body['parameters'], body['metric_id']
        # 候选SQL永不交给业务连接。只以在线身份绑定店铺读取精确归档。
        with self.database.transaction() as connection:
            row = connection.execute(select(db.snapshots).where(
                db.snapshots.c.store_id == p.store_id,
                db.snapshots.c.snapshot_id == source_ref['snapshot_id'],
                db.snapshots.c.revision == source_ref['revision'])).mappings().first()
        if row is None:
            raise AppError('snapshot_not_found', '归档不存在或不可访问', 404)
        budget()
        if len(json.dumps(row['body'], ensure_ascii=False).encode()) > LIMITS['max_snapshot_bytes']:
            raise AppError('query_resource_limit', '归档超过查询预算', 422)
        if digest(row['body']) != row['digest'] or (
                'digest' in source_ref and source_ref['digest'] != row['digest']):
            raise AppError('snapshot_digest_conflict', '归档摘要不匹配', 409)
        source = validate_snapshot(row['body'])
        if (source['store_id'] != p.store_id or source['snapshot_id'] != source_ref['snapshot_id']
                or source['revision'] != source_ref['revision']):
            raise AppError('snapshot_digest_conflict', '归档身份不匹配', 409)
        if (len(source['orders']) > LIMITS['max_orders']
                or sum(len(o['items']) for o in source['orders']) > LIMITS['max_items']):
            raise AppError('query_resource_limit', '投影超过行数预算', 422)
        if 'sku_id' in params and params['sku_id'] not in {s['sku_id'] for s in source['skus']}:
            reject('SKU不在当前快照中')
        visible = min(as_of, parse_time(source['as_of']), self.clock())
        budget()
        connection = sqlite3.connect(':memory:')
        try:
            connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, LIMITS['max_snapshot_bytes'])
            connection.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, LIMITS['max_sql_bytes'])
            connection.setlimit(sqlite3.SQLITE_LIMIT_ATTACHED, 0)
            connection.execute('PRAGMA page_size=4096')
            connection.execute('PRAGMA max_page_count=8192')
            connection.execute('PRAGMA temp_store=MEMORY')
            connection.execute('CREATE TABLE ops_order(order_id TEXT PRIMARY KEY, paid_minor TEXT)')
            connection.execute('CREATE TABLE ops_order_item(order_id TEXT, line_no INTEGER, '
                               'sku_id TEXT, qty TEXT, PRIMARY KEY(order_id,line_no))')
            connection.create_aggregate('sum', 1, ExactSum)
            connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
            for order in source['orders']:
                budget()
                if order['status'] != 'paid':
                    continue
                paid_at = parse_time(order['paid_at'])
                if not (start <= paid_at < end and paid_at <= visible):
                    continue
                if ('channel' in params and
                        (order['channel'].strip() or 'unknown') != params['channel']):
                    continue
                connection.execute('INSERT INTO ops_order VALUES (?,?)',
                                   (order['order_id'], order['paid_minor']))
                for index, item in enumerate(order['items']):
                    budget()
                    connection.execute('INSERT INTO ops_order_item VALUES (?,?,?,?)',
                        (order['order_id'], index, item['sku_id'], str(item['qty'])))
            connection.commit()
            connection.execute('PRAGMA query_only=ON')

            def authorize(action, name, column, database, trigger):
                if action == sqlite3.SQLITE_SELECT:
                    return sqlite3.SQLITE_OK
                if action == sqlite3.SQLITE_READ and name in ('ops_order', 'ops_order_item'):
                    return sqlite3.SQLITE_OK
                if action == sqlite3.SQLITE_FUNCTION and column in ('sum', 'coalesce', 'count'):
                    return sqlite3.SQLITE_OK
                return sqlite3.SQLITE_DENY

            connection.set_authorizer(authorize)
            # 执行经验证的候选原文；不是按metric_id返回固定工具的计算结果。
            values = connection.execute(body['sql'], {'sku_id': params['sku_id']}
                if metric == 'paid_units' else {}).fetchmany(LIMITS['max_rows']+1)
            budget()
            if len(values) != 1:
                raise AppError('query_resource_limit', '结果行数超过预算', 422)
            value = str(values[0][0]) if metric == 'payment_minor' else int(values[0][0])
            if len(json.dumps(value).encode()) > LIMITS['max_result_bytes']:
                raise AppError('query_resource_limit', '结果超过字节预算', 422)
        except sqlite3.Error as exc:
            timeout = time.monotonic() >= deadline
            raise AppError('query_timeout' if timeout else 'query_execution_failed',
                           '隔离SQL执行失败或超过预算', 408 if timeout else 422) from exc
        finally:
            connection.close()
        return {**{k: SCHEMA[k] for k in ('schema_revision', 'schema_digest', 'metric_revision',
                  'metric_digest', 'calculation_revision', 'validator_revision')},
                'snapshot': {**source_ref, 'digest': row['digest']},
                'window': {'start_at': start.isoformat(), 'end_at': end.isoformat()},
                'requested_as_of': as_of.isoformat(), 'visible_at': visible.isoformat(),
                'watermark': source['as_of'], 'source_kind': source['source_kind'],
                'source': 'controlled_sqlite_memory', 'rows': [{metric: value}], 'row_count': 1,
                'truncated': False, 'query_ref': ref, 'request_id': request_id,
                'elapsed_ms': round((time.monotonic()-began)*1000, 3)}
