"""Run-scoped entry guards only; no DB, sockets or live configuration."""
import importlib.util
import sys
from copy import deepcopy
from pathlib import Path

import pytest

ENTRY = Path(__file__).resolve().parents[2] / 'scripts/int_20261026_01.py'


def load():
    assert ENTRY.is_file(), 'run-scoped guarded entry has not been implemented'
    spec = importlib.util.spec_from_file_location('ops_int_entry', ENTRY)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def config(module):
    identities = {}
    for name in ('migration', 'runtime'):
        user = 'synthetic_' + name
        identities[name] = {'url': f'postgresql+psycopg://{user}:synthetic-not-a-secret@192.168.10.100:55432/ops02_int_20261026_01',
            'identity': {'run_id': module.RUN, 'host': '192.168.10.100', 'port': 55432,
                'database': module.DATABASE, 'current_user': user,
                'instance_id': 'be3ca3fc-210b-4eba-a3e1-f550bbefdf44'}}
    return {'run_id': module.RUN, 'api_host': '127.0.0.1', 'api_port': 18120,
        'frontend_port': 18174, 'browser_origin': 'http://127.0.0.1:18173',
        'identity_base_url': 'http://127.0.0.1:18080', 'trade_base_url': 'http://127.0.0.1:18080',
        'clock_file': str(module.CLOCK_PATH), 'models': 'disabled',
        'service_stores': ['int_store_A', 'int_store_B'], 'service_token': 'synthetic-token-not-real',
        'database': identities}


def test_entry_accepts_explicit_run_configuration_without_opening_database():
    module = load()
    value = config(module)
    assert module.validate_config(value)['run_id'] == module.RUN
    assert module.validate_config(value)['database']['runtime']['identity']['database'] == module.DATABASE


@pytest.mark.parametrize(('field', 'value'), [
    ('run_id', 'XOPS-20261023-01'), ('api_host', '0.0.0.0'), ('api_port', 8120),
    ('api_port', True), ('frontend_port', 5174), ('browser_origin', 'http://127.0.0.1:5173'),
    ('identity_base_url', 'http://127.0.0.1:8000'), ('trade_base_url', 'https://example.com'),
    ('clock_file', '.runtime/XOPS-20261023-01/joint-clock.json'), ('models', 'enabled'),
    ('service_stores', []), ('service_stores', ['A', 'A']), ('service_token', ''),
])
def test_wrong_run_endpoints_models_and_scope_rejected(field, value):
    module = load()
    body = config(module)
    body[field] = value
    with pytest.raises(module.Refused):
        module.validate_config(body)


@pytest.mark.parametrize('change', ['old_db', 'sqlite', 'wrong_host', 'wrong_port', 'identity_role', 'instance', 'identity_run', 'same_roles', 'query_override'])
def test_database_target_and_each_role_identity_rejected(change):
    module = load()
    body = config(module)
    record = body['database']['runtime']
    if change == 'old_db':
        record['url'] = record['url'].replace(module.DATABASE, 'ops02_joint_xops_20261023_01')
    elif change == 'sqlite':
        record['url'] = 'sqlite+pysqlite:///demo.db'
    elif change == 'wrong_host':
        record['identity']['host'] = '127.0.0.1'
    elif change == 'wrong_port':
        record['identity']['port'] = 5432
    elif change == 'identity_role':
        record['identity']['current_user'] = 'another_role'
    elif change == 'instance':
        record['identity']['instance_id'] = 'ba6a39ef-0ded-4224-8cb3-ed1d8a96b91d'
    elif change == 'identity_run':
        record['identity']['run_id'] = 'old'
    elif change == 'same_roles':
        body['database']['runtime'] = deepcopy(body['database']['migration'])
    elif change == 'query_override':
        record['url'] += '?host=other-host'
    with pytest.raises(module.Refused):
        module.validate_config(body)


def test_missing_configuration_and_secret_values_are_not_echoed(tmp_path):
    module = load()
    with pytest.raises(module.Refused, match='config_missing'):
        module.read_json(tmp_path / 'missing.json')
    body = config(module)
    secret = body['service_token']
    body['database']['runtime']['url'] = 'bad-url:' + secret
    with pytest.raises(module.Refused) as error:
        module.validate_config(body)
    assert secret not in str(error.value)


def test_clock_requires_zone_and_is_monotonic_across_restart(tmp_path):
    module = load()
    source, high = tmp_path / 'clock.json', tmp_path / 'high.json'
    module.atomic_json(source, {'now': '2026-10-26T04:00:00Z', 'run_id': module.RUN})
    clock = module.RunClock(source, high)
    assert clock.now().isoformat() == '2026-10-26T04:00:00+00:00'
    module.atomic_json(source, {'now': '2026-10-26T05:00:00Z'})
    clock.now()
    module.atomic_json(source, {'now': '2026-10-26T04:59:59Z'})
    with pytest.raises(module.Refused, match='clock_rollback'):
        module.RunClock(source, high).now()
    module.atomic_json(source, {'now': '2026-10-26T06:00:00'})
    with pytest.raises(module.Refused, match='clock_invalid'):
        clock.now()


def test_clock_rejects_foreign_run_and_before_initial_epoch(tmp_path):
    module = load()
    source, high = tmp_path / 'clock.json', tmp_path / 'high.json'
    module.atomic_json(source, {'now': '2026-10-26T04:00:00Z', 'run_id': 'old'})
    with pytest.raises(module.Refused):
        module.RunClock(source, high).now()
    module.atomic_json(source, {'now': '2026-10-25T04:00:00Z'})
    with pytest.raises(module.Refused):
        module.RunClock(source, high).now()


