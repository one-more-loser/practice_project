"""执行桥契约替身测试；不调用真实01或模型。"""
import json
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select
from test_domain import NOW, draft, snapshot

from ops_agent import db, domain
from ops_agent.auth import Principal
from ops_agent.errors import AppError


@pytest.fixture
def setup_bridge(tmp_path):
    from ops_agent.bridge import Bridge, digest
    database = db.Database('sqlite:///' + str(tmp_path / 'bridge.db'), 'demo')
    database.initialize()
    source, spec = snapshot(), draft()
    source['source_kind'] = 'business_import'
    plan = dict(plan_id='plan_A', store_id='store_A', version=1, draft=spec,
                snapshot=source, status='confirmed')
    bridge = Bridge(database, lambda: NOW, service_token='secret', service_stores=('store_A',))
    approval = dict(actor_ref='manager', auth_version=1, capability_revision=2,mode='integration',
                    manifest_digest=digest(bridge.manifest(plan)), source_kind='business_import',
                    evaluation_id='eval_A', evidence_kind='integration', preview=dict(
                        contract_version='XOPS-ACTIVITY-v0.1',store_id='store_A',plan_id='plan_A',
                        plan_version=1,manifest_digest=digest(bridge.manifest(plan)),
                        preview_ref='preview_A',status='passed',evidence_kind='integration',
                        expires_at=(NOW+timedelta(minutes=5)).isoformat()),
                    confirmed_at=NOW.isoformat())
    with database.transaction() as c:
        c.execute(db.snapshots.insert().values(store_id='store_A', snapshot_id=source['snapshot_id'],
            revision=1, digest=digest(source), body=source, created_at=NOW.isoformat()))
        c.execute(db.plans.insert().values(plan_id='plan_A', store_id='store_A', version=1,
            status='confirmed', created_at=NOW.isoformat(), updated_at=NOW.isoformat()))
        c.execute(db.revisions.insert().values(plan_id='plan_A', version=1, store_id='store_A',
            body=spec, digest=digest(spec), created_at=NOW.isoformat()))
        c.execute(db.evaluations.insert().values(evaluation_id='eval_A', plan_id='plan_A', version=1,
            store_id='store_A', body=domain.evaluate_plan(spec, source, NOW), created_at=NOW.isoformat()))
        c.execute(db.approvals.insert().values(approval_ref='approval_A', plan_id='plan_A',version=1,
            store_id='store_A',body=approval,created_at=NOW.isoformat()))
    payload = dict(approval_ref='approval_A', intent_id='intent_A',
                   manifest_digest=approval['manifest_digest'], actor_ref='manager',
                   auth_version=1, capability_revision=2, preview_ref='preview_A')
    yield bridge, database, plan, payload
    bridge.close()
    database.close()


def test_manifest_is_controlled_and_fixed_quantity(setup_bridge):
    bridge, _, plan, _ = setup_bridge
    manifest = bridge.manifest(plan)
    assert manifest['lines'] == [{'sku_id': 'A', 'base_price_minor': '10000',
        'activity_price_minor': '9200', 'quantity_cap': 100, 'price_version': 1}]
    assert manifest['variable_budget_cap_minor'] == '90000'
    plan['draft']['goal']['scenario_policy'] = 'all'
    assert bridge.manifest(plan)['lines'][0]['quantity_cap'] == 120
    plan['draft']['selected_candidate'] = 'coupon'
    with pytest.raises(AppError):
        bridge.manifest(plan)


def test_internal_auth_fails_closed(setup_bridge):
    bridge, *_ = setup_bridge
    bridge.authenticate({'Authorization': 'Bearer secret'}, 'store_A')
    for headers, store in [({}, 'store_A'), ({'Authorization': 'Bearer secret'}, 'store_B')]:
        with pytest.raises(AppError):
            bridge.authenticate(headers, store)


