"""Model loading and native checkpoints; no training framework or implicit download."""
from __future__ import annotations

import hashlib
import inspect
import json
import os
import random
from pathlib import Path

from config import resolve_path


def write_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + '\n', encoding='utf-8')
    os.replace(tmp, path)


def seed_everything(seed):
    import torch
    random.seed(seed)
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)


def load_policy(cfg, adapter_path=None, trainable=True):
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer
    from lora import inject_lora, load_adapter
    device = cfg['runtime']['device']
    if cfg['model'].get('local_files_only') is not True:
        raise ValueError('runtime requires a separately prepared local model; implicit downloads are disabled')
    if device == 'auto':
        device = 'cuda' if torch.cuda.is_available() else 'cpu'
    if device == 'cuda':
        if not torch.cuda.is_available():
            raise RuntimeError('CUDA is unavailable; no implicit CPU fallback for the 8B run')
        if not torch.cuda.is_bf16_supported() and cfg['model']['torch_dtype'] == 'bfloat16':
            raise RuntimeError('this CUDA device does not support BF16')
        total = torch.cuda.get_device_properties(0).total_memory / 2**30
        if total < cfg['runtime'].get('min_vram_gb', 0):
            raise RuntimeError(f'GPU has {total:.1f} GiB; profile requires {cfg["runtime"]["min_vram_gb"]}')
        torch.backends.cuda.matmul.allow_tf32 = cfg['runtime'].get('allow_tf32', True)
        torch.backends.cudnn.allow_tf32 = cfg['runtime'].get('allow_tf32', True)
    dtype = getattr(torch, cfg['model']['torch_dtype'])
    local = cfg['model'].get('local_files_only', True)
    model_name = cfg['model']['path']
    if local:
        model_name = str(resolve_path(model_name))
        if not Path(model_name).is_dir():
            raise FileNotFoundError(f'local model directory absent: {model_name}; download only after fee approval')
    kwargs = {'local_files_only': local, 'trust_remote_code': False}
    if cfg['model'].get('revision'):
        kwargs['revision'] = cfg['model']['revision']
    tokenizer = AutoTokenizer.from_pretrained(model_name, **kwargs)
    if tokenizer.eos_token_id is None:
        raise ValueError('tokenizer must define EOS')
    # Decoder-only generation: EOS is also padding, so generated padding cannot
    # be confused with a valid interior action when masking GRPO completions.
    tokenizer.pad_token = tokenizer.eos_token
    tokenizer.padding_side = 'right' if trainable else 'left'
    model = AutoModelForCausalLM.from_pretrained(model_name, torch_dtype=dtype,
        attn_implementation=cfg['model'].get('attention', 'sdpa'), **kwargs)
    model = inject_lora(model, cfg['adapter'])
    from provenance import base_identity
    model._native_base_identity = base_identity(cfg)
    if adapter_path:
        load_adapter(model, resolve_path(adapter_path), trainable)
    if not trainable:
        model.requires_grad_(False)
    model.to(device)
    model._native_supports_logits_to_keep = 'logits_to_keep' in inspect.signature(model.forward).parameters
    model.config.use_cache = not trainable
    if trainable and cfg['runtime'].get('gradient_checkpointing', True):
        model.gradient_checkpointing_enable(gradient_checkpointing_kwargs={'use_reentrant': False})
    if cfg['runtime'].get('compile', False):
        model.forward = torch.compile(model.forward, dynamic=True, mode=cfg['runtime'].get('compile_mode', 'default'))
    model.train(trainable)
    return model, tokenizer


def causal_logits(model, input_ids, attention_mask, first_target_index):
    """Only project completion positions for Qwen3, avoiding prompt x vocab logits."""
    if not 1 <= first_target_index < input_ids.shape[1]:
        raise ValueError('first target must follow a nonempty prompt and precede sequence end')
    count = input_ids.shape[1] - first_target_index + 1
    kwargs = {'input_ids': input_ids, 'attention_mask': attention_mask, 'use_cache': False}
    if getattr(model, '_native_supports_logits_to_keep', False):
        kwargs['logits_to_keep'] = count
        return model(**kwargs).logits[:, :-1]
    return model(**kwargs).logits[:, first_target_index - 1:-1]


def save_checkpoint(path, model, optimizer, step, extra=None, scheduler=None):
    import torch
    from lora import adapter_state_dict, save_adapter
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    state = {'format': 'native-training-v1', 'step': step,
        'identity': {'base': getattr(model, '_native_base_identity', None),
                     'training': getattr(model, '_native_training_identity', None)},
        'adapter': adapter_state_dict(model), 'optimizer': optimizer.state_dict(),
        'scheduler': scheduler.state_dict() if scheduler else None,
        'rng_python': random.getstate(), 'rng_torch': torch.get_rng_state(),
        'rng_cuda': torch.cuda.get_rng_state_all() if torch.cuda.is_available() else [],
        'extra': extra or {}}
    tmp = path / 'checkpoint.pt.tmp'
    torch.save(state, tmp)
    os.replace(tmp, path / 'checkpoint.pt')
    from provenance import file_sha256
    write_json(path / 'checkpoint-integrity.json', {'sha256': file_sha256(path / 'checkpoint.pt')})
    save_adapter(model, path / 'adapter', extra={'step': step})
    write_json(path / 'checkpoint.json', {'format': state['format'], 'step': step, 'extra': extra or {}})


def load_checkpoint(path, model, optimizer=None, scheduler=None):
    import torch
    from lora import load_adapter_state_dict
    path = Path(path)
    from provenance import file_sha256, validate_checkpoint_metadata
    file = path / 'checkpoint.pt' if path.is_dir() else path
    integrity = file.parent / 'checkpoint-integrity.json'
    if not integrity.exists() or json.loads(integrity.read_text())['sha256'] != file_sha256(file):
        raise ValueError('checkpoint integrity missing/mismatched; legacy conversion is explicit')
    state = torch.load(file, map_location='cpu', weights_only=True)
    identity = {'base': getattr(model, '_native_base_identity', None),
                'training': getattr(model, '_native_training_identity', None)}
    validate_checkpoint_metadata(state, identity)
    load_adapter_state_dict(model, state['adapter'], trainable=True)
    if optimizer is not None:
        optimizer.load_state_dict(state['optimizer'])
    if scheduler is not None and state.get('scheduler'):
        scheduler.load_state_dict(state['scheduler'])
    random.setstate(state['rng_python'])
    torch.set_rng_state(state['rng_torch'])
    if torch.cuda.is_available() and state.get('rng_cuda'):
        torch.cuda.set_rng_state_all(state['rng_cuda'])
    return state


class UpdateTracker:
    """A few small LoRA tensors provide before/after numerical update evidence."""
    def __init__(self, model, limit=4):
        from lora import adapter_parameters
        self.model = model
        self.initial = {n: p.detach().cpu().clone() for n, p in list(adapter_parameters(model))[:limit]}

    def report(self):
        import torch
        from lora import adapter_parameters
        current = dict(adapter_parameters(self.model))
        details = []
        for name, before in self.initial.items():
            after = current[name].detach().cpu()
            delta = (after.float() - before.float()).norm().item()
            finite = bool(torch.isfinite(after).all().item())
            digest = lambda t: hashlib.sha256(t.contiguous().view(torch.uint8).numpy().tobytes()).hexdigest()
            details.append({'name': name, 'before_sha256': digest(before), 'after_sha256': digest(after),
                'delta_norm': delta, 'finite': finite})
        return {'tensors': details, 'changed': any(x['finite'] and x['delta_norm'] > 0 for x in details),
            'all_finite': all(x['finite'] for x in details)}
