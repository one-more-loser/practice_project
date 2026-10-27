"""Local native-adapter inference; never executes SQL."""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'training'))
from common import load_jsonl, prompt
from scoring import strict_json


def generate_rows(cfg, rows, adapter_path=None, loader=None):
    import torch
    if loader is None:
        from runtime import load_policy
        loader = load_policy
    if cfg['model'].get('local_files_only') is not True:
        raise ValueError('Inference requires model.local_files_only=true')
    model, tokenizer = loader(cfg, adapter_path=adapter_path, trainable=False)
    model.eval()
    tokenizer.padding_side = 'left'
    if tokenizer.pad_token_id is None:
        if tokenizer.eos_token_id is None:
            raise ValueError('Tokenizer must define EOS or padding token')
        tokenizer.pad_token = tokenizer.eos_token
    ev = cfg['evaluation']
    batch_size = int(ev.get('batch_size', 1))
    max_prompt = int(ev.get('max_prompt_tokens', 1152))
    if min(batch_size, max_prompt, int(ev.get('max_new_tokens', 256))) < 1:
        raise ValueError('Token limits and batch_size must be positive')
    records = []
    for start in range(0, len(rows), batch_size):
        batch = rows[start:start + batch_size]
        texts = [prompt(row) for row in batch]
        lengths = [len(tokenizer(text, add_special_tokens=False)['input_ids']) for text in texts]
        pending = []
        for offset, (row, text, length) in enumerate(zip(batch, texts, lengths)):
            rec = {'id': row.get('id', str(start + offset)), 'template_id': row.get('template_id', ''),
                   'prompt_tokens': length, 'prompt_truncated': length > max_prompt, 'generated': ''}
            if length > max_prompt and not ev.get('allow_truncate', False):
                rec['skipped'] = 'prompt_exceeds_max_prompt_tokens'
            else:
                pending.append((text, rec))
            records.append(rec)
        if not pending:
            continue
        inputs = tokenizer([item[0] for item in pending], return_tensors='pt', padding=True,
                           truncation=True, max_length=max_prompt, add_special_tokens=False)
        device = next(model.parameters()).device
        inputs = {key: value.to(device) for key, value in inputs.items()
                  if key in ('input_ids', 'attention_mask')}
        width = inputs['input_ids'].shape[1]
        generation = {'max_new_tokens': int(ev.get('max_new_tokens', 256)),
                      'do_sample': bool(ev.get('do_sample', False)),
                      'pad_token_id': tokenizer.pad_token_id,
                      'eos_token_id': tokenizer.eos_token_id, 'use_cache': bool(ev.get('use_cache', True))}
        if ev.get('cache_implementation'):
            generation['cache_implementation'] = ev['cache_implementation']
        if generation['do_sample']:
            generation.update(temperature=float(ev.get('temperature', 1.0)), top_p=float(ev.get('top_p', 1.0)))
        with torch.inference_mode():
            output = model.generate(**inputs, **generation)
        for tokens, (_, rec) in zip(output, pending):
            completion = tokens[width:]
            rec['generated'] = tokenizer.decode(completion, skip_special_tokens=True)
            rec['generated_tokens'] = len(completion)
            rec['generation_truncated'] = (len(completion) >= generation['max_new_tokens']
                and not any(int(token) == tokenizer.eos_token_id for token in completion))
    for rec in records:
        raw = rec['generated']
        obj = strict_json(raw)
        rec.update(output_bytes=len(raw.encode('utf-8')), empty_output=not raw.strip(),
                   parse_diagnostics={'strict_json': obj is not None, 'kind': obj.get('kind') if obj else None})
    return records


def main():
    from config import load_config, resolve_path
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--config', required=True)
    ap.add_argument('--set', action='append', default=[])
    source = ap.add_mutually_exclusive_group(required=True)
    source.add_argument('--question')
    source.add_argument('--input-jsonl')
    ap.add_argument('--adapter')
    ap.add_argument('--output')
    args = ap.parse_args()
    cfg = load_config(args.config, args.set)
    rows = ([{'id': 'question', 'question': args.question}] if args.question is not None
            else load_jsonl(resolve_path(args.input_jsonl)))
    result = {'records': generate_rows(cfg, rows, args.adapter)}
    encoded = json.dumps(result, ensure_ascii=False, indent=2) + '\n'
    if args.output:
        target = resolve_path(args.output)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(encoded, encoding='utf-8')
    else:
        print(encoded, end='')


if __name__ == '__main__':
    main()
