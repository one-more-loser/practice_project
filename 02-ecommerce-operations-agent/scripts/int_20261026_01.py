"""INT-20261026-01 only. Offline check by default; real commands need explicit stage gate."""
import argparse
import hashlib
import json
import os
import socket
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.request import urlopen

from sqlalchemy.engine import make_url

RUN = 'INT-20261026-01'
DATABASE = 'ops02_int_20261026_01'
ROOT = Path(__file__).resolve().parents[1]
PRIVATE = ROOT / '.runtime' / RUN
CONFIG = PRIVATE / 'config-private.json'
P1 = ROOT.parent / '01-ecommerce-customer-service-agent'
CLOCK_PATH = P1 / '.runtime' / RUN / 'clock.json'
HANDOFF = P1 / '.runtime' / RUN / 'ops02-handoff'
READY = P1 / 'docs/07-iteration/ITER-20261025-01/review-output' / ('integration-' + RUN) / 'resources-ready.json'
EPOCH = datetime(2026, 10, 26, 4, tzinfo=timezone.utc)
NODE = Path('C:/Users/27997/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe')
CS_SOURCE = '1a3463351c982f7192d1df9d79ace6cb579405847c6e313d84769e04d44acf4b'
OPS_INSTANCE = 'f6e3a21c-9643-42a1-b1a9-d8af05442ca3'
ACCOUNT_NAMES = ('int_admin_a', 'int_ops_a', 'int_viewer_a', 'int_revocable_a', 'int_admin_b')
GATE = P1 / 'docs/07-iteration/ITER-20261025-01' / ('integration-' + RUN) / 'coordinator-gate-review/stage-gate.json'


class Refused(Exception):
    """Messages are constant reason codes; never interpolate secrets or driver errors."""


def read_json(path):
    try:
        path = Path(path)
        if path.is_symlink() or path.stat().st_size > 1024 * 1024:
            raise Refused('config_invalid')
        return json.loads(path.read_text(encoding='utf-8-sig'))
    except FileNotFoundError:
        raise Refused('config_missing') from None
    except (OSError, ValueError, TypeError):
        raise Refused('config_invalid') from None


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def validate_config(value):
    fixed = {'run_id': RUN, 'api_host': '127.0.0.1', 'api_port': 18120, 'frontend_port': 18174,
             'browser_origin': 'http://127.0.0.1:18173', 'identity_base_url': 'http://127.0.0.1:18080',
             'trade_base_url': 'http://127.0.0.1:18080', 'models': 'disabled'}
    if (not isinstance(value, dict) or set(value) != set(fixed) | {
            'clock_file', 'service_stores', 'service_token', 'database'} or
            any(type(value.get(k)) is not type(v) or value[k] != v for k, v in fixed.items())):
        raise Refused('config_scope_mismatch')
    try:
        if Path(value['clock_file']).resolve() != CLOCK_PATH.resolve():
            raise Refused('clock_path_mismatch')
        stores = value['service_stores']
        if (not isinstance(stores, list) or not stores or
                any(not isinstance(s, str) or not s.strip() or len(s) > 80 for s in stores) or
                len(stores) != len(set(stores)) or not isinstance(value['service_token'], str) or
                not value['service_token'].strip()):
            raise Refused('service_scope_invalid')
        roles = value['database']
        if not isinstance(roles, dict) or set(roles) != {'migration', 'runtime'}:
            raise Refused('database_roles_missing')
        for role in roles.values():
            if not isinstance(role, dict) or set(role) != {'url', 'identity'}:
                raise Refused('database_config_invalid')
            url, identity = make_url(role['url']), role['identity']
            if (url.drivername != 'postgresql+psycopg' or url.database != DATABASE or
                    url.host != '192.168.10.100' or url.port != 55432 or not url.username or
                    not url.password or url.query or not isinstance(identity, dict) or
                    set(identity) != {'run_id', 'host', 'port', 'database', 'current_user', 'instance_id'}):
                raise Refused('database_target_mismatch')
            expected = {'run_id': RUN, 'host': url.host, 'port': url.port, 'database': DATABASE,
                        'current_user': url.username}
            if any(type(identity.get(k)) is not type(v) or identity[k] != v for k, v in expected.items()):
                raise Refused('database_identity_mismatch')
            uuid.UUID(identity['instance_id'])
        if (roles['migration']['identity']['instance_id'] != roles['runtime']['identity']['instance_id'] or
                roles['migration']['identity']['current_user'] == roles['runtime']['identity']['current_user']):
            raise Refused('database_role_or_instance_mismatch')
    except Refused:
        raise
    except Exception:
        raise Refused('config_invalid') from None
    return value


