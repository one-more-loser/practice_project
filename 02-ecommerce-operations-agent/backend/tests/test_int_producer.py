"""Actual01 public shapes + synthetic secrets; no private file or network used."""
import hashlib
import json
from copy import deepcopy

import pytest
from test_int_run_entry import config, load


def producer(module):
    body = config(module)
    body['service_stores'] = ['int_store_a']
    for role, user in [('migration', 'opsint_20261026_owner'), ('runtime', 'opsint_20261026_app')]:
        old_user = body['database'][role]['identity']['current_user']
        body['database'][role]['url'] = body['database'][role]['url'].replace(old_user, user)
        body['database'][role]['identity'].update(current_user=user, instance_id=module.OPS_INSTANCE)
    effective = {'integration_run_id': module.RUN, 'host': '127.0.0.1', 'port': 18080,
        'origin': body['browser_origin'], 'ops_base_url': 'http://127.0.0.1:18120',
        'stores': ['int_store_a'], 'clock_file': str(module.CLOCK_PATH), 'models': 'disabled',
        'workers': {'agent': False, 'expiration': False, 'activity_delivery': False},
        'source_digest': module.CS_SOURCE, 'config_digest': 'c' * 64}
    ready = {'integration_run_id': module.RUN, 'status': 'ready',
        'container': {'host': '192.168.10.100', 'port': 55432},
        'resources': {'integration_run_id': module.RUN, 'databases': {'ops': module.DATABASE},
            'roles': {'ops_owner': 'opsint_20261026_owner', 'ops_app': 'opsint_20261026_app'},
            'instance_ids': {'ops': module.OPS_INSTANCE}},
        'clock': {'file': str(module.CLOCK_PATH)}}
    files = {}
    for role in ('migration', 'runtime'):
        files[role + '-private.json'] = {'integration_run_id': module.RUN, 'url': body['database'][role]['url']}
        identity = deepcopy(body['database'][role]['identity'])
        identity.pop('run_id')
        files[role + '-identity.json'] = identity
    files['service-private.json'] = {'integration_run_id': module.RUN, 'service_token': body['service_token'],
        'stores': body['service_stores'], 'clock_file': str(module.CLOCK_PATH)}
    files['accounts-private.json'] = {name: 'synthetic-password-not-real' for name in module.ACCOUNT_NAMES}
    clock = {'integration_run_id': module.RUN, 'now': '2026-10-26T04:00:00+00:00'}
    return ready, effective, files, clock


def test_actual01_shapes_adapt_without_secret_echo():
    module = load()
    ready, effective, files, clock = producer(module)
    body, accounts = module.adapt_producer(ready, effective, files, clock)
    assert module.validate_config(body) == body
    assert set(accounts) == set(module.ACCOUNT_NAMES)
    assert module.require_real_stage(module.RUN, module.normalize_resources(ready, effective), body)


@pytest.mark.parametrize('mutation', [
    'ready_run', 'nested_run', 'missing_private_run', 'private_run', 'identity_run', 'dual_run',
    'instance', 'role', 'dsn', 'store', 'clock_path', 'clock_run', 'clock_dual', 'clock_missing_run',
    'token_empty', 'token_type', 'enabled_model', 'worker_enabled', 'source', 'account_shape',
])
def test_producer_groups_reject_mismatches(mutation):
    module = load()
    ready, effective, files, clock = producer(module)
    if mutation == 'ready_run':
        ready['integration_run_id'] = 'old'
    elif mutation == 'nested_run':
        ready['resources']['integration_run_id'] = 'old'
    elif mutation == 'missing_private_run':
        files['migration-private.json'].pop('integration_run_id')
    elif mutation == 'private_run':
        files['runtime-private.json']['integration_run_id'] = 'old'
    elif mutation == 'identity_run':
        files['migration-identity.json']['integration_run_id'] = 'old'
    elif mutation == 'dual_run':
        files['service-private.json']['run_id'] = 'old'
    elif mutation == 'instance':
        files['runtime-identity.json']['instance_id'] = 'd70e174e-ffae-42fc-969c-4f90eff159d5'
    elif mutation == 'role':
        files['runtime-identity.json']['current_user'] = 'opsint_20261026_owner'
    elif mutation == 'dsn':
        files['runtime-private.json']['url'] += '?host=wrong'
    elif mutation == 'store':
        files['service-private.json']['stores'] = ['int_store_b']
    elif mutation == 'clock_path':
        files['service-private.json']['clock_file'] = 'old/clock.json'
    elif mutation == 'clock_run':
        clock['integration_run_id'] = 'old'
    elif mutation == 'clock_dual':
        clock['run_id'] = 'old'
    elif mutation == 'clock_missing_run':
        clock.pop('integration_run_id')
    elif mutation == 'token_empty':
        files['service-private.json']['service_token'] = ''
    elif mutation == 'token_type':
        files['service-private.json']['service_token'] = []
    elif mutation == 'enabled_model':
        effective['models'] = 'enabled'
    elif mutation == 'worker_enabled':
        effective['workers']['activity_delivery'] = True
    elif mutation == 'source':
        effective['source_digest'] = '0' * 64
    elif mutation == 'account_shape':
        files['accounts-private.json']['unexpected'] = 'synthetic'
    with pytest.raises(module.Refused):
        module.adapt_producer(ready, effective, files, clock)


