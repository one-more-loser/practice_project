"""JSON configuration and CLI overrides; importing this never loads a model."""
from __future__ import annotations

import copy
import hashlib
import json
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / 'training/configs/rtx_pro_6000_96gb.json'


def resolve_path(path: str | Path) -> Path:
    path = Path(path).expanduser()
    return path.resolve() if path.is_absolute() else (ROOT / path).resolve()


def load_config(path: str | Path = DEFAULT_CONFIG, overrides=None) -> dict:
    source = resolve_path(path)
    cfg = json.loads(source.read_text(encoding='utf-8'))
    if not isinstance(cfg, dict):
        raise ValueError('configuration must be a JSON object')
    cfg = copy.deepcopy(cfg)
    for override in overrides or []:
        if '=' not in override:
            raise ValueError(f'override must be section.key=value: {override}')
        key, raw = override.split('=', 1)
        parts = key.split('.')
        if len(parts) < 2 or any(not p for p in parts):
            raise ValueError(f'invalid override path: {key}')
        node = cfg
        for part in parts[:-1]:
            if part not in node or not isinstance(node[part], dict):
                raise ValueError(f'unknown configuration section: {key}')
            node = node[part]
        if parts[-1] not in node:
            raise ValueError(f'unknown configuration key: {key}')
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            value = raw
        node[parts[-1]] = value
    validate_config(cfg)
    cfg['_config_path'] = str(source)
    cfg['_config_sha256'] = hashlib.sha256(source.read_bytes()).hexdigest()
    return cfg


def validate_config(cfg: dict) -> None:
    def finite_values(value, path='config'):
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError(f'{path} must be finite')
        if isinstance(value, dict):
            for key, item in value.items():
                finite_values(item, path + '.' + key)
        elif isinstance(value, list):
            for item in value:
                finite_values(item, path)
    finite_values(cfg)
    for section in ('model', 'data', 'adapter', 'sft', 'grpo', 'runtime', 'evaluation'):
        if not isinstance(cfg.get(section), dict):
            raise ValueError(f'missing configuration section {section}')
    for section, fields in {
        'adapter': ('rank', 'alpha'),
        'sft': ('batch_size', 'gradient_accumulation_steps', 'learning_rate', 'max_seq_len'),
        'grpo': ('batch_size', 'gradient_accumulation_steps', 'learning_rate', 'max_prompt_tokens', 'max_completion_tokens', 'num_generations'),
        'evaluation': ('batch_size', 'max_prompt_tokens', 'max_new_tokens'),
    }.items():
        for field in fields:
            value = cfg[section].get(field)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                raise ValueError(f'{section}.{field} must be positive')
    if cfg['grpo']['num_generations'] < 2:
        raise ValueError('GRPO requires at least two completions per question')
    for section in ('sft', 'grpo'):
        steps = cfg[section].get('max_steps', -1)
        if type(steps) is not int or steps == 0 or steps < -1:
            raise ValueError(f'{section}.max_steps must be positive or -1')
    for section, keys in {
        'adapter': ('rank',),
        'sft': ('batch_size', 'gradient_accumulation_steps', 'max_seq_len', 'save_every',
                'log_every', 'loss_chunk_tokens', 'length_bucket_size'),
        'grpo': ('batch_size', 'gradient_accumulation_steps', 'num_generations',
                 'max_prompt_tokens', 'max_completion_tokens', 'updates_per_rollout', 'save_every'),
        'evaluation': ('batch_size', 'max_prompt_tokens', 'max_new_tokens'),
    }.items():
        for key in keys:
            if key in cfg[section] and (type(cfg[section][key]) is not int or cfg[section][key] < 1):
                raise ValueError(f'{section}.{key} must be a positive integer')
    if cfg['sft'].get('epochs', 0) <= 0 or not 0 <= cfg['sft'].get('warmup_ratio', 0) <= 1:
        raise ValueError('sft.epochs must be positive and warmup_ratio in [0,1]')
    for section in ('sft', 'grpo'):
        if cfg[section].get('max_grad_norm', 0) <= 0 or cfg[section].get('weight_decay', 0) < 0:
            raise ValueError(f'{section}: invalid max_grad_norm/weight_decay')
    if cfg['runtime'].get('max_seconds') is not None and cfg['runtime'].get('max_seconds', 0) < 0:
        raise ValueError('runtime.max_seconds must be nonnegative or null')
    if cfg['model'].get('torch_dtype') not in ('bfloat16', 'float16', 'float32'):
        raise ValueError('model.torch_dtype must be bfloat16, float16, or float32')
    if cfg['runtime'].get('device') not in ('cuda', 'cpu', 'auto'):
        raise ValueError('runtime.device must be cuda, cpu, or auto')
    if cfg['model'].get('local_files_only') is not True:
        raise ValueError('model.local_files_only must be true; model preparation is a separate manual step')
    fixtures = cfg.get('reward', {}).get('fixtures')
    if not isinstance(fixtures, list) or any(not isinstance(f, str) for f in fixtures) or len(fixtures) < 2 or len(set(fixtures)) != len(fixtures) or not set(fixtures) <= {'fx_a','fx_b','fx_c'}:
        raise ValueError('reward.fixtures must name at least two distinct owned fixtures')
    if cfg['grpo']['max_steps'] < 1:
        raise ValueError('grpo.max_steps must be positive')
    for key in ('temperature',):
        if cfg['grpo'].get(key, 0) <= 0:
            raise ValueError(f'grpo.{key} must be positive')
    if cfg['grpo'].get('beta', -1) < 0 or not 0 < cfg['grpo'].get('clip_epsilon', 0) < 1:
        raise ValueError('invalid GRPO beta/clip_epsilon')
    timeout = cfg.get('pipeline', {}).get('timeout_seconds', 0)
    if timeout is not None and (not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout < 0):
        raise ValueError('pipeline.timeout_seconds must be nonnegative or null')
    if not cfg['adapter'].get('target_modules'):
        raise ValueError('adapter.target_modules must not be empty')
    if not 0 <= cfg['adapter'].get('dropout', 0) < 1:
        raise ValueError('adapter.dropout must be in [0,1)')
    for field in ('train', 'dev', 'test'):
        if not isinstance(cfg['data'].get(field), str):
            raise ValueError(f'data.{field} must be a path')


def config_fingerprint(cfg: dict) -> str:
    value = {k: v for k, v in cfg.items() if not k.startswith('_')}
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