def test_real_stage_requires_explicit_instruction_and_matching_resource_identity():
    module = load()
    body = config(module)
    resources = {'run_id': module.RUN, 'status': 'ready', 'service_stores': body['service_stores'],
        'clock_file': str(module.CLOCK_PATH), 'ops02': {
        **body['database']['runtime']['identity'], 'migration_user': 'synthetic_migration',
        'runtime_user': 'synthetic_runtime'}}
    with pytest.raises(module.Refused, match='stage_not_authorized'):
        module.require_real_stage(None, resources, body)
    assert module.require_real_stage(module.RUN, resources, body) is True
    for name, value in [('status', 'prepared'), ('run_id', 'old')]:
        invalid = deepcopy(resources)
        invalid[name] = value
        with pytest.raises(module.Refused):
            module.require_real_stage(module.RUN, invalid, body)
    resources['ops02']['database'] = 'old'
    with pytest.raises(module.Refused):
        module.require_real_stage(module.RUN, resources, body)


def test_inherited_ops_or_model_route_environment_rejected_and_not_logged():
    module = load()
    with pytest.raises(module.Refused):
        module.reject_inherited_environment({'OPS_DATABASE_URL': 'not-a-real-secret'})
    with pytest.raises(module.Refused):
        module.reject_inherited_environment({'CS_MODEL_SERVICES_FILE': 'old-model-route.json'})
    assert module.reject_inherited_environment({'PATH': 'safe', 'TEMP': 'safe'}) is None


def test_process_ownership_mismatch_refuses_stop():
    module = load()
    recorded = {'pid': 42, 'created': 'instant-A', 'command_line': 'owned-entry --run INT-20261026-01'}
    assert module.same_process(recorded, dict(recorded))
    assert not module.same_process(recorded, {**recorded, 'created': 'instant-B'})
    assert not module.same_process(recorded, {**recorded, 'command_line': 'another-service'})
    assert not module.same_process(recorded, None)


def test_api_start_keeps_integration_mode_and_only_verifies_schema(tmp_path, monkeypatch):
    module = load()
    body = config(module)
    monkeypatch.setattr(module, 'PRIVATE', tmp_path)
    monkeypatch.setenv('OPS_DATABASE_IDENTITY_FILE', 'test-before')
    monkeypatch.setenv('OPS_QUERY_AUDIT_DIR', 'test-before')
    from ops_agent import app as application
    calls = []

    class GuardedDatabase:
        identity_record = {'database': module.DATABASE, 'current_user': 'synthetic_runtime'}

        def __init__(self, url, mode):
            assert url == body['database']['runtime']['url'] and mode == 'integration'
            calls.append('guard_constructor_substitute')

        def initialize(self):
            pytest.fail('serve must never initialize a schema')

        def verify_schema(self):
            calls.append('verify_schema')

        def close(self):
            calls.append('close')

    monkeypatch.setattr(application, 'Database', GuardedDatabase)
    loaded = {'digest': 'a' * 64, 'algorithm': 'test', 'files': {'scripts/int_20261026_01.py': 'b' * 64}}
    def clock():
        return module.EPOCH

    api = module.build_api(body, loaded, factory=application.create_app, clock=clock)
    # Lifespan has only synchronous work before/after yield: drive without a socket/event loop.
    context = api.router.lifespan_context(api)
    with pytest.raises(StopIteration):
        context.__aenter__().send(None)
    assert calls == ['guard_constructor_substitute', 'verify_schema']
    health = next(route.endpoint for route in api.routes if route.path == '/health/integration')()
    assert health['run_id'] == module.RUN and health['loaded_source_digest'] == 'a' * 64
    assert 'url' not in health['database'] and health['models'] == 'disabled'
    with pytest.raises(StopIteration):
        context.__aexit__(None, None, None).send(None)
    assert calls[-1] == 'close'


def test_main_cannot_initialize_without_real_stage_instruction(monkeypatch, capsys):
    module = load()
    body = config(module)
    monkeypatch.setattr(module, 'reject_inherited_environment', lambda env: None)
    monkeypatch.setattr(module, 'read_json', lambda path: body if path == module.CONFIG else {'run_id': module.RUN, 'status': 'ready'})
    calls = []
    monkeypatch.setattr(module, 'initialize', lambda value: calls.append(value))
    monkeypatch.setattr(sys, 'argv', [str(ENTRY), 'init-db', '--run', module.RUN])
    assert module.main() == 2
    assert calls == [] and 'stage_not_authorized' in capsys.readouterr().out


def test_stop_refuses_foreign_pid_before_any_signal(tmp_path, monkeypatch, capsys):
    module = load()
    monkeypatch.setattr(module, 'PRIVATE', tmp_path)
    monkeypatch.setattr(module, 'reject_inherited_environment', lambda env: None)
    recorded = {'pid': 42, 'created': 'A', 'command_line': 'own', 'executable': 'python'}
    module.atomic_json(tmp_path / 'processes.json', {'run_id': module.RUN, 'children': [recorded]})
    monkeypatch.setattr(module, 'process_info', lambda pid: {**recorded, 'created': 'B'})
    calls = []
    monkeypatch.setattr(module, 'stop_owned', lambda child: calls.append(child))
    monkeypatch.setattr(sys, 'argv', [str(ENTRY), 'stop', '--run', module.RUN])
    assert module.main() == 2
    assert calls == [] and 'no_signal_sent' in capsys.readouterr().out