def reject_inherited_environment(environment):
    # Inspect names, never values of model credentials. Worker environments are built from an OS allowlist.
    if any(key.startswith(('CS_', 'OPS_', 'VITE_')) or key in ('NODE_OPTIONS', 'PYTHONPATH') for key in environment):
        raise Refused('inherited_business_or_model_environment')


def clean_environment():
    allowed = ('SystemRoot', 'WINDIR', 'COMSPEC', 'PATH', 'PATHEXT', 'TEMP', 'TMP',
               'USERPROFILE', 'LOCALAPPDATA', 'APPDATA', 'PROCESSOR_ARCHITECTURE')
    return {**{key: os.environ[key] for key in allowed if key in os.environ},
            'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONUTF8': '1', 'PYTHON_DOTENV_DISABLED': '1'}


def producer_run(value, *, required=True):
    if not isinstance(value, dict) or (required and 'integration_run_id' not in value):
        raise Refused('producer_run_missing')
    for field in ('integration_run_id', 'run_id'):
        if field in value and value[field] != RUN:
            raise Refused('producer_run_mismatch')


def normalize_resources(ready, effective):
    try:
        for item in (ready, ready['resources'], effective):
            producer_run(item)
        if (ready['status'] != 'ready' or ready['container']['host'] != '192.168.10.100' or
                ready['container']['port'] != 55432 or ready['resources']['databases']['ops'] != DATABASE or
                ready['resources']['roles']['ops_owner'] != 'opsint_20261026_owner' or
                ready['resources']['roles']['ops_app'] != 'opsint_20261026_app' or
                ready['resources']['instance_ids']['ops'] != OPS_INSTANCE or
                effective['source_digest'] != CS_SOURCE or effective['host'] != '127.0.0.1' or
                effective['port'] != 18080 or effective['origin'] != 'http://127.0.0.1:18173' or
                effective['ops_base_url'] != 'http://127.0.0.1:18120' or effective['models'] != 'disabled' or
                effective['workers'] != {'agent': False, 'expiration': False, 'activity_delivery': False} or
                effective['stores'] != ['int_store_a'] or
                Path(effective['clock_file']).resolve() != CLOCK_PATH.resolve() or
                Path(ready['clock']['file']).resolve() != CLOCK_PATH.resolve()):
            raise Refused('producer_resources_mismatch')
        return {'run_id': ready['integration_run_id'], 'status': ready['status'],
            'ops02': {'host': ready['container']['host'], 'port': ready['container']['port'],
                'database': ready['resources']['databases']['ops'], 'instance_id': ready['resources']['instance_ids']['ops'],
                'migration_user': ready['resources']['roles']['ops_owner'], 'runtime_user': ready['resources']['roles']['ops_app']},
            'service_stores': effective['stores'], 'clock_file': str(CLOCK_PATH)}
    except Refused:
        raise
    except Exception:
        raise Refused('producer_shape_invalid') from None


def load_resources():
    ready = read_json(READY)
    try:
        public = READY.parent.resolve()
        effective_path = Path(ready['effective_config'])
        index_path = Path(ready['evidence_index']['path'])
        if (effective_path.is_symlink() or index_path.is_symlink() or
                not effective_path.resolve().is_relative_to(public) or
                index_path.resolve() != (public / 'evidence-index.json')):
            raise Refused('producer_public_path_invalid')
        index_sha = hashlib.sha256(index_path.read_bytes()).hexdigest()
        if index_sha != ready['evidence_index']['sha256']:
            raise Refused('producer_index_conflict')
        index = read_json(index_path)
        producer_run(index)
        key = effective_path.resolve().relative_to(public).as_posix()
        if hashlib.sha256(effective_path.read_bytes()).hexdigest() != index['files'][key]:
            raise Refused('producer_effective_digest_conflict')
        gate = read_json(GATE)
        if (gate.get('run_id') != RUN or gate.get('allowed_stage') != '02_adapter_initialize_start_and_basic_smoke' or
                gate.get('resources_ready_sha256') != hashlib.sha256(READY.read_bytes()).hexdigest() or
                gate.get('producer_source_digest') != CS_SOURCE or gate.get('ops_instance_id') != OPS_INSTANCE or
                gate.get('no_activity_or_faults_or_clock_advance') is not True):
            raise Refused('coordinator_stage_gate_mismatch')
        effective = read_json(effective_path)
        return ready, effective, normalize_resources(ready, effective)
    except Refused:
        raise
    except Exception:
        raise Refused('producer_public_packet_invalid') from None


