"""受控 CSV 文本转 Snapshot：只解析和校验，整批成功后由调用方持久化。"""

import csv
import io
import re
from datetime import datetime

from ops_agent.domain import DomainError, validate_snapshot

MANIFEST_FIELDS = {'snapshot_id', 'revision', 'store_id', 'source_kind', 'as_of', 'currency'}
HEADERS = {
    'skus_csv': ('sku_id', 'title', 'category', 'base_price_minor', 'cost_minor',
                 'variable_fee_minor', 'available_qty', 'price_version'),
    'orders_csv': ('order_id', 'paid_at', 'status', 'channel', 'order_paid_minor',
                   'sku_id', 'qty', 'item_paid_minor'),
    'refunds_csv': ('refund_id', 'order_id', 'occurred_at', 'status', 'amount_minor'),
}
MAX_ROWS = 20_000
MAX_SKU_ROWS = 2_000
MAX_BIGINT = 9_223_372_036_854_775_807
# 行数之外限制原始载荷，避免单个巨大字段占用无界解析内存。
MAX_TEXT_CHARS = 16 * 1024 * 1024


def _fail(file, line, message):
    raise DomainError('invalid_csv_import', f'{file} 第{line}行：{message}')


def _number(value, field, file, line, *, integer=False, nullable=False, minimum=0):
    if nullable and value == '':
        return None
    if not re.fullmatch(r'[0-9]+', value) or len(value) > 19:
        _fail(file, line, f'{field}须为非负整数分或整数字符串，禁止小数、指数和符号')
    number = int(value)
    if not minimum <= number <= MAX_BIGINT:
        _fail(file, line, f'{field}超出允许整数范围')
    return number if integer else value


def _time(value, field, file, line):
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError
        return parsed
    except (ValueError, TypeError, OverflowError):
        _fail(file, line, f'{field}须为带时区的ISO时间')


def _rows(text, file, total):
    if not isinstance(text, str):
        _fail(file, 1, '须提供CSV文本字符串')
    reader = csv.DictReader(io.StringIO(text.removeprefix('\ufeff'), newline=''), strict=True)
    try:
        names = reader.fieldnames
        expected = HEADERS[file]
        if names is None or len(names) != len(expected) or set(names) != set(expected):
            _fail(file, 1, f'列须且仅须为 {",".join(expected)}，禁止重复列及个人信息列')
        count = 0
        for row in reader:
            count += 1
            total[0] += 1
            line = reader.line_num
            if total[0] > MAX_ROWS or (file == 'skus_csv' and count > MAX_SKU_ROWS):
                _fail(file, line, '超出总数据行20000或SKU数据行2000的限制（去重前计数）')
            if None in row or any(value is None for value in row.values()):
                _fail(file, line, '数据列数与表头不符')
            yield line, row
    except csv.Error as error:
        _fail(file, max(reader.line_num, 1), f'CSV语法或字段长度错误：{error}')


def _unique(records, identity, value, file, line):
    if not identity.strip():
        _fail(file, line, '主键不能为空')
    if identity in records:
        if records[identity] != value:
            _fail(file, line, '重复主键内容冲突')
        return False
    records[identity] = value
    return True


def _validate(snapshot, file, line):
    try:
        return validate_snapshot(snapshot)
    except DomainError as error:
        _fail(file, line, error.message)


