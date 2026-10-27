"""同步SQL执行测试：绕开当前沙箱阻塞的Windows asyncio socketpair，不冒充HTTP验收。"""
from copy import deepcopy
from datetime import datetime, timezone

import pytest
from test_query_min import request, snapshot

from ops_agent import db
from ops_agent.auth import Principal
from ops_agent.db import Database
from ops_agent.errors import AppError
from ops_agent.query import SQL, Queries, schema
from ops_agent.service import Operations


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setenv('OPS_QUERY_AUDIT_DIR', str(tmp_path / 'audit'))
    now = datetime(2026, 11, 10, 12, tzinfo=timezone.utc)
    database = Database(f'sqlite+pysqlite:///{tmp_path / "sync.db"}', 'test')
    database.initialize()
    p = Principal('test', 'shop_A', ('data.import', 'cost.read', 'metrics.read'), 'csrf')
    operations = Operations(database, lambda: now, 'test', None)
    operations.import_snapshot(p, snapshot())
    yield Queries(database, lambda: now), p, operations, database
    database.close()


def run(runtime, body):
    q, p, _, _ = runtime
    return q.execute(p, body, 'request', 'query')


@pytest.mark.parametrize('metric,value', [('payment_minor', '30000'),
                                         ('paid_order_count', 2), ('paid_units', 4)])
def test_sync_sql_metrics(runtime, metric, value):
    result = run(runtime, request(schema(), metric))
    assert result['rows'] == [{metric: value}]
    assert result['snapshot']['digest'] and not result['truncated']


@pytest.mark.parametrize('sql', ['DELETE FROM ops_order',
    SQL['paid_order_count'] + '; SELECT 1',
    SQL['paid_order_count'] + ' WHERE 1=1',
    SQL['paid_order_count'] + ' JOIN ops_order_item USING(order_id)',
    SQL['paid_order_count'] + ' --comment',
    'SELECT load_extension(:sku_id) AS paid_order_count FROM ops_order'])
def test_sync_rejects(runtime, sql):
    with pytest.raises(AppError, match='SQL或参数'):
        run(runtime, request(schema(), 'paid_order_count', sql=sql))


def test_sync_revision_digest_and_tenant(runtime):
    q, p, operations, _ = runtime
    source = snapshot()
    source.update(revision=2, orders=[])
    operations.import_snapshot(p, source)
    assert run(runtime, request(schema()))['rows'] == [{'payment_minor': '30000'}]
    body = request(schema())
    body['snapshot']['revision'] = 2
    assert run(runtime, body)['rows'] == [{'payment_minor': '0'}]
    body['snapshot']['digest'] = '0'*64
    with pytest.raises(AppError) as error:
        run(runtime, body)
    assert error.value.code == 'snapshot_digest_conflict'
    with pytest.raises(AppError) as error:
        q.execute(Principal('test', 'shop_B', ('metrics.read',), ''), request(schema()), 'r', 'q')
    assert error.value.code == 'snapshot_not_found'


def test_sync_bigint_and_actual_aggregate(runtime, monkeypatch):
    from ops_agent.query import ExactSum
    _, p, operations, _ = runtime
    source = snapshot()
    source['revision'] = 2
    source['orders'] = []
    amount = '9223372036854775807'
    for index in range(2):
        order = deepcopy(snapshot()['orders'][0])
        order.update(order_id=f'large{index}', paid_minor=amount,
                     items=[{'sku_id': 'A', 'qty': 1, 'paid_minor': amount}])
        source['orders'].append(order)
    operations.import_snapshot(p, source)
    calls = []
    original = ExactSum.step
    def step(self, value):
        calls.append(value)
        return original(self, value)
    monkeypatch.setattr(ExactSum, 'step', step)
    body = request(schema())
    body['snapshot']['revision'] = 2
    assert run(runtime, body)['rows'] == [{'payment_minor': '18446744073709551614'}]
    assert calls == [amount, amount]


def test_sync_cutoff_channel_tamper_and_no_write(runtime):
    _, _, _, database = runtime
    with database.transaction() as connection:
        before = {table.name: connection.execute(db.select(table)).all()
                  for table in db.metadata.sorted_tables}
    body = request(schema(), 'paid_order_count')
    body['parameters']['channel'] = 'unknown'
    assert run(runtime, body)['rows'] == [{'paid_order_count': 1}]
    body['parameters']['as_of'] = '2026-10-01T00:00:00+08:00'
    assert run(runtime, body)['rows'] == [{'paid_order_count': 0}]
    with database.transaction() as connection:
        after = {table.name: connection.execute(db.select(table)).all()
                 for table in db.metadata.sorted_tables}
        assert before == after
        connection.execute(db.snapshots.update().values(digest='0'*64))
    with pytest.raises(AppError) as error:
        run(runtime, request(schema()))
    assert error.value.code == 'snapshot_digest_conflict'


def test_sync_limits_and_revocation(runtime, monkeypatch):
    from ops_agent import query
    body = request(schema())
    monkeypatch.setitem(query.LIMITS, 'timeout_ms', 0)
    with pytest.raises(AppError) as error:
        run(runtime, body)
    assert error.value.code == 'query_timeout'
    monkeypatch.setitem(query.LIMITS, 'timeout_ms', 1000)
    monkeypatch.setitem(query.LIMITS, 'max_items', 1)
    with pytest.raises(AppError) as error:
        run(runtime, body)
    assert error.value.code == 'query_resource_limit'
    q, _, _, _ = runtime
    with pytest.raises(AppError) as error:
        q.execute(Principal('test', 'shop_A', (), ''), body, 'r', 'q')
    assert error.value.code == 'forbidden'


def test_sync_oracle_and_trusted_now(runtime):
    from datetime import timedelta

    from ops_agent.domain import calculate_metrics
    q, _, _, _ = runtime
    body = request(schema())
    params = body['parameters']
    oracle = calculate_metrics(snapshot(), datetime.fromisoformat(params['start_at']),
                               datetime.fromisoformat(params['end_at']),
                               datetime.fromisoformat(params['as_of']))
    assert run(runtime, body)['rows'] == [{'payment_minor': oracle['payment_minor']}]
    q.clock = lambda: datetime(2026, 11, 1, tzinfo=timezone.utc) - timedelta(hours=8)
    body = request(schema(), 'paid_order_count')
    result = run(runtime, body)
    assert result['rows'] == [{'paid_order_count': 1}]
    assert datetime.fromisoformat(result['visible_at']) == q.clock()
    # 请求独立连接；前一个SKU筛选不污染后一个订单聚合。
    assert run(runtime, request(schema(), 'paid_units'))['rows'] == [{'paid_units': 3}]
    assert run(runtime, request(schema(), 'paid_order_count'))['rows'] == [{'paid_order_count': 1}]


def test_sync_concurrency_and_safe_audit(runtime):
    import json

    from ops_agent import query
    q, p, _, _ = runtime
    body = request(schema())
    assert query.SLOTS.acquire(False)
    assert query.SLOTS.acquire(False)
    try:
        with pytest.raises(AppError) as error:
            run(runtime, body)
        assert error.value.code == 'query_busy'
    finally:
        query.SLOTS.release()
        query.SLOTS.release()
    result = run(runtime, body)
    q.audit('req', 'ref', body, p, 'success', 1.0, result)
    events = [json.loads(line) for path in q.audit_dir.glob('*.jsonl')
              for line in path.read_text(encoding='utf-8').splitlines()]
    assert events[0]['snapshot_digest'] == result['snapshot']['digest']
    assert not {'sql', 'parameters', 'cookie', 'store_id', 'actor_ref'} & events[0].keys()