def test_preview_requires_configuration_and_checks_binding(setup_bridge):
    from ops_agent.bridge import Bridge, digest
    bridge, database, plan, _ = setup_bridge
    principal = Principal('manager', 'store_A', ('activity.publish',), 'csrf',
                          auth_version=1, capability_revision=2)
    request = SimpleNamespace(cookies={'cs_session': 'cookie'}, headers={'Origin': 'https://shop.test'})
    with pytest.raises(AppError) as caught:
        bridge.preview(plan, principal, request)
    assert caught.value.status == 503
    def respond(req):
        assert req.headers['cookie'] == 'cs_session=cookie'
        assert req.headers['x-csrf-token'] == 'csrf'
        return httpx.Response(200, json={'data': dict(contract_version='XOPS-ACTIVITY-v0.1',
            store_id='store_A', plan_id='plan_A', plan_version=1,
            manifest_digest=digest(bridge.manifest(plan)),preview_ref='preview_A', status='passed',
            expires_at=(NOW+timedelta(minutes=5)).isoformat())})
    gateway = Bridge(database, lambda: NOW, base_url='https://shop.test',
                     transport=httpx.MockTransport(respond))
    result = gateway.preview(plan, principal, request)
    assert result['evidence_kind'] == 'contract_stub'
    gateway.close()


def test_grant_is_approval_unique_and_never_reopens(setup_bridge):
    bridge, database, _, payload = setup_bridge
    result = bridge.grant('store_A', 'plan_A', payload)
    assert result == bridge.grant('store_A', 'plan_A', payload)
    assert result['expires_at'] == (NOW+timedelta(seconds=60)).isoformat()
    with pytest.raises(AppError):
        bridge.grant('store_A', 'plan_A', dict(payload, intent_id='another'))
    bridge.clock = lambda: NOW+timedelta(minutes=2)
    assert bridge.grant('store_A', 'plan_A', payload) == result
    with database.transaction() as c:
        assert c.execute(select(db.grants.c.state)).scalar_one() == 'result_unknown'


@pytest.mark.parametrize('change', ['sandbox', 'digest', 'actor', 'stale_snapshot', 'no_eval',
                                    'expired_preview','stub_preview','mode_missing'])
def test_grant_revalidates_database_authority(setup_bridge, change):
    bridge, database, _, payload = setup_bridge
    with database.transaction() as c:
        if change == 'sandbox':
            c.execute(db.plans.update().values(status='sandbox_confirmed'))
        elif change == 'digest':
            payload['manifest_digest'] = '0'*64
        elif change == 'actor':
            payload['actor_ref'] = 'attacker'
        elif change == 'stale_snapshot':
            source = snapshot()
            source['revision'] = 2
            c.execute(db.snapshots.insert().values(store_id='store_A', snapshot_id=source['snapshot_id'],
                revision=2, digest='a'*64, body=source, created_at=NOW.isoformat()))
        elif change == 'no_eval':
            c.execute(db.evaluations.delete())
        else:
            body = deepcopy(c.execute(select(db.approvals.c.body)).scalar_one())
            if change == 'expired_preview':
                body['preview']['expires_at'] = NOW.isoformat()
            elif change == 'stub_preview':
                body['preview']['evidence_kind'] = 'contract_stub'
            else:
                body.pop('mode')
            c.execute(db.approvals.update().values(body=body))
    with pytest.raises(AppError):
        bridge.grant('store_A', 'plan_A', payload)


def receipt(payload, **kwargs):
    return dict(contract_version='XOPS-ACTIVITY-v0.1', event_id='event_A', store_id='store_A',
        intent_id=payload['intent_id'], approval_ref=payload['approval_ref'],
        manifest_digest=payload['manifest_digest'], status='committed', terminal_revision=1,
        activity_id='activity_A', activity_revision=1, phase='scheduled', **kwargs)


def test_receipt_deduplicates_and_conflicts_never_overwrite(setup_bridge):
    bridge, database, _, payload = setup_bridge
    bridge.grant('store_A', 'plan_A', payload)
    value = receipt(payload)
    assert bridge.receipt('store_A', value) == bridge.receipt('store_A', value)
    with pytest.raises(AppError):
        bridge.receipt('store_A', dict(value, event_id='event_B', status='closed_no_effect',
            activity_id=None, activity_revision=None,phase=None, terminal_revision=2))
    with database.transaction() as c:
        assert c.execute(select(db.grants.c.state)).scalar_one() == 'committed'


