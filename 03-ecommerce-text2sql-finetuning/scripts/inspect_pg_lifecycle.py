"""Read-only checks after the lifecycle driver's process exited."""
import hashlib
import json
from pathlib import Path
import sys
from datetime import datetime, timezone
import psutil

ROOT=Path(__file__).resolve().parents[1]
PRIVATE=ROOT/'.runtime/INT-20261026-01'
OUT=ROOT/'docs/07-iteration/ITER-20261025-01/review-output/integration-INT-20261026-01'
raw=json.loads((PRIVATE/'run-lifecycle.json').read_text())
assert raw['status']=='completed_awaiting_coordinator_reverification'
assert not raw['pending_cleanup'] and not raw['forbidden_calls']

def process_state(pid,created=None):
    try:
        process=psutil.Process(pid)
        actual=process.create_time()
        same=created is None or actual==created
        return {'pid':pid,'recorded_created':created,'present':True,'actual_created':actual,
            'same_identity':same,'original_exited':not same or not process.is_running(),
            'scope':'recorded PID+creation time' if created is not None else 'PID absence corroboration only; real wait receipt is primary'}
    except psutil.NoSuchProcess:
        return {'pid':pid,'recorded_created':created,'present':False,'original_exited':True}

rows=[process_state(raw['owner']['python_pid'],raw['owner']['python_created'])]
helpers=[event['helper'] for event in raw['events'] if event['stage']=='auxiliary-ready']
rows += [process_state(row['helper_pid'],row['helper_created']) for row in helpers]
directories=[]
for name,resource in raw['resources'].items():
    rows.append(process_state(resource['pg_pid'],resource['pg_created']))
    rows += [process_state(row['pid'],row['created']) for row in resource['children']]
    known={row['pid'] for row in resource['children']}|{resource['pg_pid']}
    rows += [process_state(pid) for pid in resource['shutdown_receipt']['tracked_pids_waited'] if pid not in known]
    path=Path(resource['pgdata'])
    directories.append({'resource':name,'pgdata':str(path),'exists':path.exists(),
        'pid_file_exists':(path/'postmaster.pid').exists(),
        'socket_exists':any(Path(p).exists() for p in resource['socket_files'])})
assert all(row['original_exited'] for row in rows),rows
assert not any(row['exists'] or row['pid_file_exists'] or row['socket_exists'] for row in directories)
assert not Path(raw['owner']['run_root']).exists()

before=json.loads((OUT/'source-before.json').read_text())
hashes=[]
for row in before:
    current=hashlib.sha256((ROOT/row['path']).read_bytes()).hexdigest()
    hashes.append({'path':row['path'],'unchanged':current==row['sha256'],'sha256':current})
assert all(row['unchanged'] for row in hashes),'unexpected source/data drift before documentation update'
for row in raw['historical_indexes']:
    assert hashlib.sha256((ROOT/row['path']).read_bytes()).hexdigest()==row['sha256']
for row in raw['dependencies']['files']:
    assert hashlib.sha256(Path(row['file']).read_bytes()).hexdigest()==row['sha256']

report={'checked_at':datetime.now(timezone.utc).isoformat(),'read_only':True,'processes':rows,
    'directories':directories,'run_root_removed':True,'source_hashes':hashes,
    'dependencies_and_historical_indexes_unchanged':True,'pending_cleanup':[],
    'signal_sent':False,'database_connected':False,'training_executed':False}
(OUT/'post-exit-inspection.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
print(json.dumps({'process_observations':len(rows),'directories':len(directories),
    'source_hashes_unchanged':len(hashes),'pending_cleanup':[],'real_pg_started_by_inspector':False}))
