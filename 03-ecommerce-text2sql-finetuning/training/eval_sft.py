"""Score raw saved predictions or evaluate a local Base/native adapter."""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'training'))
sys.path.insert(0, str(ROOT / 'sandbox' / 'runner'))
from common import load_jsonl
from scoring import score_rows, index_predictions


def load_predictions(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding='utf-8'))
    if isinstance(data, dict):
        data = data.get('records', data.get('predictions'))
    if not isinstance(data, list) or any(not isinstance(row, dict) for row in data):
        raise ValueError('Predictions must be a list, or records/predictions list')
    index_predictions(data)
    return data


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--config')
    ap.add_argument('--set', action='append', default=[])
    ap.add_argument('--adapter')
    ap.add_argument('--test')
    ap.add_argument('--output', default='outputs/eval-base.json')
    ap.add_argument('--predictions')
    args = ap.parse_args()
    cfg = None
    if args.config:
        from config import load_config
        cfg = load_config(args.config, args.set)
    test = Path(args.test or (cfg['data']['test'] if cfg else 'data/sft_v1/test.jsonl'))
    if not test.is_absolute():
        test = ROOT / test
    rows = load_jsonl(test)
    if args.predictions:
        path = Path(args.predictions)
        preds = load_predictions(path if path.is_absolute() else ROOT / path)
    else:
        if cfg is None:
            ap.error('--config is required for model evaluation')
        from inference import generate_rows
        preds = generate_rows(cfg, rows, args.adapter)
    result = score_rows(rows, preds)
    by_id = {row['id']: row for row in preds}
    for record in result['records']:
        for key in ('prompt_tokens', 'prompt_truncated', 'skipped', 'output_bytes', 'empty_output',
                    'generation_truncated', 'generated_tokens', 'timed_out', 'error'):
            source = by_id.get(record['id'], {})
            if key in source:
                record[key] = source[key]
    result['summary'].update(test=str(test), adapter=args.adapter,
                             model=cfg['model']['path'] if cfg else 'prediction-file')
    out = Path(args.output)
    if not out.is_absolute():
        out = ROOT / out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(result['summary'], ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