def parse_csv_bundle(body: dict) -> dict:
    """接收 manifest 对象和三段 CSV 文本，返回已校验的 Snapshot。

    金额单位为分，金额保留字符串；空成本、费用和库存转 None。
    不解释任何单元格公式；不产生数据库、副本文件或部分导入结果。
    """
    required = {'manifest', *HEADERS}
    if not isinstance(body, dict) or body.keys() != required:
        _fail('bundle', 1, '字段须且仅须为manifest、skus_csv、orders_csv、refunds_csv')
    manifest = body['manifest']
    if not isinstance(manifest, dict) or manifest.keys() != MANIFEST_FIELDS:
        _fail('manifest', 1, f'字段须且仅须为 {",".join(sorted(MANIFEST_FIELDS))}')
    if sum(len(body[name]) for name in HEADERS if isinstance(body[name], str)) > MAX_TEXT_CHARS:
        _fail('bundle', 1, 'CSV文本总长度超出16Mi字符限制')
    empty = {**manifest, 'skus': [], 'orders': [], 'refunds': []}
    _validate(empty, 'manifest', 1)
    total = [0]
    skus = {}
    for line, row in _rows(body['skus_csv'], 'skus_csv', total):
        for field in ('base_price_minor', 'cost_minor', 'variable_fee_minor'):
            row[field] = _number(row[field], field, 'skus_csv', line,
                                 nullable=field != 'base_price_minor')
        for field in ('available_qty', 'price_version'):
            row[field] = _number(row[field], field, 'skus_csv', line, integer=True,
                                 nullable=field == 'available_qty',
                                 minimum=1 if field == 'price_version' else 0)
        _validate({**empty, 'skus': [row]}, 'skus_csv', line)
        _unique(skus, row['sku_id'], row, 'skus_csv', line)

    orders, items, order_lines = {}, {}, {}
    for line, row in _rows(body['orders_csv'], 'orders_csv', total):
        file = 'orders_csv'
        header = {name: row[name] for name in ('order_id', 'paid_at', 'status', 'channel')}
        header['paid_at'] = header['paid_at'] or None
        header['paid_minor'] = _number(row['order_paid_minor'], 'order_paid_minor', file, line)
        item = {'sku_id': row['sku_id'],
                'qty': _number(row['qty'], 'qty', file, line, integer=True, minimum=1),
                'paid_minor': _number(row['item_paid_minor'], 'item_paid_minor', file, line)}
        identity = row['order_id']
        if not identity.strip():
            _fail(file, line, 'order_id不能为空')
        if row['sku_id'] not in skus:
            _fail(file, line, '订单行关联不存在的SKU')
        if identity in orders and {k: v for k, v in orders[identity].items() if k != 'items'} != header:
            _fail(file, line, '同订单的支付时间、状态、渠道或订单实收冲突')
        if identity not in orders:
            orders[identity] = {**header, 'items': []}
            items[identity] = {}
        if _unique(items[identity], row['sku_id'], item, file, line):
            orders[identity]['items'].append(item)
        order_lines[identity] = line

    # 先按订单汇总，再校验原生行分摊；订单实收不按CSV行累计。
    for identity, order in orders.items():
        related_skus = [skus[item['sku_id']] for item in order['items']]
        _validate({**empty, 'skus': related_skus, 'orders': [order]},
                  'orders_csv', order_lines[identity])

    refunds, refunded = {}, {}
    for line, row in _rows(body['refunds_csv'], 'refunds_csv', total):
        file = 'refunds_csv'
        row['amount_minor'] = _number(row['amount_minor'], 'amount_minor', file, line)
        occurred = _time(row['occurred_at'], 'occurred_at', file, line)
        if row['order_id'] not in orders:
            _fail(file, line, '退款关联不存在的订单')
        if row['status'] not in ('succeeded', 'pending', 'failed'):
            _fail(file, line, '未知退款状态')
        if not _unique(refunds, row['refund_id'], row, file, line):
            continue
        original = orders[row['order_id']]
        if row['status'] == 'succeeded':
            if original['status'] != 'paid':
                _fail(file, line, '未成功支付订单不能存在成功退款')
            if occurred < _time(original['paid_at'], 'paid_at', file, line):
                _fail(file, line, '退款时间不能早于原支付时间')
            amount = refunded.get(row['order_id'], 0) + int(row['amount_minor'])
            if amount > int(original['paid_minor']):
                _fail(file, line, '成功退款累计超原实付')
            refunded[row['order_id']] = amount

    return _validate({**manifest, 'skus': list(skus.values()), 'orders': list(orders.values()),
                      'refunds': list(refunds.values())}, 'bundle', 1)