def test_close_requires_durable_terminal_revision(setup_bridge):
    bridge, database, _, payload = setup_bridge
    bridge.grant('store_A', 'plan_A', payload)
    value = receipt(payload)
    value.update(status='closed_no_effect', activity_id=None,activity_revision=None,
                 phase=None,terminal_revision=0)
    with pytest.raises(AppError):
        bridge.receipt('store_A', value)
    assert bridge.reconcile('store_A', 'intent_A')['status'] == 'result_unknown'
    with database.transaction() as c:
        assert c.execute(select(db.grants.c.state)).scalar_one() == 'result_unknown'


def test_actuals_revisions_replace_not_accumulate(setup_bridge):
    bridge, _, _, payload = setup_bridge
    bridge.grant('store_A', 'plan_A', payload)
    bridge.receipt('store_A', receipt(payload))
    bridge.clock = lambda: NOW + timedelta(days=1)
    value = dict(contract_version='XOPS-ACTIVITY-v0.1', store_id='store_A', snapshot_id='actual_A',
        activity_id='activity_A', revision=2, source_kind='business_import',
        window_start=draft()['start_at'],window_end=draft()['end_at'],bucket='activity_cumulative',
        watermark=bridge.clock().isoformat(), payment_minor='100', refund_minor='20',
        consumed_discount_minor='5', reserved_discount_minor='0', paid_units=1)
    assert bridge.actuals('store_A', value)['revision'] == 2
    assert bridge.actuals('store_A', value)['revision'] == 2
    assert bridge.actuals('store_A', dict(value, revision=1))['revision'] == 2
    with pytest.raises(AppError):
        bridge.actuals('store_A', dict(value, payment_minor='200'))
    with pytest.raises(AppError):
        bridge.actuals('store_A', dict(value, snapshot_id='another', revision=1))


def test_monitoring_partial_full_and_snapshot_scope(setup_bridge):
    from ops_agent.auth import OWNER
    from ops_agent.service import Operations
    bridge, database, _, payload = setup_bridge
    bridge.grant('store_A', 'plan_A', payload)
    bridge.receipt('store_A', receipt(payload))
    clock = NOW + timedelta(days=1)
    bridge.clock = lambda: clock
    operations = Operations(database, lambda: clock, 'test', bridge)
    p = Principal('manager', 'store_A', OWNER, 'csrf')
    value = dict(contract_version='XOPS-ACTIVITY-v0.1', store_id='store_A', snapshot_id='actual_A',
        activity_id='activity_A', revision=1, source_kind='business_import',
        window_start=draft()['start_at'], window_end=draft()['end_at'], bucket='activity_cumulative',
        watermark=clock.isoformat(), payment_minor='100', refund_minor='20',
        consumed_discount_minor='5', reserved_discount_minor='0', paid_units=1)
    bridge.actuals('store_A', value)
    result = operations.monitor(p, 'plan_A')
    assert result['actual']['net_paid_minor'] == '80'
    assert result['actual']['contribution_profit_minor'] is None
    assert any(d['status'] == 'partial' for d in result['deviations'])
    assert not any(d['metric'] == 'payment_vs_scenario_minor' for d in result['deviations'])
    assert operations.activity_list(p)['items'][0]['plan_id'] == 'plan_A'
    clock = NOW + timedelta(days=2)
    bridge.actuals('store_A', dict(value, revision=2, watermark=clock.isoformat(), payment_minor='900000'))
    result = operations.monitor(p, 'plan_A')
    assert result['actual']['revision'] == 2
    assert any(d['metric'] == 'payment_vs_scenario_minor' and d['value'] == '-20000'
               for d in result['deviations'])


def test_nonzero_actual_before_activity_is_rejected(setup_bridge):
    bridge, _, _, payload = setup_bridge
    bridge.grant('store_A', 'plan_A', payload)
    bridge.receipt('store_A', receipt(payload))
    value = dict(contract_version='XOPS-ACTIVITY-v0.1', store_id='store_A', snapshot_id='actual_A',
        activity_id='activity_A', revision=1, source_kind='business_import',
        window_start=draft()['start_at'], window_end=draft()['end_at'], bucket='activity_cumulative',
        watermark=NOW.isoformat(), payment_minor='100', refund_minor='20',
        consumed_discount_minor='5', reserved_discount_minor='0', paid_units=1)
    with pytest.raises(AppError) as caught:
        bridge.actuals('store_A', value)
    assert caught.value.status == 422


