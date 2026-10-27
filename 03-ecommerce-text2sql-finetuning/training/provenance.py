"""Content identities for native adapters/checkpoints; no model imports."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
from config import ROOT, resolve_path


def file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as source:
        for chunk in iter(lambda: source.read(4 * 1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def files_identity(paths, root):
    root = Path(root).resolve()
    result = {}
    for file in sorted(set(Path(p).resolve() for p in paths)):
        result[str(file.relative_to(root))] = file_sha256(file)
    return result


def base_identity(cfg):
    path = resolve_path(cfg['model']['path'])
    files = [p for p in path.iterdir() if p.is_file() and
             (p.suffix in {'.json', '.safetensors', '.bin', '.model'} or p.name in {'merges.txt', 'vocab.txt'})]
    if not files or not any(p.suffix in {'.safetensors', '.bin'} for p in files):
        raise ValueError('local Base identity requires actual model weight files')
    # Hash weights once per loaded process, never per step/checkpoint. This
    # costs one sequential disk read but detects an in-place Base replacement.
    return {'revision': cfg['model'].get('revision'), 'files': files_identity(files, path)}


def training_identity(cfg, stage, consumer_root=None):
    paths = list((ROOT / 'training').glob('*.py'))
    paths += [ROOT / 'training/schema_customer_self.txt']
    for folder in ('sandbox/runner', 'sandbox/sql', 'sandbox/fixtures', 'sandbox/consumer01/backend/src'):
        paths.extend(p for p in (ROOT / folder).rglob('*') if p.suffix in {'.py', '.sql'})
    paths.extend((ROOT/'sandbox/vendor/sqlglot30').rglob('*.py'))
    code = files_identity(paths, ROOT)
    names = ('train', 'dev', 'manifest') if stage == 'sft' else ('train', 'manifest')
    data = {name: file_sha256(resolve_path(cfg['data'][name])) for name in names}
    optimization = dict(cfg['sft' if stage == 'sft' else 'grpo'])
    if stage == 'grpo':
        optimization = {k:v for k,v in optimization.items() if k not in {'max_steps','save_every','resume_from'}}
    consumer_source = None
    if stage == 'grpo':
        consumer = resolve_path(consumer_root or cfg['reward']['consumer01_root'])/'backend/src'
        consumer_source = files_identity(consumer.rglob('*.py'), consumer)
    return {'stage': stage, 'source': code, 'data': data, 'adapter': cfg['adapter'],
            'optimization': optimization, 'reward': cfg.get('reward') if stage == 'grpo' else None,
            'consumer_source':consumer_source}


def validate_checkpoint_metadata(state, expected_identity=None):
    required = {'format', 'step', 'adapter', 'optimizer', 'scheduler', 'rng_python',
                'rng_torch', 'rng_cuda', 'extra', 'identity'}
    if not isinstance(state, dict) or not required <= state.keys():
        raise ValueError('incomplete/legacy checkpoint; explicit conversion is required')
    if state['format'] != 'native-training-v1' or type(state['step']) is not int or state['step'] < 0:
        raise ValueError('unsupported checkpoint format or step')
    if not isinstance(state['extra'], dict):
        raise ValueError('checkpoint extra must be an object')
    if expected_identity is not None and state['identity'] != expected_identity:
        raise ValueError('checkpoint Base/source/schema/data identity changed')


def validate_adapter_metadata(metadata, config, expected_base=None):
    if metadata.get('format') != 'native-lora-v1':
        raise ValueError('expected native-lora-v1 adapter; explicit conversion required')
    for key in ('rank', 'alpha', 'dropout', 'target_modules'):
        if config[key] != metadata.get(key):
            raise ValueError('adapter config mismatch: ' + key)
    if expected_base is not None and metadata.get('base_identity') != expected_base:
        raise ValueError('adapter Base identity differs or is missing; explicit conversion required')


def evaluation_identity(rows, consumer_root=None):
    paths = [ROOT/'training/scoring.py', ROOT/'training/eval_business.py',
             ROOT/'training/common.py', ROOT/'training/schema_customer_self.txt',
             ROOT/'sandbox/runner/run.py', ROOT/'sandbox/runner/validator.py',
             ROOT/'sandbox/sql/00_roles.sql', ROOT/'sandbox/sql/01_schema.sql',
             ROOT/'sandbox/fixtures/fx_a.sql', ROOT/'training/provenance.py',ROOT/'training/oracle_errors.py']
    paths.extend((ROOT/'sandbox/vendor/sqlglot30').rglob('*.py'))
    consumer = resolve_path(consumer_root or 'sandbox/consumer01')/'backend/src'
    encoded = json.dumps(rows, sort_keys=True, ensure_ascii=False, separators=(',', ':'))
    return {'rows_sha256': hashlib.sha256(encoded.encode()).hexdigest(),
            'oracle_source': files_identity(paths, ROOT), 'fixture':'fx_a',
            'consumer_source':files_identity(consumer.rglob('*.py'),consumer),
            'protocol':'strict-raw-json-fixed-denominators-v2'}