def test_producer_clock_strict_path_rejects_missing_and_foreign_run(tmp_path):
    module = load()
    source, high = tmp_path / 'clock.json', tmp_path / 'high.json'
    clock = module.RunClock(source, high, producer=True)
    for value in [{'now': module.EPOCH.isoformat()}, {'now': module.EPOCH.isoformat(), 'integration_run_id': 'old', 'run_id': module.RUN}]:
        module.atomic_json(source, value)
        with pytest.raises(module.Refused):
            clock.now()
    module.atomic_json(source, {'now': module.EPOCH.isoformat(), 'integration_run_id': module.RUN})
    assert clock.now() == module.EPOCH


def test_live_health_binds_source_config_workers_and_database():
    module = load()
    ready, effective, _, _ = producer(module)
    health = {'integration_run_id': module.RUN, 'service': 'cs01', 'ready': True,
        'loaded_source_digest': effective['source_digest'], 'config_digest': effective['config_digest'],
        'models': 'disabled', 'model_capabilities': [], 'workers': effective['workers'],
        'database': 'cs_agent_int_20261026_01', 'database_identity': {'host': '192.168.10.100', 'port': 55432,
            'server_port': 5432, 'app_role': 'csint_20261026_app', 'instance_id': 'e1a0d2b3-ba15-4461-b383-099ad8183b7b'}}
    assert module.verify_producer_health(ready, effective, health)
    for field, value in [('loaded_source_digest', '0' * 64), ('config_digest', '0' * 64), ('models', 'enabled'), ('ready', False)]:
        changed = {**health, field: value}
        with pytest.raises(module.Refused):
            module.verify_producer_health(ready, effective, changed)


def test_effective_config_is_indexed_file_inside_run_delivery(tmp_path, monkeypatch):
    module = load()
    ready, effective, _, _ = producer(module)
    public = tmp_path / 'public'
    effective_path = public / 'evidence/service-effective-config.json'
    module.atomic_json(effective_path, effective)
    index_path = public / 'evidence-index.json'
    module.atomic_json(index_path, {'integration_run_id': module.RUN, 'files': {
        'evidence/service-effective-config.json': hashlib.sha256(effective_path.read_bytes()).hexdigest()}})
    ready.update(effective_config=str(effective_path), evidence_index={
        'path': str(index_path), 'sha256': hashlib.sha256(index_path.read_bytes()).hexdigest()})
    ready_path, gate_path = public / 'resources-ready.json', tmp_path / 'gate.json'
    module.atomic_json(ready_path, ready)
    gate = {'run_id': module.RUN, 'allowed_stage': '02_adapter_initialize_start_and_basic_smoke',
        'resources_ready_sha256': hashlib.sha256(ready_path.read_bytes()).hexdigest(),
        'producer_source_digest': module.CS_SOURCE, 'ops_instance_id': module.OPS_INSTANCE,
        'no_activity_or_faults_or_clock_advance': True}
    module.atomic_json(gate_path, gate)
    monkeypatch.setattr(module, 'READY', ready_path)
    monkeypatch.setattr(module, 'GATE', gate_path)
    assert module.load_resources()[1] == effective
    for changed in [
        {**ready, 'effective_config': effective},
        {**ready, 'effective_config': str(tmp_path / 'outside.json')},
        {**ready, 'evidence_index': {**ready['evidence_index'], 'sha256': '0' * 64}},
    ]:
        module.atomic_json(ready_path, changed)
        with pytest.raises(module.Refused):
            module.load_resources()
    module.atomic_json(ready_path, ready)
    effective_path.write_text(json.dumps({**effective, 'models': 'enabled'}), encoding='utf-8')
    with pytest.raises(module.Refused, match='producer_effective_digest_conflict'):
        module.load_resources()


def test_windows_venv_launcher_resolves_only_verified_own_child():
    module = load()
    launcher = {'pid': 101, 'service': 'api', 'port': 18120, 'created': 'start',
        'command_line': str(module.__file__) + ' --run ' + module.RUN, 'executable': str(module.sys.executable)}
    child = {'pid': 102, 'parent_pid': 101, 'created': 'child-start',
        'command_line': launcher['command_line'], 'executable': str(module.sys._base_executable)}
    result = module.runtime_identity(launcher, 102, inspect=lambda pid: child)
    assert result['pid'] == 102 and result['launcher_identity']['pid'] == 101
    assert len(module.managed_processes({'children': [result]})) == 2
    for changed in [{**child, 'parent_pid': 999}, {**child, 'command_line': 'another-service'}, {**child, 'executable': 'another.exe'}]:
        with pytest.raises(module.Refused):
            module.runtime_identity(launcher, 102, inspect=lambda pid: changed)


def test_ledger_hardening_only_revokes_mutation_on_schema_ledger():
    module = load()
    from contextlib import contextmanager
    from types import SimpleNamespace

    from sqlalchemy.dialects.postgresql import dialect

    statements = []

    @contextmanager
    def transaction():
        yield SimpleNamespace(execute=lambda statement: statements.append(str(statement)))

    database = SimpleNamespace(engine=SimpleNamespace(dialect=dialect()), transaction=transaction)
    module.harden_ledger(config(module), database=database)
    assert len(statements) == 1
    assert 'REVOKE INSERT, UPDATE, DELETE' in statements[0]
    assert 'ON TABLE public.ops_schema_versions' in statements[0]
    assert 'GRANT' not in statements[0] and 'ops_snapshots' not in statements[0]
