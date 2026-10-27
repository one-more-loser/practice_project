"""最小SQL真实HTTP/内存SQL验收：不连接现有业务库，tmp_path数据隔离。"""
import json
from datetime import datetime, timezone

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from ops_agent import db
from ops_agent.app import create_app
from ops_agent.domain import calculate_metrics

NOW = datetime(2026, 11, 10, 12, tzinfo=timezone.utc)
ORIGIN = 'http://testserver'
PREFIX = '/api/ops/v1/query'
SQL = {
    'payment_minor': 'SELECT COALESCE(SUM(paid_minor), 0) AS payment_minor FROM ops_order',
    'paid_order_count': 'SELECT COUNT(*) AS paid_order_count FROM ops_order',
    'paid_units': 'SELECT COALESCE(SUM(qty), 0) AS paid_units FROM ops_order_item WHERE sku_id = :sku_id',
}


def snapshot():
    return {'snapshot_id': 'query-fixture', 'revision': 1, 'store_id': 'shop_A',
        'source_kind': 'synthetic', 'as_of': NOW.isoformat(), 'currency': 'CNY',
        'skus': [{'sku_id': key, 'title': '合成商品', 'category': '合成', 'base_price_minor': '10000',
            'cost_minor': None, 'variable_fee_minor': None, 'available_qty': None, 'price_version': 1}
            for key in ('A', 'B')],
        'orders': [
            {'order_id': 'O1', 'status': 'paid', 'paid_at': '2026-11-01T00:00:00+08:00',
             'channel': '', 'paid_minor': '30000', 'items': [
                 {'sku_id': 'A', 'qty': 1, 'paid_minor': '10000'},
                 {'sku_id': 'A', 'qty': 2, 'paid_minor': '10000'},
                 {'sku_id': 'B', 'qty': 4, 'paid_minor': '10000'}]},
            {'order_id': 'O2', 'status': 'paid', 'paid_at': '2026-11-03T10:00:00+08:00',
             'channel': 'web', 'paid_minor': '0', 'items': [{'sku_id': 'A', 'qty': 1, 'paid_minor': '0'}]},
            {'order_id': 'right', 'status': 'paid', 'paid_at': '2026-11-10T00:00:00+08:00',
             'channel': 'web', 'paid_minor': '5000', 'items': [{'sku_id': 'B', 'qty': 1, 'paid_minor': '5000'}]},
        ], 'refunds': []}


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv('OPS_QUERY_AUDIT_DIR', str(tmp_path / 'audit'))
    state = {'store': 'shop_A', 'caps': ['metrics.read', 'cost.read', 'data.import']}
    def identity(request):
        return httpx.Response(200, json={'data': {'subject_id': 'operator', 'store_id': state['store'],
            'capabilities': state['caps'], 'csrf_token': 'csrf', 'auth_version': 1,
            'capability_revision': 1, 'expires_at': '2026-11-11T00:00:00+00:00'}})
    app = create_app(database_url=f'sqlite+pysqlite:///{tmp_path / "business.db"}', mode='test',
        origin=ORIGIN, clock=lambda: NOW, identity_base_url='http://identity',
        identity_transport=httpx.MockTransport(identity))
    with TestClient(app, headers={'Origin': ORIGIN, 'X-CSRF-Token': 'csrf'}) as client:
        client.cookies.set('cs_session', 'opaque')
        response = client.post('/api/ops/v1/snapshots', json=snapshot())
        assert response.status_code == 201
        yield client, state, app, tmp_path


def request(schema, metric='payment_minor', **changes):
    value = {'schema_revision': schema['schema_revision'], 'schema_digest': schema['schema_digest'],
        'metric_revision': schema['metric_revision'], 'metric_id': metric,
        'snapshot': {'snapshot_id': 'query-fixture', 'revision': 1},
        'parameters': {'start_at': '2026-11-01T00:00:00+08:00',
            'end_at': '2026-11-10T00:00:00+08:00', 'as_of': NOW.isoformat()},
        'sql': SQL[metric], 'parameter_names': ['sku_id'] if metric == 'paid_units' else []}
    if metric == 'paid_units':
        value['parameters']['sku_id'] = 'A'
    value.update(changes)
    return value


def schema(client):
    result = client.get(PREFIX + '/schema')
    assert result.status_code == 200, result.text
    return result.json()['data']