def test_application_cancel_unknown_stays_pending_and_reconcilable(setup_bridge):
    from ops_agent.auth import OWNER
    from ops_agent.service import Operations
    bridge, database, _, payload = setup_bridge
    bridge.grant('store_A', 'plan_A', payload)
    operations = Operations(database, lambda: NOW, 'test', bridge)
    p = Principal('manager', 'store_A', OWNER, 'csrf', capability_revision=2)
    result = operations.cancel_approval(p, 'plan_A', 1, '经营条件变化', 'cancel')
    assert result['status'] == 'cancel_pending'
    assert operations.reconcile_execution(p, 'plan_A', 1)['status'] == 'cancel_pending'
    with database.transaction() as c:
        assert c.execute(select(db.grants.c.state)).scalar_one() == 'result_unknown'


def test_application_cancel_known_committed_keeps_activity_fact(setup_bridge):
    from ops_agent.auth import OWNER
    from ops_agent.service import Operations
    bridge, database, _, payload = setup_bridge
    bridge.grant('store_A', 'plan_A', payload)
    bridge.receipt('store_A', receipt(payload))
    operations = Operations(database, lambda: NOW, 'test', bridge)
    p = Principal('manager', 'store_A', OWNER, 'csrf', capability_revision=2)
    result = operations.cancel_approval(p, 'plan_A', 1, '已提交后请求取消', 'cancel')
    assert result['status'] == 'scheduled'
    assert result['cancellation'] is not None


@pytest.mark.parametrize('change', ['store_id','plan_id','plan_version','manifest_digest',
                                    'contract_version','status','expires_at'])
def test_preview_rejects_unbound_or_expired_response(setup_bridge, change):
    from ops_agent.bridge import Bridge, digest
    _, database, plan, _ = setup_bridge
    def respond(req):
        value = dict(contract_version='XOPS-ACTIVITY-v0.1',store_id='store_A',plan_id='plan_A',
            plan_version=1,manifest_digest=digest(bridge.manifest(plan)),preview_ref='preview_A',
            status='passed',expires_at=(NOW+timedelta(minutes=5)).isoformat())
        value[change] = NOW.isoformat() if change == 'expires_at' else 'invalid'
        return httpx.Response(200,json={'data':value})
    bridge = Bridge(database,lambda:NOW,base_url='https://shop.test',transport=httpx.MockTransport(respond))
    principal = Principal('manager','store_A',('activity.publish',),'csrf')
    request = SimpleNamespace(cookies={'cs_session':'cookie'},headers={})
    with pytest.raises(AppError):
        bridge.preview(plan,principal,request)
    bridge.close()


@pytest.mark.parametrize('result', ['missing','timeout','unknown'])
def test_reconcile_missing_or_timeout_never_unfreezes(setup_bridge, result):
    from ops_agent.bridge import Bridge
    bridge, database, _, payload = setup_bridge
    bridge.grant('store_A','plan_A',payload)
    def respond(req):
        assert req.method == 'POST'
        assert req.url.path.endswith('/finalize-expired')
        if result == 'timeout':
            raise httpx.ReadTimeout('bounded test')
        return httpx.Response(404 if result == 'missing' else 200,
                              json={'data':{'status':'unknown'}})
    gateway = Bridge(database,lambda:NOW+timedelta(minutes=2),base_url='https://shop.test',
        service_token='secret',service_stores=('store_A',),transport=httpx.MockTransport(respond))
    assert gateway.reconcile('store_A','intent_A',True)['status'] == 'result_unknown'
    with database.transaction() as c:
        assert c.execute(select(db.grants.c.state)).scalar_one() == 'result_unknown'
    gateway.close()


@pytest.mark.parametrize('value', [None, [], 'unknown', 1, True])
def test_reconcile_malformed_data_keeps_execution_unknown(setup_bridge, value):
    from ops_agent.bridge import Bridge
    bridge, database, _, payload = setup_bridge
    bridge.grant('store_A', 'plan_A', payload)
    gateway = Bridge(database, lambda: NOW, base_url='https://shop.test',
        service_token='secret', service_stores=('store_A',),
        transport=httpx.MockTransport(lambda req: httpx.Response(200, json={'data': value})))
    try:
        assert gateway.reconcile('store_A', 'intent_A')['status'] == 'result_unknown'
        with database.transaction() as c:
            assert c.execute(select(db.grants.c.state)).scalar_one() == 'result_unknown'
            assert c.execute(select(db.plans.c.status)).scalar_one() == 'publishing'
    finally:
        gateway.close()


