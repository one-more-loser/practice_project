"""Sequential, resumable process-isolated Base -> SFT -> GRPO pipeline."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'training'))
STAGES = ('base_eval', 'base_business', 'sft', 'sft_eval', 'sft_business', 'grpo',
          'grpo_eval', 'grpo_business', 'compare')
STAGE_GROUPS = {'base': ('base_eval', 'base_business'), 'sft': ('sft',),
                'sft_eval': ('sft_eval', 'sft_business'), 'grpo': ('grpo',),
                'grpo_eval': ('grpo_eval', 'grpo_business', 'compare'), 'compare': ('compare',)}


def select_stages(requested):
    if not requested:
        return set(STAGES)
    result = set()
    for item in requested:
        for name in item.split(','):
            if name not in STAGE_GROUPS and name not in STAGES:
                raise ValueError(f'Unknown stage: {name}')
            result.update(STAGE_GROUPS.get(name, (name,)))
    return result


def digest_path(path):
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)
    files = sorted(p for p in path.rglob('*') if p.is_file() and '__pycache__' not in p.parts) if path.is_dir() else [path]
    if not files:
        raise ValueError(f'Empty artifact/input directory: {path}')
    h = hashlib.sha256()
    for file in files:
        h.update(str(file.relative_to(path) if path.is_dir() else file.name).encode())
        with file.open('rb') as source:
            for chunk in iter(lambda: source.read(4 * 1024 * 1024), b''):
                h.update(chunk)
    return h.hexdigest()


def json_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def consumer_root(cfg):
    value = cfg.get('business', {}).get('consumer01_root') or cfg.get('reward', {}).get('consumer01_root')
    path = Path(value) if value else ROOT.parent / '01-ecommerce-customer-service-agent'
    return path if path.is_absolute() else ROOT / path


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    tmp.replace(path)


def build_plan(cfg, config_path, output_dir, python=None):
    """No model import, file reads, writes, or downloads."""
    python = python or sys.executable
    out = Path(output_dir)
    config_path = str(config_path)
    test = str(cfg['data']['test'])
    consumer = str(consumer_root(cfg))
    sft_adapter, grpo_adapter = out / 'sft' / 'adapter', out / 'grpo' / 'adapter'
    result = []
    for stage in STAGES:
        artifacts = []
        if stage.endswith('_eval'):
            kind = stage.removesuffix('_eval')
            cmd = [python, '-m', 'training.eval_sft', '--config', config_path,
                   '--test', test, '--output', str(out / f'{kind}-eval.json')]
            if kind != 'base':
                cmd += ['--adapter', str(sft_adapter if kind == 'sft' else grpo_adapter)]
            artifacts = [out / f'{kind}-eval.json']
        elif stage.endswith('_business'):
            kind = stage.removesuffix('_business')
            cmd = [python, '-m', 'training.eval_business', '--test', test,
                   '--predictions', str(out / f'{kind}-eval.json'),
                   '--output', str(out / f'{kind}-business.json'), '--consumer01-root', consumer]
            artifacts = [out / f'{kind}-business.json']
        elif stage in ('sft', 'grpo'):
            cmd = [python, '-m', f'training.train_{stage}', '--config', config_path,
                   '--output', str(out / stage)]
            if stage == 'grpo':
                cmd += ['--sft-adapter', str(sft_adapter)]
            resume = cfg.get('pipeline', {}).get('resume', {}).get(stage)
            if resume:
                cmd += ['--resume', str(resume)]
            artifacts = [sft_adapter if stage == 'sft' else grpo_adapter]
        else:
            cmd = [python, '-m', 'training.compare_runs', '--test', test, '--output', str(out / 'comparison.json')]
            for kind in ('base', 'sft', 'grpo'):
                cmd += [f'--{kind}', str(out / f'{kind}-business.json')]
            artifacts = [out / 'comparison.json', out / 'comparison.md']
        result.append({'stage': stage, 'command': cmd, 'artifacts': list(map(str, artifacts)),
                       'run_dir': str(out / stage if stage in ('sft', 'grpo') else out),
                       'adapter_path': str(sft_adapter if stage.startswith('sft') else grpo_adapter) if stage.startswith(('sft', 'grpo')) else None})
    return result


def run_process(command, log_path, timeout_seconds):
    """Terminate the entire spawned process group on timeout or interrupt."""
    options = {'start_new_session': True} if os.name != 'nt' else {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP}
    with Path(log_path).open('w', encoding='utf-8') as log:
        proc = subprocess.Popen(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, **options)
        try:
            return proc.wait(timeout=timeout_seconds or None), False
        except (subprocess.TimeoutExpired, KeyboardInterrupt) as exc:
            if os.name == 'nt':
                subprocess.run(['taskkill', '/PID', str(proc.pid), '/T', '/F'], capture_output=True, check=False)
            else:
                os.killpg(proc.pid, signal.SIGTERM)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
                # Parent exit does not imply all grandchildren have exited.
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            proc.wait()
            if isinstance(exc, KeyboardInterrupt):
                raise
            return proc.returncode, True


def execute_plan(plan, selected, state_path, provenance, timeout_seconds, runner=run_process):
    from output_lock import exclusive_output
    with exclusive_output(Path(state_path).parent):
        return _execute_plan(plan, selected, state_path, provenance, timeout_seconds, runner)


def _execute_plan(plan, selected, state_path, provenance, timeout_seconds, runner=run_process):
    state_path = Path(state_path)
    state = json.loads(state_path.read_text(encoding='utf-8')) if state_path.exists() else {'version': 1, 'stages': {}}
    chain = provenance
    # A four-hour budget is shared by the entire invocation, not renewed for
    # each of nine stages. A new manual resume starts a new explicit budget.
    deadline = time.monotonic() + timeout_seconds if timeout_seconds else None
    for item in plan:
        stage = item['stage']
        previous = state['stages'].get(stage, {})
        signature = json_hash({'input': chain, 'command': item['command']})
        try:
            actual = {path: digest_path(path) for path in item['artifacts']}
        except (FileNotFoundError, ValueError):
            actual = None
        valid = (previous.get('status') == 'complete' and previous.get('returncode') == 0
                 and previous.get('signature') == signature and actual is not None
                 and previous.get('artifacts') == actual)
        if stage not in selected:
            if not valid:
                # Only selected stages need dependencies; no later selection means stop.
                if any(entry['stage'] in selected for entry in plan[plan.index(item) + 1:]):
                    raise RuntimeError(f'{stage} dependency is missing/stale; include it in --stages')
                break
        elif not valid:
            log = state_path.parent / f'{stage}.log'
            record = {**item, 'status': 'running', 'signature': signature, 'started_at': time.time(), 'log': str(log)}
            state['stages'][stage] = record
            atomic_json(state_path, state)
            try:
                remaining = max(0.0, deadline - time.monotonic()) if deadline else None
                if remaining is not None and remaining <= 0:
                    raise TimeoutError('pipeline total time budget exhausted before ' + stage)
                rc, timed_out = runner(item['command'], log, remaining)
                record.update(returncode=rc, timed_out=timed_out, finished_at=time.time())
                if rc != 0 or timed_out:
                    raise RuntimeError(f'{stage} failed (rc={rc}, timeout={timed_out}); process timeout does not shut down AutoDL')
                actual = {path: digest_path(path) for path in item['artifacts']}
                record.update(status='complete', artifacts=actual)
                atomic_json(state_path, state)
            except BaseException as exc:
                record.update(status='failed', error=str(exc), finished_at=time.time())
                atomic_json(state_path, state)
                raise
        else:
            print(f'Skip verified stage: {stage}')
        chain = json_hash({'signature': signature, 'artifacts': actual})
    return state


def collect_provenance(cfg):
    paths = [ROOT / 'training', ROOT / 'sandbox' / 'runner', ROOT / 'sandbox' / 'fixtures',
             ROOT / 'sandbox' / 'sql', ROOT / 'sandbox' / 'vendor']
    for key in ('train', 'dev', 'test', 'manifest'):
        value = cfg['data'].get(key)
        if value:
            path = Path(value)
            paths.append(path if path.is_absolute() else ROOT / path)
    model = Path(cfg['model']['path'])
    paths.append(model if model.is_absolute() else ROOT / model)
    consumer = consumer_root(cfg)
    paths.append(consumer / 'backend' / 'src' / 'cs_agent' / 'query')
    hashes = {str(path): digest_path(path) for path in paths}
    return json_hash({'config': cfg, 'sha256': hashes})


def main():
    from config import load_config, resolve_path
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--config', required=True)
    ap.add_argument('--set', action='append', default=[])
    ap.add_argument('--stages', nargs='+', help='Stage names, space or comma separated')
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()
    cfg = load_config(args.config, args.set)
    output = resolve_path(cfg['runtime']['output_dir'])
    try:
        selected = select_stages(args.stages or cfg.get('pipeline', {}).get('stages'))
    except ValueError as exc:
        ap.error(str(exc))
    config_path = output / 'resolved_config.json'
    plan = build_plan(cfg, config_path, output, cfg.get('pipeline', {}).get('python'))
    if args.dry_run:
        print(json.dumps({'resolved_config': cfg, 'selected': sorted(selected), 'plan': plan,
                          'boundary': 'No model load/training/download; stage timeout only terminates process group, not AutoDL instance'}, ensure_ascii=False, indent=2))
        return
    from output_lock import exclusive_output
    # Lock before writing the resolved config or any state, not just children.
    with exclusive_output(output):
        provenance = collect_provenance(cfg)
        atomic_json(config_path, cfg)
        timeout = cfg.get('pipeline', {}).get('timeout_seconds', cfg['runtime'].get('max_seconds', 0))
        _execute_plan(plan, selected, output / 'pipeline-state.json', provenance, timeout)


if __name__ == '__main__':
    main()
