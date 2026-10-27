"""Native PyTorch SFT: completion-only loss, explicit accumulation, exact resume.

Transformers only loads model/tokenizer. --dry-run never imports torch or models.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import math
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'training'))
from common import completion_text, load_jsonl, prompt
from config import DEFAULT_CONFIG, config_fingerprint, load_config, resolve_path


def tokenize_example(tok, row, max_len, allow_truncate=False):
    """Keep the complete schema/question and answer; never pad a label."""
    prefix = tok(prompt(row), add_special_tokens=False)['input_ids']
    completion = tok(completion_text(row), add_special_tokens=False)['input_ids']
    if tok.eos_token_id is None:
        raise ValueError('tokenizer must define EOS')
    if not completion or completion[-1] != tok.eos_token_id:
        completion.append(tok.eos_token_id)
    if len(completion) >= max_len:
        raise ValueError(f'answer length {len(completion)} leaves no prompt in {max_len}')
    if len(prefix) + len(completion) > max_len:
        if not allow_truncate:
            raise ValueError(f'{row.get("id")}: {len(prefix) + len(completion)} exceeds {max_len}; truncation forbidden')
        prefix = prefix[-(max_len - len(completion)):]
    if not prefix:
        raise ValueError('a nonempty prompt is required for causal answer prediction')
    ids = prefix + completion
    return {'input_ids': ids, 'attention_mask': [1] * len(ids),
            'labels': [-100] * len(prefix) + completion}


def epoch_batches(rows, batch_size, seed, epoch, bucket_size):
    """Shuffle deterministically; group nearby lengths in small buckets."""
    rng = random.Random(seed + epoch)
    indices = list(range(len(rows)))
    rng.shuffle(indices)
    bucket_size = max(batch_size, bucket_size)
    batches = []
    for start in range(0, len(indices), bucket_size):
        bucket = indices[start:start + bucket_size]
        bucket.sort(key=lambda i: len(rows[i]['input_ids']))
        batches.extend(bucket[i:i + batch_size] for i in range(0, len(bucket), batch_size))
    rng.shuffle(batches)
    return batches


def collate(rows, pad_token_id, device, pin_memory=False):
    import torch
    width = max(len(row['input_ids']) for row in rows)
    ids = torch.full((len(rows), width), pad_token_id, dtype=torch.long)
    mask = torch.zeros_like(ids)
    labels = torch.full_like(ids, -100)
    for index, row in enumerate(rows):
        n = len(row['input_ids'])
        ids[index, :n] = torch.tensor(row['input_ids'])
        mask[index, :n] = 1
        labels[index, :n] = torch.tensor(row['labels'])
    result = {'input_ids': ids, 'attention_mask': mask, 'labels': labels}
    if pin_memory and str(device).startswith('cuda'):
        result = {k: v.pin_memory() for k, v in result.items()}
    return {k: v.to(device, non_blocking=pin_memory) for k, v in result.items()}


def completion_loss(model, batch, chunk_tokens=32):
    """Causal CE on answers. Qwen3 skips prompt vocabulary projection."""
    import torch
    from runtime import causal_logits
    labels = batch['labels']
    first = int((labels != -100).nonzero()[:, 1].min().item())
    logits = causal_logits(model, batch['input_ids'], batch['attention_mask'], first)
    targets = labels[:, first:]
    flat_logits, flat_targets = logits.reshape(-1, logits.shape[-1]), targets.reshape(-1)
    count = int((flat_targets != -100).sum().item())
    # Chunk the FP32 CE workspace; model and saved logits stay in model dtype.
    total = logits.new_zeros((), dtype=torch.float32)
    for start in range(0, flat_targets.numel(), chunk_tokens):
        stop = start + chunk_tokens
        total = total + torch.nn.functional.cross_entropy(flat_logits[start:stop].float(),
            flat_targets[start:stop], ignore_index=-100, reduction='sum')
    return total / count, count


def dataset_identity(cfg):
    return {name: hashlib.sha256(resolve_path(cfg['data'][name]).read_bytes()).hexdigest()
            for name in ('train', 'dev')}


def check_sft_inputs(cfg):
    """Never allow a held-out split to become SFT merely by changing its path."""
    train_path, dev_path, test_path = (resolve_path(cfg['data'][s]) for s in ('train','dev','test'))
    if len({train_path, dev_path, test_path}) != 3:
        raise ValueError('train/dev/test paths must be distinct')
    manifest = json.loads(resolve_path(cfg['data']['manifest']).read_text(encoding='utf-8'))
    split_rows = {}
    for split, path in [('train',train_path),('dev',dev_path)]:
        rows = load_jsonl(path)
        if not rows or any(r.get('split') != split for r in rows):
            raise ValueError(f'{split} contains wrong or missing explicit split labels')
        ids = [r.get('id') for r in rows]
        if any(not isinstance(i, str) or not i for i in ids) or len(ids) != len(set(ids)):
            raise ValueError(f'{split} IDs must be nonempty and unique')
        if hashlib.sha256(path.read_bytes()).hexdigest() != manifest.get('files', {}).get(split, {}).get('sha256'):
            raise ValueError(f'{split} data differs from its manifest')
        for row in rows:
            prompt(row)  # Reject a SQLite/operations sample before model loading.
        split_rows[split] = rows
    for key in ('id','group_id'):
        if {r[key] for r in split_rows['train'] if key in r} & {r[key] for r in split_rows['dev'] if key in r}:
            raise ValueError(f'train/dev {key} overlap')
    return split_rows['train'], split_rows['dev']


def train(cfg, output, resume=None):
    from output_lock import exclusive_output
    with exclusive_output(output, name='.training.lock'):
        return _train(cfg, output, resume)


def _train(cfg, output, resume=None):
    train_rows, dev_rows = check_sft_inputs(cfg)
    import torch
    from lora import adapter_parameters, save_adapter
    from runtime import (UpdateTracker, load_checkpoint, load_policy, save_checkpoint,
                         seed_everything, write_json)
    opts = cfg['sft']
    seed_everything(opts['seed'])
    model, tokenizer = load_policy(cfg, trainable=True)
    from provenance import training_identity
    model._native_training_identity = training_identity(cfg, 'sft')
    encoded = [tokenize_example(tokenizer, r, opts['max_seq_len']) for r in train_rows]
    dev = [tokenize_example(tokenizer, r, opts['max_seq_len']) for r in dev_rows]
    if not encoded:
        raise ValueError('empty training data')
    parameters = [p for _, p in adapter_parameters(model)]
    device = next(model.parameters()).device
    if opts.get('optimizer', 'adamw') != 'adamw':
        raise ValueError('sft.optimizer supports native torch AdamW')
    optimizer = torch.optim.AdamW(parameters, lr=opts['learning_rate'],
        weight_decay=opts['weight_decay'], fused=bool(opts.get('fused_adamw', True) and device.type == 'cuda'))
    batches_per_epoch = len(epoch_batches(encoded, opts['batch_size'], opts['seed'], 0,
                                          opts.get('length_bucket_size', 32)))
    per_epoch = math.ceil(batches_per_epoch / opts['gradient_accumulation_steps'])
    total_steps = opts['max_steps'] if opts['max_steps'] > 0 else math.ceil(per_epoch * opts['epochs'])
    warmup = math.ceil(total_steps * opts.get('warmup_ratio', 0.03))
    def lr_multiplier(step):
        if warmup and step < warmup:
            return (step + 1) / warmup
        return 0.5 * (1 + math.cos(math.pi * min(1, (step - warmup) / max(1, total_steps - warmup))))
    scheduler = torch.optim.lr_scheduler.LambdaLR(optimizer, lr_multiplier)
    scaler = torch.amp.GradScaler('cuda', enabled=device.type == 'cuda' and cfg['model']['torch_dtype'] == 'float16')
    identity = dataset_identity(cfg)
    # Output paths/pipeline resume flags/time budget do not change optimization.
    fingerprint = config_fingerprint({k: cfg[k] for k in ('model', 'adapter', 'sft', 'data')})
    step, epoch, cursor = 0, 0, 0
    if resume:
        state = load_checkpoint(resolve_path(resume), model, optimizer, scheduler)
        extra = state['extra']
        if extra.get('data_sha256') != identity or extra.get('config_fingerprint') != fingerprint:
            raise ValueError('resume data or effective configuration changed')
        step, epoch, cursor = state['step'], extra['epoch'], extra['cursor']
        if extra.get('grad_scaler'):
            scaler.load_state_dict(extra['grad_scaler'])
    initial_step = step
    tracker = UpdateTracker(model)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    write_json(output / 'resolved_config.json', cfg)
    if device.type == 'cuda':
        torch.cuda.reset_peak_memory_stats()
    start = time.monotonic()
    chunk = opts.get('loss_chunk_tokens', 32)
    metrics_path = output / 'metrics.jsonl'
    run = {'status': 'running', 'stage': 'sft', 'global_step': step, 'data_sha256': identity,
        'config_fingerprint': fingerprint, 'model': cfg['model'],
        'train_rows': len(encoded), 'dev_rows': len(dev), 'target_steps': total_steps,
        'trainable_parameters': sum(p.numel() for p in parameters), 'device': str(device),
        'torch': torch.__version__, 'cuda': torch.version.cuda,
        'gpu': torch.cuda.get_device_name(device) if device.type == 'cuda' else None}
    write_json(output / 'run.json', run)
    def checkpoint(path):
        save_checkpoint(path, model, optimizer, step, extra={'epoch': epoch, 'cursor': cursor,
            'data_sha256': identity, 'config_fingerprint': fingerprint,
            'grad_scaler': scaler.state_dict()}, scheduler=scheduler)
    try:
        while step < total_steps:
            batches = epoch_batches(encoded, opts['batch_size'], opts['seed'], epoch,
                                    opts.get('length_bucket_size', 32))
            if cursor >= len(batches):
                epoch, cursor = epoch + 1, 0
                continue
            group = batches[cursor:cursor + opts['gradient_accumulation_steps']]
            optimizer.zero_grad(set_to_none=True)
            group_tokens, summed_loss = 0, 0.0
            tick = time.monotonic()
            # Match a larger batch by weighting the supervised token count.
            counts = [sum(sum(v != -100 for v in encoded[i]['labels']) for i in b) for b in group]
            for batch_ids, count in zip(group, counts):
                batch = collate([encoded[i] for i in batch_ids], tokenizer.pad_token_id,
                                device, opts.get('pin_memory', True))
                loss, n = completion_loss(model, batch, chunk)
                if not bool(torch.isfinite(loss).item()):
                    raise FloatingPointError('nonfinite SFT loss; optimizer step refused')
                scaler.scale(loss * count / sum(counts)).backward()
                summed_loss += loss.detach().item() * count
                group_tokens += n
            scaler.unscale_(optimizer)
            norm = torch.nn.utils.clip_grad_norm_(parameters, opts['max_grad_norm'])
            if not bool(torch.isfinite(norm).item()):
                raise FloatingPointError('nonfinite gradient; optimizer step refused')
            scaler.step(optimizer)
            scaler.update()
            scheduler.step()
            step, cursor = step + 1, cursor + len(group)
            metric = {'step': step, 'epoch': epoch, 'cursor': cursor,
                'loss': summed_loss / group_tokens, 'learning_rate': scheduler.get_last_lr()[0],
                'gradient_norm': float(norm), 'answer_tokens': group_tokens,
                'answer_tokens_per_second': group_tokens / max(1e-9, time.monotonic() - tick),
                'elapsed_seconds': time.monotonic() - start}
            if device.type == 'cuda':
                metric.update(peak_allocated_gb=torch.cuda.max_memory_allocated() / 2**30,
                              peak_reserved_gb=torch.cuda.max_memory_reserved() / 2**30)
            with metrics_path.open('a', encoding='utf-8') as handle:
                handle.write(json.dumps(metric) + '\n')
            if step % opts.get('log_every', 1) == 0:
                print(json.dumps(metric), flush=True)
            if step % opts['save_every'] == 0:
                checkpoint(output / f'checkpoint-{step}')
            limit = cfg['runtime'].get('max_seconds', 0)
            if limit and time.monotonic() - start >= limit and step < total_steps:
                run['status'] = 'time_limit'
                break
        # Save cursor/RNG before dev evaluation so resumed dropout matches.
        checkpoint(output / 'checkpoint-last')
        if run['status'] == 'running':
            run['status'] = 'completed'
        if dev and run['status'] == 'completed':
            model.eval()
            dev_sum, dev_tokens = 0.0, 0
            with torch.no_grad():
                for row in dev:
                    loss, count = completion_loss(model, collate([row], tokenizer.pad_token_id, device), chunk)
                    dev_sum, dev_tokens = dev_sum + loss.item() * count, dev_tokens + count
            run['dev_loss'] = dev_sum / dev_tokens
        save_adapter(model, output / 'adapter', extra={'stage': 'sft', 'global_step': step,
                     'base_model': cfg['model'], 'data_sha256': identity})
        tokenizer.save_pretrained(output / 'adapter')
        run.update(global_step=step, elapsed_seconds=time.monotonic() - start,
                   weight_update=tracker.report(), adapter=str(output / 'adapter'))
        if step > initial_step and not run['weight_update']['changed']:
            raise RuntimeError('optimizer steps produced no observed adapter update')
    except BaseException as exc:
        run.update(status='failed', error=f'{type(exc).__name__}: {exc}', global_step=step)
        raise
    finally:
        write_json(output / 'run.json', run)
    if run['status'] != 'completed':
        raise RuntimeError('SFT time limit reached; resume checkpoint-last instead of evaluating a partial run')
    return run


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default=str(DEFAULT_CONFIG))
    parser.add_argument('--set', action='append', default=[])
    parser.add_argument('--output')
    parser.add_argument('--resume')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    cfg = load_config(args.config, args.set)
    if args.dry_run:
        rows, dev = check_sft_inputs(cfg)
        if not rows:
            raise ValueError('empty train data')
        print(json.dumps({'status': 'data_check_only_not_training', 'model_loaded': False,
            'train_rows': len(rows), 'dev_rows': len(dev), 'config': cfg,
            'data_sha256': dataset_identity(cfg)}, ensure_ascii=False, indent=2))
        return
    output = resolve_path(args.output) if args.output else resolve_path(cfg['runtime']['output_dir']) / 'sft'
    print(json.dumps(train(cfg, output, args.resume), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
