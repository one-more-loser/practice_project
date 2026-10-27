"""Compare three real business-oracle reports with fixed test denominators."""
from __future__ import annotations
import argparse
from collections import defaultdict
import json
from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'training'))
from common import load_jsonl
from provenance import evaluation_identity


def compare(rows, reports):
    expected = {row['id'] for row in rows}
    identity = None
    if set(reports) != {'base', 'sft', 'grpo'}:
        raise ValueError('comparison requires Base/SFT/GRPO reports')
    if len(expected) != len(rows):
        raise ValueError('duplicate frozen evaluation IDs')
    indexed = {}
    for name, report in reports.items():
        actual_identity = evaluation_identity(rows, report.get('summary', {}).get('consumer01_root'))
        if identity is None:
            identity = actual_identity
        if report.get('summary', {}).get('oracle_error') is not False:
            raise ValueError(f'{name}: missing successful business oracle evidence')
        if report.get('evaluation_identity') != actual_identity or actual_identity != identity:
            raise ValueError(f'{name}: test/oracle identity differs or missing; rescore raw predictions explicitly')
        records = report.get('results', [])
        ids = [item['id'] for item in records]
        if len(ids) != len(set(ids)) or set(ids) != expected:
            raise ValueError(f'{name}: report IDs must exactly match frozen test')
        indexed[name] = {item['id']: item for item in records}
    details = []
    categories = defaultdict(lambda: {'total': 0, **{name: 0 for name in reports}})
    for row in rows:
        passes = {name: indexed[name][row['id']].get('pass') is True for name in reports}
        detail = {'id': row['id'], 'template_id': row['template_id'], 'expected_kind': row['expected_kind'],
                  'question': row['question'], 'pass': passes,
                  'runs': {name: indexed[name][row['id']] for name in reports},
                  'sft_vs_base': int(passes['sft']) - int(passes['base']),
                  'grpo_vs_sft': int(passes['grpo']) - int(passes['sft'])}
        details.append(detail)
        for category in ('all', 'kind:' + row['expected_kind'], 'template:' + row['template_id']):
            categories[category]['total'] += 1
            for name in reports:
                categories[category][name] += int(passes[name])
    for stats in categories.values():
        stats['rates'] = {name: stats[name] / stats['total'] for name in reports}
    return {'categories': dict(categories), 'records': details, 'evaluation_identity': identity,
            'summaries': {name: report['summary'] for name, report in reports.items()},
            'boundary': 'Business oracle reports; all three models evaluated on identical frozen test IDs'}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--test', required=True)
    for name in ('base', 'sft', 'grpo'):
        ap.add_argument('--' + name, required=True)
    ap.add_argument('--output', required=True)
    args = ap.parse_args()
    def path(value):
        p = Path(value)
        return p if p.is_absolute() else ROOT / p
    rows = load_jsonl(path(args.test))
    reports = {name: json.loads(path(getattr(args, name)).read_text(encoding='utf-8'))
               for name in ('base', 'sft', 'grpo')}
    result = compare(rows, reports)
    out = path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    lines = ['# Base / SFT / GRPO 同题业务评测', '', '| 类别 | 题数 | Base通过 | SFT通过 | GRPO通过 |', '|---|---:|---:|---:|---:|']
    for category, stats in result['categories'].items():
        lines.append(f"| {category} | {stats['total']} | {stats['base']} | {stats['sft']} | {stats['grpo']} |")
    lines += ['', '| 题目ID | 类型 | Base | SFT | GRPO | SFT变化 | GRPO变化 |', '|---|---|---:|---:|---:|---:|---:|']
    for rec in result['records']:
        values = rec['pass']
        lines.append(f"| {rec['id']} | {rec['expected_kind']} | {int(values['base'])} | {int(values['sft'])} | {int(values['grpo'])} | {rec['sft_vs_base']:+d} | {rec['grpo_vs_sft']:+d} |")
    out.with_suffix('.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()
