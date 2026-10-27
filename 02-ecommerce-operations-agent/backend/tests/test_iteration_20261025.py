"""ITER-20261025-01已复现反例；仅tmp_path本地库，不连接PG。"""
import pytest
from test_query_min import request
from test_query_sync import runtime as runtime_fixture

from ops_agent import query
from ops_agent.errors import AppError


@pytest.fixture
def query_runtime(tmp_path, monkeypatch):
    yield from runtime_fixture.__wrapped__(tmp_path, monkeypatch)


def test_revision_above_archive_integer_range_is_rejected_before_binding(query_runtime):
    engine, principal, _, _ = query_runtime
    body = request(query.schema())
    body['snapshot']['revision'] = 9223372036854775808
    with pytest.raises(AppError) as failure:
        engine.execute(principal, body, 'invalid-revision', 'ref')
    assert failure.value.code == 'query_rejected'


def test_sqlite_unsupported_unicode_whitespace_is_rejected_before_execution(query_runtime):
    engine, principal, _, _ = query_runtime
    body = request(query.schema())
    body['sql'] = '\u00a0' + body['sql']
    with pytest.raises(AppError) as failure:
        engine.execute(principal, body, 'invalid-space', 'ref')
    assert failure.value.code == 'query_rejected'


def test_assistant_explicit_window_does_not_claim_last_thirty_days(query_runtime):
    _, principal, operations, _ = query_runtime
    start, end = '2026-11-01T00:00:00+08:00', '2026-11-02T00:00:00+08:00'
    result = operations.assistant(principal, {'question': '支付金额是多少',
        'snapshot_id': 'query-fixture', 'start_at': start, 'end_at': end})
    assert '近30日' not in result['answer']
    assert any(item['value'] == {'start_at': start, 'end_at': end}
        for item in result['evidence'])


def test_audit_quota_preserves_existing_events_and_rejects_new_append(query_runtime, monkeypatch):
    engine, principal, _, _ = query_runtime
    engine.audit_dir.mkdir(parents=True)
    old = engine.audit_dir / 'retained.jsonl'
    old.write_bytes(b'old\n')
    monkeypatch.setattr(query, 'MAX_AUDIT_BYTES', 4, raising=False)
    with pytest.raises(AppError) as failure:
        engine.audit('quota', 'ref', {}, principal, 'success', 0)
    assert failure.value.code == 'query_audit_unavailable'
    assert old.read_text(encoding='utf-8') == 'old\n'
    assert sum(p.stat().st_size for p in engine.audit_dir.glob('*.jsonl')) == 4