def test_durable_closed_receipt_and_late_commit_conflict(setup_bridge):
    bridge, database, _, payload = setup_bridge
    original = bridge.grant('store_A','plan_A',payload)
    closed = dict(receipt(payload),status='closed_no_effect',activity_id=None,activity_revision=None,phase=None)
    bridge.receipt('store_A',closed)
    assert bridge.grant('store_A','plan_A',payload) == original
    with pytest.raises(AppError):
        bridge.receipt('store_A',dict(receipt(payload),event_id='late_commit',terminal_revision=2))
    with database.transaction() as c:
        assert c.execute(select(db.grants.c.state)).scalar_one() == 'closed_no_effect'


def test_activity_phase_revision_replaces_and_old_event_cannot_rewind(setup_bridge):
    bridge, database, _, payload = setup_bridge
    bridge.grant('store_A','plan_A',payload)
    initial = receipt(payload)
    bridge.receipt('store_A',initial)
    paused = dict(initial,event_id='phase_2',activity_revision=2,phase='paused')
    bridge.receipt('store_A',paused)
    bridge.receipt('store_A',dict(initial,event_id='old_event'))
    with database.transaction() as c:
        assert c.execute(select(db.activities.c.revision)).scalar_one() == 2
        assert c.execute(select(db.plans.c.status)).scalar_one() == 'paused'
    assert bridge.reconcile('store_A','intent_A')['phase'] == 'paused'
    with pytest.raises(AppError):
        bridge.receipt('store_A',dict(paused,event_id='phase_conflict',phase='active'))
    ended = dict(paused,event_id='phase_3',activity_revision=3,phase='ended')
    bridge.receipt('store_A',ended)
    with pytest.raises(AppError):
        bridge.receipt('store_A',dict(ended,event_id='phase_4',activity_revision=4,phase='active'))


def test_actuals_require_bound_activity_window(setup_bridge):
    bridge, _, _, payload = setup_bridge
    bridge.grant('store_A','plan_A',payload)
    bridge.receipt('store_A',receipt(payload))
    value = dict(contract_version='XOPS-ACTIVITY-v0.1',store_id='store_A',snapshot_id='actual_A',
        activity_id='activity_A',revision=1,source_kind='business_import',watermark=NOW.isoformat(),
        payment_minor='100',refund_minor='0',consumed_discount_minor='5',reserved_discount_minor='0',
        paid_units=1,bucket='activity_cumulative',window_start=NOW.isoformat(),window_end=draft()['end_at'])
    with pytest.raises(AppError):
        bridge.actuals('store_A',value)


@pytest.mark.parametrize('terminal', ['committed', 'closed_no_effect'])
def test_cancel_consumes_trusted_slot_terminal(setup_bridge, terminal):
    from ops_agent.bridge import Bridge
    bridge, database, _, payload = setup_bridge
    granted = bridge.grant('store_A', 'plan_A', payload)
    context = dict(actor_ref='manager', auth_version=1, capability_revision=2, reason='撤销方案')
    value = receipt(payload)
    if terminal == 'closed_no_effect':
        value.update(status=terminal, activity_id=None, activity_revision=None, phase=None)
    def respond(req):
        assert req.method == 'POST'
        assert req.url.path == '/api/v1/internal/stores/store_A/execution-intents/intent_A/cancel-request'
        assert req.headers['authorization'] == 'Bearer secret'
        assert json.loads(req.content) == dict(context, grant_ref=granted['grant_ref'])
        return httpx.Response(200, json={'data': value})
    gateway = Bridge(database, lambda: NOW, base_url='https://shop.test', service_token='secret',
        service_stores=('store_A',), transport=httpx.MockTransport(respond))
    assert gateway.cancel('store_A', 'intent_A', context) == value
    with database.transaction() as c:
        assert c.execute(select(db.grants.c.state)).scalar_one() == terminal
    gateway.close()