def verify_producer_health(ready, effective, health):
    normalize_resources(ready, effective)
    try:
        producer_run(health)
        if (health['service'] != 'cs01' or health['ready'] is not True or
                health['loaded_source_digest'] != CS_SOURCE or health['config_digest'] != effective['config_digest'] or
                health['models'] != 'disabled' or health['model_capabilities'] != [] or
                health['workers'] != effective['workers'] or health['database'] != 'cs_agent_int_20261026_01' or
                health['database_identity'] != {'host': '192.168.10.100', 'port': 55432, 'server_port': 5432,
                    'instance_id': 'e1a0d2b3-ba15-4461-b383-099ad8183b7b', 'app_role': 'csint_20261026_app'}):
            raise Refused('producer_live_health_mismatch')
        return True
    except Refused:
        raise
    except Exception:
        raise Refused('producer_live_health_invalid') from None


def adapt_producer(ready, effective, files, clock):
    normalized = normalize_resources(ready, effective)
    expected_names = {'migration-private.json', 'migration-identity.json', 'runtime-private.json',
                      'runtime-identity.json', 'service-private.json', 'accounts-private.json'}
    if not isinstance(files, dict) or set(files) != expected_names:
        raise Refused('producer_file_group_invalid')
    try:
        for name in ('migration-private.json', 'runtime-private.json', 'service-private.json'):
            producer_run(files[name])
        producer_run(clock)
        RunClock.parse(clock)
        service = files['service-private.json']
        if (service['stores'] != normalized['service_stores'] or
                Path(service['clock_file']).resolve() != CLOCK_PATH.resolve()):
            raise Refused('producer_service_binding_mismatch')
        roles = {}
        for role, public_role in (('migration', 'migration_user'), ('runtime', 'runtime_user')):
            raw = dict(files[role + '-identity.json'])
            producer_run(raw, required=False)
            raw.pop('integration_run_id', None)
            raw.pop('run_id', None)
            if (raw['current_user'] != normalized['ops02'][public_role] or raw['instance_id'] != OPS_INSTANCE):
                raise Refused('producer_role_or_instance_mismatch')
            roles[role] = {'url': files[role + '-private.json']['url'],
                           'identity': {**raw, 'run_id': files[role + '-private.json']['integration_run_id']}}
        config = {'run_id': ready['integration_run_id'], 'api_host': '127.0.0.1', 'api_port': 18120,
            'frontend_port': 18174, 'browser_origin': effective['origin'], 'identity_base_url': 'http://127.0.0.1:18080',
            'trade_base_url': 'http://127.0.0.1:18080', 'clock_file': str(CLOCK_PATH), 'models': effective['models'],
            'service_stores': service['stores'], 'service_token': service['service_token'], 'database': roles}
        validate_config(config)
        accounts = files['accounts-private.json']
        if (not isinstance(accounts, dict) or set(accounts) != set(ACCOUNT_NAMES) or
                any(not isinstance(value, str) or not value for value in accounts.values())):
            raise Refused('producer_accounts_invalid')
        return config, accounts
    except Refused:
        raise
    except Exception:
        raise Refused('producer_private_shape_invalid') from None


def require_real_stage(explicit_run, resources, config):
    if explicit_run != RUN:
        raise Refused('stage_not_authorized')
    if not isinstance(resources, dict) or resources.get('run_id') != RUN or resources.get('status') != 'ready':
        raise Refused('resources_not_ready')
    record = resources.get('ops02', {})
    runtime = config['database']['runtime']['identity']
    expected = {k: runtime[k] for k in ('host', 'port', 'database', 'instance_id')}
    expected.update(migration_user=config['database']['migration']['identity']['current_user'],
                    runtime_user=runtime['current_user'])
    if not isinstance(record, dict) or any(record.get(k) != v for k, v in expected.items()):
        raise Refused('resources_identity_mismatch')
    if (resources.get('service_stores') != config['service_stores'] or
            resources.get('clock_file') != str(CLOCK_PATH)):
        raise Refused('resources_store_or_clock_mismatch')
    return True


