"""Native GRPO operations. Importing this module does not load PyTorch."""
from __future__ import annotations

import math


def normalize_group(rewards: list[float]) -> list[float]:
    """Population normalization; a constant group has exactly zero advantage."""
    if len(rewards) < 2 or not all(math.isfinite(x) for x in rewards):
        raise ValueError("a finite reward group with at least two members is required")
    # Scale before centering: finite rewards can otherwise overflow the sum
    # (or underflow their squared deviations) and silently produce NaN/zeros.
    scale = max(abs(x) for x in rewards)
    values = [x / scale for x in rewards] if scale else list(rewards)
    mean = math.fsum(values) / len(values)
    std = math.sqrt(math.fsum((x - mean) ** 2 for x in values) / len(values))
    return [(x - mean) / std for x in values] if std else [0.0] * len(rewards)


def completion_mask(tokens, eos_token_id, pad_token_id):
    """Include the first EOS, exclude subsequent tokens and actual padding."""
    import torch
    eos = tokens.eq(eos_token_id)
    before_or_first = eos.cumsum(-1) - eos.to(torch.long) == 0
    valid = before_or_first
    if pad_token_id != eos_token_id:
        valid = valid & tokens.ne(pad_token_id)
    return valid.to(torch.float32)


def token_log_probs(model, input_ids, attention_mask, prompt_length, temperature=1.0):
    """Only completion predictions, shifted by one causal position."""
    import torch
    from runtime import causal_logits
    if not math.isfinite(temperature) or temperature <= 0:
        raise ValueError("temperature must be finite and positive")
    logits = causal_logits(model, input_ids, attention_mask, prompt_length)
    targets = input_ids[:, prompt_length:]
    # Cross entropy avoids a second full vocabulary-sized log_softmax allocation.
    return -torch.nn.functional.cross_entropy(
        logits.reshape(-1, logits.shape[-1]).float() / temperature, targets.reshape(-1),
        reduction="none").reshape(targets.shape)


def grpo_loss(log_probs, old_log_probs, reference_log_probs, advantages,
              mask, beta, clip_epsilon):
    """Sequence-balanced clipped surrogate with nonnegative sampled KL."""
    import torch
    # Mask BEFORE exponentiation: inf * 0 is NaN and padded logits must never
    # affect either objective or gradient.
    active = mask.bool()
    log_probs = torch.where(active, log_probs, torch.zeros_like(log_probs))
    old = torch.where(active, old_log_probs.detach(), torch.zeros_like(old_log_probs))
    reference = torch.where(active, reference_log_probs.detach(),
                            torch.zeros_like(reference_log_probs))
    advantage = advantages.detach().unsqueeze(-1)
    ratio = torch.exp(log_probs - old)
    unclipped = ratio * advantage
    clipped = ratio.clamp(1 - clip_epsilon, 1 + clip_epsilon) * advantage
    delta = reference - log_probs
    kl = torch.exp(delta) - delta - 1
    per_token = -torch.minimum(unclipped, clipped) + beta * kl
    lengths = mask.sum(-1)
    if bool((lengths == 0).any()):
        raise ValueError("empty completion cannot be optimized")
    loss = ((per_token * mask).sum(-1) / lengths).mean()
    return loss, {"kl": ((kl.detach() * mask).sum() / mask.sum()).item(),
                  "clip_fraction": (((ratio.detach() - 1).abs() > clip_epsilon)
                                    * mask).sum().div(mask.sum()).item()}