@pytest.mark.parametrize('failure', ['timeout', 'missing', 'bad_contract', 'other_intent', 'unknown'])
def test_cancel_ambiguous_result_preserves_freeze(setup_bridge, failure):
    from ops_agent.bridge import Bridge
    bridge, database, _, payload = setup_bridge
    bridge.grant('store_A', 'plan_A', payload)
    def respond(req):
        if failure == 'timeout':
            raise httpx.ReadTimeout('bounded test')
        value = receipt(payload)
        if failure == 'bad_contract':
            value = []
        elif failure == 'other_intent':
            value['intent_id'] = 'other_intent'
        elif failure == 'unknown':
            value = {'status': 'unknown'}
        return httpx.Response(404 if failure == 'missing' else 200, json={'data': value})
    gateway = Bridge(database, lambda: NOW, base_url='https://shop.test', service_token='secret',
        service_stores=('store_A',), transport=httpx.MockTransport(respond))
    context = dict(actor_ref='manager', auth_version=1, capability_revision=2, reason='撤销方案')
    assert gateway.cancel('store_A', 'intent_A', context)['status'] == 'result_unknown'
    with database.transaction() as c:
        assert c.execute(select(db.grants.c.state)).scalar_one() == 'result_unknown'
        assert c.execute(select(db.plans.c.status)).scalar_one() == 'publishing'
    gateway.close()


def test_cancel_unconfigured_or_cross_store_never_invents_success(setup_bridge):
    bridge, database, _, payload = setup_bridge
    bridge.grant('store_A', 'plan_A', payload)
    context = dict(actor_ref='manager', auth_version=1, capability_revision=2, reason='撤销方案')
    assert bridge.cancel('store_A', 'intent_A', context)['status'] == 'result_unknown'
    with pytest.raises(AppError) as caught:
        bridge.cancel('store_B', 'intent_A', context)
    assert caught.value.status == 404
    bridge.receipt('store_A', receipt(payload))
    assert bridge.cancel('store_A', 'intent_A', context)['status'] == 'committed'
    with database.transaction() as c:
        assert c.execute(select(db.plans.c.status)).scalar_one() == 'scheduled'


def test_cancel_rejects_client_grant_ref_override(setup_bridge):
    bridge, _, _, payload = setup_bridge
    bridge.grant('store_A', 'plan_A', payload)
    context = dict(actor_ref='manager', auth_version=1, capability_revision=2, reason='撤销方案',
                   grant_ref='forged')
    with pytest.raises(AppError):
        bridge.cancel('store_A', 'intent_A', context)


def test_complete_export_includes_bound_execution_archives(setup_bridge):
    from ops_agent.auth import OWNER
    from ops_agent.service import Operations
    bridge, database, _, payload = setup_bridge
    bridge.grant('store_A', 'plan_A', payload)
    bridge.receipt('store_A', receipt(payload))
    operations = Operations(database, lambda: NOW, 'test', bridge)
    principal = Principal('manager', 'store_A', OWNER, 'csrf')
    for format in ('markdown', 'csv'):
        report, _ = operations.export(principal, 'plan_A', format)
        assert 'intent_A' in report
        assert 'activity_A' in report
        assert 'actual_revisions' in report
        assert 'retrospectives' in report


@pytest.mark.parametrize('source_kind', ['synthetic', 'derived'])
@pytest.mark.parametrize('revision', [1, 2, 3])
def test_actuals_source_is_bound_to_grant_even_for_old_revision(setup_bridge, source_kind, revision):
    bridge, database, _, payload = setup_bridge
    bridge.grant('store_A', 'plan_A', payload)
    bridge.receipt('store_A', receipt(payload))
    bridge.clock = lambda: NOW + timedelta(days=1)
    value = dict(contract_version='XOPS-ACTIVITY-v0.1', store_id='store_A', snapshot_id='actual_A',
        activity_id='activity_A', revision=2, source_kind='business_import',
        window_start=draft()['start_at'], window_end=draft()['end_at'], bucket='activity_cumulative',
        watermark=bridge.clock().isoformat(), payment_minor='100', refund_minor='20',
        consumed_discount_minor='5', reserved_discount_minor='0', paid_units=1)
    bridge.actuals('store_A', value)
    with pytest.raises(AppError, match='来源必须与原授权'):
        bridge.actuals('store_A', dict(value, source_kind=source_kind, revision=revision))
    with database.transaction() as connection:
        saved = connection.execute(select(db.actuals.c.body)).scalars().all()
    assert saved == [value]