class RunClock:
    def __init__(self, path, highwater, *, producer=False):
        self.path, self.highwater, self.lock = Path(path), Path(highwater), threading.Lock()
        self.producer = producer

    @staticmethod
    def parse(value):
        try:
            if not isinstance(value, dict):
                raise ValueError()
            producer_run(value, required=False)
            now = datetime.fromisoformat(value['now'])
            if now.tzinfo is None or now.utcoffset() is None or now < EPOCH:
                raise ValueError()
            return now
        except (ValueError, KeyError, TypeError):
            raise Refused('clock_invalid') from None

    def now(self):
        with self.lock:
            raw = read_json(self.path)
            if self.producer:
                producer_run(raw)
            value = self.parse(raw)
            old = self.parse(read_json(self.highwater)) if self.highwater.exists() else EPOCH
            if value < old:
                raise Refused('clock_rollback')
            if not self.highwater.exists() or value > old:
                atomic_json(self.highwater, {'run_id': RUN, 'now': value.isoformat()})
            return value


def source_manifest():
    excluded = {'.venv', 'node_modules', '__pycache__', '.pytest_cache', '.ruff_cache', 'dist',
                '.runtime', '.git', '.agents', 'playwright-report', 'test-results'}
    files = {}
    for folder in ('backend', 'frontend', 'scripts', 'contracts'):
        for path in (ROOT / folder).rglob('*'):
            relative = path.relative_to(ROOT)
            if (not path.is_file() or set(relative.parts) & excluded or path.name.startswith('.env') or
                    path.suffix in ('.pyc', '.log', '.db', '.png', '.jpg')):
                continue
            files[relative.as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    raw = json.dumps(files, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
    return {'algorithm': 'sha256 canonical compact UTF8 path-to-SHA JSON',
            'digest': hashlib.sha256(raw).hexdigest(), 'files': files}


def ensure_free(port):
    try:
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', port))
    except OSError:
        raise Refused('port_unavailable') from None


def role_identity(config, role):
    identity = config['database'][role]['identity']
    target = PRIVATE / (role + '-identity.json')
    atomic_json(target, identity)
    os.environ['OPS_DATABASE_IDENTITY_FILE'] = str(target)
    os.environ['OPS_QUERY_AUDIT_DIR'] = str(PRIVATE / 'query-audit')
    return identity


def initialize(config):
    role_identity(config, 'migration')
    from ops_agent.db import Database
    database = Database(config['database']['migration']['url'], 'integration')
    try:
        database.initialize()
        harden_ledger(config, database=database)
    finally:
        database.close()


def harden_ledger(config, *, database=None):
    """New-run schema ledger is read-only to app, without changing business-table grants."""
    from ops_agent.db import Database
    from sqlalchemy import text
    owned = database is None
    if owned:
        role_identity(config, 'migration')
        database = Database(config['database']['migration']['url'], 'integration')
    try:
        role = database.engine.dialect.identifier_preparer.quote(config['database']['runtime']['identity']['current_user'])
        with database.transaction() as connection:
            connection.execute(text('REVOKE INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER '
                'ON TABLE public.ops_schema_versions FROM ' + role))
    finally:
        if owned:
            database.close()


def build_api(config, loaded, *, factory=None, clock=None):
    role_identity(config, 'runtime')
    clock = clock or RunClock(CLOCK_PATH, PRIVATE / 'clock-highwater.json', producer=True).now
    clock()
    if factory is None:
        from ops_agent.app import create_app
        factory = create_app
    app = factory(database_url=config['database']['runtime']['url'], mode='integration',
        origin=config['browser_origin'], clock=clock, identity_base_url=config['identity_base_url'],
        trade_base_url=config['trade_base_url'], service_token=config['service_token'],
        service_stores=tuple(config['service_stores']))

    @app.get('/health/integration')
    def integration_health():
        return {'run_id': RUN, 'service': 'ops02', 'models': 'disabled', 'pid': os.getpid(),
                'api_host': '127.0.0.1', 'api_port': 18120, 'origin': config['browser_origin'],
                'loaded_source_digest': loaded['digest'], 'source_algorithm': loaded['algorithm'],
                'entry_sha256': loaded['files']['scripts/int_20261026_01.py'],
                'database': app.state.database.identity_record, 'clock': clock().isoformat()}
    return app


def process_info(pid):
    if type(pid) is not int or pid < 1 or os.name != 'nt':
        raise Refused('process_inspection_unavailable')
    command = ("[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false); "
               f"$p=Get-CimInstance Win32_Process -Filter 'ProcessId = {pid}'; "
               "if($p){[pscustomobject]@{pid=$p.ProcessId;parent_pid=$p.ParentProcessId;created=$p.CreationDate.ToUniversalTime().ToString('o');"
               "command_line=$p.CommandLine;executable=$p.ExecutablePath}|ConvertTo-Json -Compress}")
    result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', command],
        env=clean_environment(), capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=5)
    if result.returncode:
        raise Refused('process_inspection_failed')
    return json.loads(result.stdout) if result.stdout.strip() else None


def same_process(recorded, observed):
    return isinstance(observed, dict) and all(recorded.get(k) == observed.get(k) for k in ('pid', 'created', 'command_line', 'executable'))


def runtime_identity(launcher, reported_pid, *, inspect=None):
    inspect = inspect or process_info
    if type(reported_pid) is not int or reported_pid < 1:
        raise Refused('runtime_pid_invalid')
    observed = inspect(reported_pid)
    if reported_pid == launcher['pid']:
        if not same_process(launcher, observed):
            raise Refused('runtime_identity_mismatch')
        return dict(launcher)
    if (not isinstance(observed, dict) or observed.get('parent_pid') != launcher['pid'] or
            launcher['service'] != 'api' or str(Path(__file__).resolve()).casefold() not in observed.get('command_line', '').casefold() or
            RUN not in observed.get('command_line', '') or
            Path(observed.get('executable', '')).resolve() not in {
                Path(sys.executable).resolve(), Path(getattr(sys, '_base_executable', sys.executable)).resolve()}):
        raise Refused('runtime_parent_or_command_mismatch')
    return {**observed, 'service': launcher['service'], 'port': launcher['port'], 'launcher_identity': launcher}


def managed_processes(record):
    rows = list(record['children'])
    rows += [row['launcher_identity'] for row in record['children'] if 'launcher_identity' in row]
    return list({row['pid']: row for row in rows}.values())


def stop_owned(child):
    """Pin the Windows process handle and creation time, then stop only that object."""
    import ctypes
    from ctypes import wintypes
    if not same_process(child, process_info(child['pid'])):
        raise Refused('process_identity_mismatch_no_signal_sent')
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x1000 | 0x0001 | 0x00100000, False, child['pid'])
    if not handle:
        raise Refused('owned_process_handle_unavailable')
    try:
        times = [wintypes.FILETIME() for _ in range(4)]
        if not kernel.GetProcessTimes(handle, *(ctypes.byref(item) for item in times)):
            raise Refused('owned_process_time_unavailable')
        ticks = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
        created = datetime(1601, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=ticks // 10)
        if created != datetime.fromisoformat(child['created']):
            raise Refused('owned_process_generation_changed')
        if not kernel.TerminateProcess(handle, 0) or kernel.WaitForSingleObject(handle, 5000) != 0:
            raise Refused('owned_process_exit_not_confirmed')
    finally:
        kernel.CloseHandle(handle)


def listener_identity(port):
    command = (f"@(Get-NetTCPConnection -State Listen -LocalPort {port} -ErrorAction SilentlyContinue | "
               "Select-Object LocalAddress,LocalPort,OwningProcess) | ConvertTo-Json -Compress")
    value = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', command],
        env=clean_environment(), capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=5)
    if value.returncode:
        raise Refused('listener_inspection_failed')
    records = json.loads(value.stdout) if value.stdout.strip() else []
    return records if isinstance(records, list) else [records]


