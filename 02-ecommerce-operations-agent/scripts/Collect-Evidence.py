"""Collect local evidence; does not run services/tests or claim external acceptance."""
import hashlib
import json
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

root = Path(__file__).resolve().parents[1]
assert root.name == '02-ecommerce-operations-agent'
suite = ET.parse(root / '.runtime/backend-results.xml').getroot()
suites = [suite] if suite.tag == 'testsuite' else list(suite.iter('testsuite'))
backend = {key: sum(int(s.get(key, '0')) for s in suites)
           for key in ('tests', 'failures', 'errors', 'skipped')}
front = json.loads((root / '.runtime/frontend-final-result.json').read_text(encoding='utf-8'))
assert front['exit_code'] == 0
assert backend['failures'] == backend['errors'] == 0
matched = re.search(r'Tests\s+(\d+) passed', front['output'])
assert matched and 'built in' in front['output']
files = list((root / 'backend/src').rglob('*.py')) + list((root / 'backend/tests').rglob('*.py'))
files += [p for p in (root / 'frontend/src').rglob('*') if p.is_file()]
files += [root / 'backend/requirements.lock', root / 'frontend/package-lock.json',
          root / 'frontend/vite.config.ts', root / 'contracts/activity-v0.1.json']
fingerprints = {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(files)}
report = {
    'captured_at': datetime.now(timezone.utc).isoformat(),
    'scope': '02 local independent modules; full V1 and 01 integration not accepted',
    'python': sys.version.split()[0], 'backend': backend,
    'frontend': {'tests_passed': int(matched[1]), 'build_exit_code': front['exit_code'],
                 'build_output': front['output']},
    'commands': ['backend: ../.venv/Scripts/python.exe -m pytest -q --tb=short --basetemp ../.runtime/acceptance-final --junitxml ../.runtime/backend-results.xml',
                 'frontend: npm test', 'frontend: npm run build',
                 'root: .venv/Scripts/python.exe -m ruff check backend/src backend/tests'],
    'ruff': 'All checks passed, exit 0 (parent tool)',
    'browser': {'surface': 'Codex in-app browser, http://127.0.0.1:5174/',
                'observed': ['explicit demo owner login', 'dashboard payment 300.00/refund 150.00/net 150.00',
                             'complete plan saved, evaluation profit 2600.00/budget 900.00',
                             'sandbox_confirmed and no real activity', 'deterministic question with source',
                             'viewer has no cost column and no plan detail action',
                             'restart preserves session/plan'],
                'screenshot': '经营工作台.png', 'mobile_and_full_accessibility': 'not verified'},
    'process_lifecycle': 'Start-Local -> verified health/page; Stop-Local identity check -> restart successful, demo DB retained',
    'parent_observed_red': {'case': 'late old snapshot revision replaces default head',
                            'before': 'expected revision3, got2 (pytest1 failed)',
                            'after': 'same test1 passed; included in full suite'},
    'limitations': ['PostgreSQL/concurrency not run; local Docker daemon unavailable',
                    '01 actual identity/activity/checkout/ledger/slot barrier/actual feed not integrated',
                    'No paid model or production deployment', 'Synthetic data is not real business effect',
                    'One upstream TestClient HTTPX deprecation warning'],
    'code_sha256': fingerprints,
}
target = root / 'docs/06-acceptance/验证证据.json'
target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps({'backend': backend, 'frontend_passed': int(matched[1]),
                  'source_files': len(fingerprints)}, ensure_ascii=False))
