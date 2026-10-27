import csv
import io

import pytest

from ops_agent.csv_import import parse_csv_bundle
from ops_agent.domain import DomainError

SKU = 'sku_id,title,category,base_price_minor,cost_minor,variable_fee_minor,available_qty,price_version'
ORDER = 'order_id,paid_at,status,channel,order_paid_minor,sku_id,qty,item_paid_minor'
REFUND = 'refund_id,order_id,occurred_at,status,amount_minor'


def bundle():
    return {
        'manifest': {'snapshot_id': 'csv-1', 'revision': 1, 'store_id': 'store-1',
                     'source_kind': 'business_import', 'as_of': '2026-10-23T10:00:00+08:00',
                     'currency': 'CNY'},
        'skus_csv': SKU + '\ns1,"中文,标题",食品,100,,,10,1\ns2,=1+1,食品,200,50,0,,1\n',
        'orders_csv': ORDER + '\no1,2026-10-22T10:00:00+08:00,paid,web,300,s1,1,100\n'
                      'o1,2026-10-22T10:00:00+08:00,paid,web,300,s2,1,200\n',
        'refunds_csv': REFUND + '\nr1,o1,2026-10-23T09:00:00+08:00,succeeded,20\n',
    }


def test_multiline_orders_bom_quotes_nullable_and_formula():
    body = bundle()
    body['skus_csv'] = '\ufeff' + body['skus_csv'].replace('\n', '\r\n')
    result = parse_csv_bundle(body)
    assert len(result['orders']) == 1
    assert result['orders'][0]['paid_minor'] == '300'
    assert len(result['orders'][0]['items']) == 2
    assert result['skus'][0]['title'] == '中文,标题'
    assert result['skus'][0]['cost_minor'] is None
    assert result['skus'][0]['variable_fee_minor'] is None
    assert result['skus'][1]['available_qty'] is None
    assert result['skus'][1]['title'] == '=1+1'


@pytest.mark.parametrize('file', ['skus_csv', 'orders_csv', 'refunds_csv'])
def test_identical_duplicates_are_idempotent(file):
    body = bundle()
    body[file] += body[file].splitlines()[1] + '\n'
    assert parse_csv_bundle(body) == parse_csv_bundle(bundle())


@pytest.mark.parametrize('file', ['skus_csv', 'orders_csv', 'refunds_csv'])
def test_conflicting_duplicate_rejected_with_file_line(file):
    body = bundle()
    row = next(csv.reader(io.StringIO(body[file].splitlines()[1])))
    row[-1] = '2' if file == 'skus_csv' else '99'
    output = io.StringIO()
    csv.writer(output).writerow(row)
    body[file] += output.getvalue()
    with pytest.raises(DomainError, match=file + r'.*行'):
        parse_csv_bundle(body)


@pytest.mark.parametrize('value', ['1.0', '1e2', '-1', '+1', ' 1', '9223372036854775808'])
def test_money_strict_and_bounded(value):
    body = bundle()
    body['skus_csv'] = body['skus_csv'].replace(',100,,,', f',{value},,,')
    with pytest.raises(DomainError, match=r'skus_csv.*2'):
        parse_csv_bundle(body)


def test_money_beyond_float_precision_preserved():
    body = bundle()
    body['skus_csv'] = body['skus_csv'].replace(',100,,,', ',9007199254740993,,,')
    assert parse_csv_bundle(body)['skus'][0]['base_price_minor'] == '9007199254740993'


@pytest.mark.parametrize('csv_text', [SKU + ',email\n', SKU.replace('title', 'sku_id') + '\n',
                                   SKU + '\ns1,t,c,100,,,10,1,EXTRA\n',
                                   SKU + '\ns1,t,c,100\n', SKU + '\n"unterminated'])
def test_invalid_header_or_row_reports_location(csv_text):
    body = bundle()
    body['skus_csv'] = csv_text
    with pytest.raises(DomainError, match=r'skus_csv.*行'):
        parse_csv_bundle(body)


@pytest.mark.parametrize('field,value', [('qty', '1.0'), ('qty', '0'), ('price_version', '1e0')])
def test_integer_format_and_minimum(field, value):
    body = bundle()
    file = 'orders_csv' if field == 'qty' else 'skus_csv'
    rows = list(csv.reader(io.StringIO(body[file])))
    rows[1][rows[0].index(field)] = value
    output = io.StringIO()
    csv.writer(output).writerows(rows)
    body[file] = output.getvalue()
    with pytest.raises(DomainError, match=file + r'.*2'):
        parse_csv_bundle(body)


def test_header_conflict_and_missing_source_and_limit():
    body = bundle()
    body['orders_csv'] = body['orders_csv'].replace('web,300,s2', 'store,300,s2')
    with pytest.raises(DomainError, match=r'orders_csv.*3'):
        parse_csv_bundle(body)
    body = bundle()
    del body['manifest']['source_kind']
    with pytest.raises(DomainError, match='manifest'):
        parse_csv_bundle(body)
    body = bundle()
    body['skus_csv'] = SKU + '\n' + 's1,t,c,100,,,10,1\n' * 2001
    with pytest.raises(DomainError, match=r'skus_csv.*2002'):
        parse_csv_bundle(body)


def test_bad_relation_and_aggregate_refund_fail_with_location():
    body = bundle()
    body['orders_csv'] = body['orders_csv'].replace(',s1,', ',missing,')
    with pytest.raises(DomainError, match='orders_csv'):
        parse_csv_bundle(body)
    body = bundle()
    body['refunds_csv'] += 'r2,o1,2026-10-23T09:00:00+08:00,succeeded,290\n'
    with pytest.raises(DomainError, match=r'refunds_csv.*3'):
        parse_csv_bundle(body)


def test_total_raw_rows_limit_before_deduplication():
    body = bundle()
    body['refunds_csv'] += body['refunds_csv'].splitlines()[1] + '\n'
    body['refunds_csv'] = REFUND + '\n' + body['refunds_csv'].splitlines()[1] + '\n'
    body['refunds_csv'] += (body['refunds_csv'].splitlines()[1] + '\n') * 19996
    # 2 SKU + 2订单行 + 19997退款行，去重后仍须因原始行数拒绝。
    with pytest.raises(DomainError, match=r'refunds_csv.*行.*20000'):
        parse_csv_bundle(body)


def test_multiline_quoted_title_and_no_input_mutation():
    body = bundle()
    body['skus_csv'] = body['skus_csv'].replace('中文,标题', '中文,标题\n第二行')
    original = body['skus_csv']
    assert parse_csv_bundle(body)['skus'][0]['title'] == '中文,标题\n第二行'
    assert body['skus_csv'] == original


@pytest.mark.parametrize('change', ['extra_body', 'extra_manifest', 'bad_revision', 'no_source',
                                  'wrong_text', 'too_large'])
def test_bundle_and_manifest_shape(change):
    body = bundle()
    if change == 'extra_body':
        body['email'] = 'private@example.com'
    elif change == 'extra_manifest':
        body['manifest']['phone'] = '123'
    elif change == 'bad_revision':
        body['manifest']['revision'] = '1'
    elif change == 'no_source':
        body['manifest']['source_kind'] = ''
    elif change == 'wrong_text':
        body['orders_csv'] = []
    else:
        body['skus_csv'] = 'a' * (16 * 1024 * 1024 + 1)
    with pytest.raises(DomainError):
        parse_csv_bundle(body)