def start(config, loaded):
    ensure_free(18120)
    ensure_free(18174)
    if not NODE.is_file() or not (ROOT / '.venv/Scripts/python.exe').is_file():
        raise Refused('runtime_dependency_missing')
    if (PRIVATE / 'processes.json').exists():
        previous = read_json(PRIVATE / 'processes.json')
        if (previous.get('run_id') != RUN or previous.get('status') != 'owned_process_exit_confirmed' or
                any(same_process(child, process_info(child['pid'])) for child in previous['children'])):
            raise Refused('process_record_exists_inspect_before_restart')
        (PRIVATE / 'processes.json').rename(PRIVATE / ('processes-stopped-' + uuid.uuid4().hex + '.json'))
    front = {key: config[key] for key in ('run_id', 'api_host', 'api_port', 'frontend_port', 'browser_origin', 'models')}
    front['loaded_source_digest'] = loaded['digest']
    atomic_json(PRIVATE / 'frontend-public.json', front)
    atomic_json(PRIVATE / 'source-freeze.json', loaded)
    children = []
    atomic_json(PRIVATE / 'processes.json', {'run_id': RUN, 'source_digest': loaded['digest'], 'status': 'starting', 'children': children})
    for service, port, args in (
        ('api', 18120, [sys.executable, '-B', str(Path(__file__).resolve()), 'serve-api']),
        ('frontend', 18174, [str(NODE), str(ROOT / 'scripts/int_20261026_01_frontend.mjs')]),
    ):
        common = ['--run', RUN, '--source-digest', loaded['digest'], '--enter-real-stage', RUN]
        with (PRIVATE / (service + '.log')).open('ab') as output:
            child = subprocess.Popen(args + common, cwd=ROOT, env=clean_environment(), stdout=output,
                stderr=output, creationflags=subprocess.CREATE_NO_WINDOW)
        children.append({'pid': child.pid, 'service': service, 'port': port, 'status': 'identity_pending'})
        atomic_json(PRIVATE / 'processes.json', {'run_id': RUN, 'source_digest': loaded['digest'], 'status': 'starting', 'children': children})
        observed = process_info(child.pid)
        if observed is None:
            raise Refused('child_exited_before_registration')
        children[-1] = {**observed, 'service': service, 'port': port}
        atomic_json(PRIVATE / 'processes.json', {'run_id': RUN, 'source_digest': loaded['digest'], 'status': 'starting', 'children': children})
    began = time.monotonic()
    for _ in range(20):
        try:
            with urlopen('http://127.0.0.1:18120/health/integration', timeout=1) as response:
                health = json.load(response)
            with urlopen('http://127.0.0.1:18174/operations/__int_health', timeout=1) as response:
                frontend_health = json.load(response)
            if (health.get('run_id') == RUN and health.get('loaded_source_digest') == loaded['digest'] and
                    frontend_health.get('run_id') == RUN and frontend_health.get('loaded_source_digest') == loaded['digest']):
                children = [runtime_identity(children[0], health.get('pid')),
                            runtime_identity(children[1], frontend_health.get('pid'))]
                observed = {str(port): listener_identity(port) for port in (18120, 18174)}
                if any(not observed[str(child['port'])] or any(
                        row['LocalAddress'] != '127.0.0.1' or row['OwningProcess'] != child['pid']
                        for row in observed[str(child['port'])]) for child in children):
                    raise Refused('listener_owner_mismatch')
                atomic_json(PRIVATE / 'processes.json', {'run_id': RUN, 'source_digest': loaded['digest'],
                    'status': 'running', 'children': children, 'health': health, 'frontend_health': frontend_health,
                    'health_seconds': time.monotonic() - began, 'listeners': observed})
                return
        except Refused:
            raise
        except Exception:
            pass
        time.sleep(.5)
    # Preserve owned process records/logs for inspection; never kill by port or reset a DB.
    raise Refused('startup_not_confirmed_owned_records_preserved')