def test_sql_three_metrics_match_domain_without_join_fanout(api):
    client, _, _, _ = api
    contract = schema(client)
    oracle = calculate_metrics(snapshot(), datetime.fromisoformat('2026-11-01T00:00:00+08:00'),
        datetime.fromisoformat('2026-11-10T00:00:00+08:00'), NOW)
    for metric, expected in [('payment_minor', '30000'), ('paid_order_count', 2), ('paid_units', 4)]:
        body = request(contract, metric)
        reply = client.post(PREFIX, json=body)
        assert reply.status_code == 200, reply.text
        data = reply.json()['data']
        assert data['rows'] == [{metric: expected}]
        assert data['snapshot']['revision'] == 1
        assert data['row_count'] == 1 and data['source'] == 'controlled_sqlite_memory'
        assert data['query_ref'] and data['request_id']
        if metric != 'paid_units':
            assert data['rows'][0][metric] == oracle[metric]
    # 同单同SKU两行都属于真实原生行，件数1+2+零元单1=4，不按(order,sku)去重。
    assert oracle['paid_order_count'] == 2 and oracle['paid_units'] == 8


def test_exact_revision_empty_cutoff_and_digest(api):
    client, _, _, _ = api
    contract = schema(client)
    value = snapshot()
    value.update(revision=2, orders=[])
    assert client.post('/api/ops/v1/snapshots', json=value).status_code == 201
    body = request(contract)
    old = client.post(PREFIX, json=body)
    assert old.status_code == 200 and old.json()['data']['rows'][0]['payment_minor'] == '30000'
    body['snapshot']['revision'] = 2
    assert client.post(PREFIX, json=body).json()['data']['rows'] == [{'payment_minor': '0'}]
    body = request(contract, 'paid_order_count')
    body['parameters']['as_of'] = '2026-11-01T00:00:00+08:00'
    assert client.post(PREFIX, json=body).json()['data']['rows'] == [{'paid_order_count': 1}]
    body['snapshot']['digest'] = '0' * 64
    assert client.post(PREFIX, json=body).status_code == 409


@pytest.mark.parametrize('sql', [
    'DELETE FROM ops_order', 'SELECT COUNT(*) AS paid_order_count FROM ops_order; DELETE FROM ops_order',
    'SELECT COUNT(*) AS paid_order_count FROM ops_order JOIN ops_order_item USING(order_id)',
    'SELECT COUNT(*) AS paid_order_count FROM ops_order WHERE 1=1',
    'SELECT COUNT(*) AS paid_order_count FROM ops_snapshots',
    'SELECT load_extension(:sku_id) AS paid_order_count FROM ops_order',
    'SELECT COUNT(*) AS paid_order_count FROM ops_order -- comment',
])
def test_rejects_writes_scope_bypass_and_fanout(api, sql):
    client, _, _, _ = api
    contract = schema(client)
    response = client.post(PREFIX, json=request(contract, 'paid_order_count', sql=sql))
    assert response.status_code == 422
    assert response.json()['error']['code'] == 'query_rejected'


def test_tenant_binding_revocation_and_no_business_write(api):
    client, state, app, path = api
    contract = schema(client)
    with app.state.database.transaction() as connection:
        before = {table.name: connection.execute(select(table)).all() for table in db.metadata.sorted_tables}
    assert client.post(PREFIX, json=request(contract)).status_code == 200
    state['store'] = 'shop_B'
    assert client.post(PREFIX, json=request(contract)).status_code == 404
    state['store'] = 'shop_A'
    state['caps'] = []
    assert client.post(PREFIX, json=request(contract)).status_code == 403
    with app.state.database.transaction() as connection:
        after = {table.name: connection.execute(select(table)).all() for table in db.metadata.sorted_tables}
    assert before == after
    events = [json.loads(line) for audit in (path / 'audit').glob('*.jsonl')
        for line in audit.read_text(encoding='utf-8').splitlines()]
    assert any(e['outcome'] == 'success' for e in events)
    assert any(e['outcome'] == 'snapshot_not_found' for e in events)
    assert any(e['outcome'] == 'forbidden' for e in events)
    assert all('sql' not in e and 'parameters' not in e and 'cookie' not in e for e in events)


def test_rejects_unversioned_missing_window_unknown_params(api):
    client, _, _, _ = api
    contract = schema(client)
    body = request(contract)
    body['schema_digest'] = '0' * 64
    assert client.post(PREFIX, json=body).status_code == 409
    body = request(contract)
    del body['parameters']['start_at']
    assert client.post(PREFIX, json=body).status_code == 422
    body = request(contract)
    body['store_id'] = 'shop_B'
    assert client.post(PREFIX, json=body).status_code == 422
    body = request(contract)
    body['parameter_names'] = ['store_id']
    assert client.post(PREFIX, json=body).status_code == 422
    body = request(contract, 'paid_units')
    body['parameters']['sku_id'] = 'foreign'
    assert client.post(PREFIX, json=body).status_code == 422
