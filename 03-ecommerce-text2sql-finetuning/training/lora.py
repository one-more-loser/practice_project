"""Native torch LoRA. No Trainer/PEFT dependency and no copy of frozen Base."""
from __future__ import annotations

from contextlib import contextmanager
import json
import math
from pathlib import Path

import torch
from torch import nn
from torch.nn import functional as F


class LoRALinear(nn.Module):
    def __init__(self, base: nn.Linear, rank: int, alpha: float, dropout: float):
        super().__init__()
        self.base = base
        self.base.requires_grad_(False)
        self.rank = rank
        self.alpha = alpha
        self.scaling = alpha / rank
        self.dropout = nn.Dropout(dropout)
        # FP32 adapter/master/optimizer state, BF16 matrix multiplication under
        # autocast. Weights are cast, not the full hidden-state tensor.
        self.lora_A = nn.Parameter(torch.empty(rank, base.in_features, device=base.weight.device, dtype=torch.float32))
        self.lora_B = nn.Parameter(torch.zeros(base.out_features, rank, device=base.weight.device, dtype=torch.float32))
        nn.init.kaiming_uniform_(self.lora_A, a=math.sqrt(5))
        self._use_reference = False

    def forward(self, x):
        base = self.base(x)
        a = self._reference_A if self._use_reference else self.lora_A
        b = self._reference_B if self._use_reference else self.lora_B
        hidden = self.dropout(x) if not self._use_reference else x
        update = F.linear(F.linear(hidden, a.to(hidden.dtype)), b.to(hidden.dtype))
        return base + update.to(base.dtype) * self.scaling


def modules(model):
    return [(name, module) for name, module in model.named_modules() if isinstance(module, LoRALinear)]


def inject_lora(model, config: dict):
    model.requires_grad_(False)
    targets = set(config['target_modules'])
    found = []
    for name, module in list(model.named_modules()):
        if not isinstance(module, nn.Linear) or name.rsplit('.', 1)[-1] not in targets:
            continue
        parent_name, _, child = name.rpartition('.')
        parent = model.get_submodule(parent_name) if parent_name else model
        setattr(parent, child, LoRALinear(module, int(config['rank']), float(config['alpha']), float(config['dropout'])))
        found.append(name)
    if not found:
        raise ValueError(f'no Linear modules matched LoRA targets {sorted(targets)}')
    model._native_adapter_config = dict(config)
    return model


def adapter_parameters(model):
    for name, module in modules(model):
        yield f'{name}.lora_A', module.lora_A
        yield f'{name}.lora_B', module.lora_B


def adapter_state_dict(model):
    return {name: param.detach().cpu().clone() for name, param in adapter_parameters(model)}


def load_adapter_state_dict(model, state: dict, trainable=True):
    current = dict(adapter_parameters(model))
    if set(current) != set(state):
        raise ValueError(f'adapter parameter names differ: missing={set(current)-set(state)}, extra={set(state)-set(current)}')
    # Validate the entire state before touching any live parameter.
    for name, param in current.items():
        if param.shape != state[name].shape or not bool(torch.isfinite(state[name]).all().item()):
            raise ValueError(f'adapter shape/nonfinite mismatch for {name}')
    with torch.no_grad():
        for name, param in current.items():
            param.copy_(state[name].to(device=param.device, dtype=param.dtype))
            param.requires_grad_(trainable)


def save_adapter(model, path, extra=None):
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    state = adapter_state_dict(model)
    if not state:
        raise ValueError('refusing to save an empty adapter')
    torch.save(state, path / 'adapter.pt')
    config = {'format': 'native-lora-v1', **model._native_adapter_config, 'extra': extra or {},
              'base_identity': getattr(model, '_native_base_identity', None)}
    (path / 'adapter_config.json').write_text(json.dumps(config, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def load_adapter(model, path, trainable=True):
    path = Path(path)
    metadata = json.loads((path / 'adapter_config.json').read_text(encoding='utf-8'))
    own = model._native_adapter_config
    from provenance import validate_adapter_metadata
    validate_adapter_metadata(metadata, own, getattr(model, '_native_base_identity', None))
    load_adapter_state_dict(model, torch.load(path / 'adapter.pt', map_location='cpu', weights_only=True), trainable)
    return metadata


def freeze_reference(model):
    for _, module in modules(model):
        for key, param in (('_reference_A', module.lora_A), ('_reference_B', module.lora_B)):
            frozen = param.detach().clone()
            if key in module._buffers:
                module._buffers[key] = frozen
            else:
                module.register_buffer(key, frozen, persistent=False)


@contextmanager
def reference_policy(model):
    adapters = modules(model)
    if not adapters or any(not hasattr(m, '_reference_A') for _, m in adapters):
        raise RuntimeError('freeze_reference(model) must snapshot the SFT policy first')
    flags = [m._use_reference for _, m in adapters]
    try:
        for _, module in adapters:
            module._use_reference = True
        yield model
    finally:
        for (_, module), flag in zip(adapters, flags):
            module._use_reference = flag