def main():
    parser = argparse.ArgumentParser(description='INT-20261026-01 guarded run entry; no default database')
    parser.add_argument('command', choices=('check', 'adopt-handoff', 'adopt-producer', 'init-db', 'harden-ledger', 'start', 'serve-api', 'status', 'stop'))
    parser.add_argument('--run', required=True)
    parser.add_argument('--api-port', type=int, default=18120)
    parser.add_argument('--frontend-port', type=int, default=18174)
    parser.add_argument('--source-digest')
    parser.add_argument('--enter-real-stage')
    parser.add_argument('--handoff-file')
    args = parser.parse_args()
    try:
        if args.run != RUN or args.api_port != 18120 or args.frontend_port != 18174:
            raise Refused('command_scope_mismatch')
        reject_inherited_environment(os.environ)
        if args.command == 'adopt-producer':
            if args.enter_real_stage != RUN:
                raise Refused('stage_not_authorized')
            ready, effective, normalized = load_resources()
            files = {name: read_json(HANDOFF / name) for name in (
                'migration-private.json', 'migration-identity.json', 'runtime-private.json',
                'runtime-identity.json', 'service-private.json', 'accounts-private.json')}
            config, accounts = adapt_producer(ready, effective, files, read_json(CLOCK_PATH))
            require_real_stage(args.enter_real_stage, normalized, config)
            with urlopen('http://127.0.0.1:18080/health/integration', timeout=3) as response:
                verify_producer_health(ready, effective, json.load(response))
            if CONFIG.exists() or (PRIVATE / 'accounts-private.json').exists():
                raise Refused('private_config_exists_do_not_overwrite')
            atomic_json(CONFIG, config)
            atomic_json(PRIVATE / 'accounts-private.json', accounts)
            print(json.dumps({'run_id': RUN, 'status': 'producer_adapted', 'database_accessed': False}))
            return 0
        if args.command in ('status', 'stop'):
            record = read_json(PRIVATE / 'processes.json')
            if record.get('run_id') != RUN:
                raise Refused('process_record_foreign_run')
            for child in managed_processes(record):
                current = process_info(child['pid'])
                if current is not None and not same_process(child, current):
                    raise Refused('process_identity_mismatch_no_signal_sent')
            if args.command == 'stop':
                for child in managed_processes(record):
                    if process_info(child['pid']) is not None:
                        stop_owned(child)
                record['status'] = 'owned_process_exit_confirmed'
                atomic_json(PRIVATE / 'processes.json', record)
            else:
                print(json.dumps(record, ensure_ascii=False))
            return 0
        if args.command == 'adopt-handoff':
            source = Path(args.handoff_file or '')
            if source.is_symlink() or source.resolve().parent != HANDOFF.resolve() or source.suffix != '.json':
                raise Refused('handoff_path_not_authorized')
            config = validate_config(read_json(source))
            if CONFIG.exists():
                raise Refused('private_config_exists_do_not_overwrite')
            atomic_json(CONFIG, config)
            print(json.dumps({'run_id': RUN, 'status': 'copied_config_validated_offline', 'database_accessed': False}))
            return 0
        config = validate_config(read_json(CONFIG))
        if args.command == 'check':
            print(json.dumps({'run_id': RUN, 'status': 'config_validated_offline', 'database_accessed': False}))
            return 0
        if args.enter_real_stage != RUN:
            raise Refused('stage_not_authorized')
        ready, effective, normalized = load_resources()
        require_real_stage(args.enter_real_stage, normalized, config)
        with urlopen('http://127.0.0.1:18080/health/integration', timeout=3) as response:
            verify_producer_health(ready, effective, json.load(response))
        if Path(sys.executable).resolve() != (ROOT / '.venv/Scripts/python.exe').resolve():
            raise Refused('project_python_required')
        loaded = source_manifest()
        if args.source_digest != loaded['digest']:
            raise Refused('source_digest_mismatch')
        sys.path.insert(0, str(ROOT / 'backend/src'))
        if args.command == 'init-db':
            RunClock(CLOCK_PATH, PRIVATE / 'clock-highwater.json', producer=True).now()
            initialize(config)
        elif args.command == 'harden-ledger':
            harden_ledger(config)
        elif args.command == 'start':
            start(config, loaded)
        elif args.command == 'serve-api':
            ensure_free(18120)
            app = build_api(config, loaded)
            import uvicorn
            uvicorn.run(app, host='127.0.0.1', port=18120, access_log=False, log_level='critical')
        print(json.dumps({'run_id': RUN, 'command': args.command, 'status': 'completed'}))
        return 0
    except Refused as error:
        print(json.dumps({'run_id': RUN, 'status': 'refused', 'reason': str(error)}))
        return 2
    except Exception:
        # Driver/HTTP/JSON exceptions may contain DSNs; never echo or chain them in CLI logs.
        print(json.dumps({'run_id': RUN, 'status': 'failed', 'reason': 'operation_failed_private_details_suppressed'}))
        return 3


if __name__ == '__main__':
    raise SystemExit(main())
