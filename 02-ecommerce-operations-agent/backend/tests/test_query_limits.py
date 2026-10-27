"""额外执行证据：真实聚合、精度、摘要损坏、限额、CSRF。所有DB来自tmp_path。"""
from copy import deepcopy

import pytest
from sqlalchemy import select
from test_query_min import PREFIX, request, schema, snapshot
from test_query_min import api as api_fixture

from ops_agent import db
from ops_agent import query as engine


@pytest.fixture
def api(tmp_path, monkeypatch):
    yield from api_fixture.__wrapped__(tmp_path, monkeypatch)


def test_actual_sql_aggregate_and_bigint_total(api, monkeypatch):
    client, _, _, _ = api
    source = snapshot()
    amount = '9223372036854775807'
    base = deepcopy(source['orders'][0])
    base['paid_minor'] = amount
    base['items'] = [{'sku_id': 'A', 'qty': 1, 'paid_minor': amount}]
    source['orders'] = [dict(deepcopy(base), order_id='large1'),
                        dict(deepcopy(base), order_id='large2')]
    source['revision'] = 2
    assert client.post('/api/ops/v1/snapshots', json=source).status_code == 201
    steps = []
    original = engine.ExactSum.step
    def step(self, value):
        steps.append(value)
        return original(self, value)
    monkeypatch.setattr(engine.ExactSum, 'step', step)
    body = request(schema(client))
    body['snapshot']['revision'] = 2
    result = client.post(PREFIX, json=body)
    assert result.status_code == 200, result.text
    assert result.json()['data']['rows'] == [{'payment_minor': '18446744073709551614'}]
    assert steps == [amount, amount]


def test_digest_corruption_is_fail_closed(api):
    client, _, app, _ = api
    body = request(schema(client))
    with app.state.database.transaction() as connection:
        row = connection.execute(select(db.snapshots)).mappings().first()
        source = deepcopy(row['body'])
        source['orders'] = []
        connection.execute(db.snapshots.update().values(body=source))
    result = client.post(PREFIX, json=body)
    assert result.status_code == 409
    assert result.json()['error']['code'] == 'snapshot_digest_conflict'


def test_channel_visible_empty_and_csrf(api):
    client, _, _, _ = api
    body = request(schema(client), 'paid_order_count')
    body['parameters']['channel'] = 'unknown'
    assert client.post(PREFIX, json=body).json()['data']['rows'] == [{'paid_order_count': 1}]
    body['parameters']['as_of'] = '2026-10-01T00:00:00+08:00'
    assert client.post(PREFIX, json=body).json()['data']['rows'] == [{'paid_order_count': 0}]
    assert client.post(PREFIX, json=body, headers={'X-CSRF-Token': 'bad'}).status_code == 403
    assert client.post(PREFIX, json=body, headers={'Origin': 'https://foreign'}).status_code == 403


def test_resource_timeout_and_concurrency(api, monkeypatch):
    client, _, _, _ = api
    body = request(schema(client))
    monkeypatch.setitem(engine.LIMITS, 'timeout_ms', 0)
    assert client.post(PREFIX, json=body).status_code == 408
    monkeypatch.setitem(engine.LIMITS, 'timeout_ms', 1000)
    monkeypatch.setitem(engine.LIMITS, 'max_items', 1)
    assert client.post(PREFIX, json=body).status_code == 422
    assert engine.SLOTS.acquire(False)
    assert engine.SLOTS.acquire(False)
    try:
        assert client.post(PREFIX, json=body).status_code == 429
    finally:
        engine.SLOTS.release()
        engine.SLOTS.release()
