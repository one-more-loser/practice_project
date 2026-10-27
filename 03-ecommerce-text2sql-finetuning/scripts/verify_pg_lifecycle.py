"""INT-20261026-01: real, fresh, non-root PostgreSQL; never model/training.

Normal invocation retains live objects and waits for explicit stdin commands on
unexpected failure. --hold is the sole allowed auxiliary subprocess.
"""
from __future__ import annotations

import argparse
import atexit
import hashlib
import importlib.abc
import importlib.metadata
import inspect
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import tempfile
import time
import traceback
from datetime import datetime, timezone

ROOT=Path(__file__).resolve().parents[1]
RUN='INT-20261026-01'
PRIVATE=ROOT/'.runtime'/RUN
OUT=ROOT/'docs/07-iteration/ITER-20261025-01/review-output/integration-INT-20261026-01'
sys.path[:0]=[str(ROOT/'training'),str(ROOT/'sandbox/runner')]
FORBIDDEN=[]


class NoModels(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if (fullname.split('.')[0] in {'torch','transformers','peft','trl','datasets','runtime',
                                      'train_sft','train_grpo','pipeline','inference'}
                or fullname.startswith(('training.train_', 'training.pipeline','training.runtime'))):
            FORBIDDEN.append(fullname)
            raise RuntimeError('lifecycle guard: model/training imports forbidden')
sys.meta_path.insert(0,NoModels())

import pgserver
import pgserver._commands as pg_commands
import psycopg
import psutil
from owned_pg import OwnedSandbox
from oracle_errors import OracleEnvironmentError


def now():return datetime.now(timezone.utc).isoformat()
def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def dump(path, data):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_suffix(path.suffix+'.tmp')
    tmp.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n')
    os.replace(tmp,path)


def hold(path, exit_file):
    """A real cooperative handle in a separate, explicitly owned Python PID."""
    if os.geteuid()==0:raise RuntimeError('non-root required')
    resource=Path(path).resolve()
    if not str(resource).startswith('/tmp/03-int-20261026-01-'):
        raise RuntimeError('helper refuses non-run resource')
    data={'helper_pid':os.getpid(),'helper_created':psutil.Process().create_time(),
          'data_directory':str(resource),'started_at':now()}
    # Registered first, so it runs AFTER the dependency's atexit callback.
    def exited():
        data['exit_callback_at']=now()
        data['handle_pids_after_atexit']=server.global_process_id_list.get()
        dump(exit_file,data)
    atexit.register(exited)
    server=pgserver.get_server(resource,cleanup_mode=None)
    data['pg_pid']=server.get_pid()
    data['handle_pids_held']=server.global_process_id_list.get()
    print(json.dumps({'ready':True,**data}),flush=True)
    command=sys.stdin.readline().strip()
    if command!='release':raise RuntimeError('only explicit release is accepted')
    server.cleanup()
    data['released_at']=now()
    data['registry_removed']=resource not in pgserver.PostgresServer._instances
    print(json.dumps({'released':True,**data}),flush=True)


class Run:
    def __init__(self):
        if os.geteuid()==0:raise RuntimeError('non-root required; no sudo fallback')
        PRIVATE.mkdir(parents=True,exist_ok=True);OUT.mkdir(parents=True,exist_ok=True)
        self.record_path=PRIVATE/'run-lifecycle.json'
        if self.record_path.exists():raise RuntimeError('run already exists; inspect/resume rather than overwrite')
        self.started=time.perf_counter()
        self.root=Path(tempfile.mkdtemp(prefix='03-int-20261026-01-'))
        stat=self.root.stat()
        self.root_identity=(stat.st_dev,stat.st_ino)
        self.leases={};self.resources={};self.helpers=[];self.events=[];self.active_callbacks={}
        self.data={'run':RUN,'started_at':now(),'owner':{'uid':os.getuid(),'gid':os.getgid(),
            'python_pid':os.getpid(),'python_created':psutil.Process().create_time(),
            'run_root':str(self.root),'device':stat.st_dev,'inode':stat.st_ino},
            'events':self.events,'resources':self.resources,'pending_cleanup':[],
            'forbidden_calls':FORBIDDEN,'training_executed':False,'model_loaded':False}
        dump(PRIVATE/'config.json',{'run':RUN,'root':str(self.root),'connection':'owned UNIX socket only',
            'cases':['normal-evaluator','normal-direct','cooperative-holder','connection-close-proxy','reward-close'],
            'training':False,'models':False,'install':False})
        dump(self.root/'run-owner.json',self.data['owner'])
        self.snapshot_before()
        self.audit_callbacks()
        self.audit_commands()
        self.save()

    def save(self):
        self.data['elapsed_seconds']=round(time.perf_counter()-self.started,6)
        self.data['pending_cleanup']=[{'resource':name,'pgdata':str(lease.path),
            'pg_pid':(lease.identity or {}).get('pid'),'server_handle':lease.server is not None,
            'shutdown_receipt':lease.shutdown_receipt}
            for name,lease in self.leases.items() if not lease.closed]
        self.data['callbacks_retained']=list(self.active_callbacks.values())
        dump(self.record_path,self.data)
        dump(PRIVATE/'registry.json',{'owner':self.data['owner'],'resources':self.resources,
            'pending_cleanup':self.data['pending_cleanup'],'helpers':[
                {'pid':p.pid,'exited':p.poll() is not None,'returncode':p.poll()} for p in self.helpers]})

    def event(self,stage,**fields):
        row={'stage':stage,'at':now(),**fields};self.events.append(row);self.save()
        print(json.dumps(row,ensure_ascii=False),flush=True)

    def snapshot_before(self):
        manifest=ROOT/'docs/07-iteration/ITER-20261025-01/review-output/repair-R2/source-after-R2.json'
        entries=json.loads(manifest.read_text())
        for entry in entries:
            if sha(ROOT/entry['path'])!=entry['sha256']:raise RuntimeError('R2 source drift: '+entry['path'])
        dump(OUT/'source-before.json',entries)
        snapshots=OUT/'before';snapshots.mkdir(exist_ok=True)
        for name in ['owned_pg.py','eval_business.py','rewards.py']:
            (snapshots/name).write_bytes((ROOT/'training'/name).read_bytes())
        dep=[]
        for module in [inspect.getmodule(pgserver.PostgresServer),pg_commands]:
            path=Path(inspect.getfile(module));dep.append({'file':str(path),'sha256':sha(path)})
        for name in ['postgres','pg_ctl','initdb']:
            path=pg_commands.POSTGRES_BIN_PATH/name
            if not path.is_file() or not os.access(path,os.X_OK):raise RuntimeError('existing executable missing')
            dep.append({'file':str(path),'sha256':sha(path)})
        self.data['dependencies']={'pgserver':importlib.metadata.version('pgserver'),
            'psutil':importlib.metadata.version('psutil'),'psycopg':importlib.metadata.version('psycopg'),
            'files':dep,'version':subprocess.check_output([str(pg_commands.POSTGRES_BIN_PATH/'postgres'),'--version'],text=True).strip()}
        dump(OUT/'dependency-before.json',self.data['dependencies'])
        self.data['historical_indexes']=[{'path':str(p.relative_to(ROOT)),'sha256':sha(p)} for p in [
            manifest,manifest.parent/'evidence-index.json',manifest.parent.parent/'repair-R1/source-after-R1.json']]

    def audit_callbacks(self):
        original_register,original_unregister=atexit.register,atexit.unregister
        def owned(callback):
            instance=getattr(callback,'__self__',None)
            path=getattr(instance,'pgdata',None)
            return str(path) if isinstance(path,Path) and path.is_relative_to(self.root) else None
        def register(callback,*args,**kwargs):
            result=original_register(callback,*args,**kwargs)
            path=owned(callback)
            if path:self.active_callbacks[id(callback.__self__)]=path
            return result
        def unregister(callback):
            result=original_unregister(callback)
            if owned(callback):self.active_callbacks.pop(id(callback.__self__),None)
            return result
        # Observers forward the real registrations, never alter stop conditions.
        atexit.register,atexit.unregister=register,unregister

    def audit_commands(self):
        binary_dir=pg_commands.POSTGRES_BIN_PATH.resolve()
        def audit(event,args):
            if event=='subprocess.Popen':
                executable=Path(args[0]).resolve()
                argv=args[1]
                allowed=(executable.parent==binary_dir and executable.name in {'postgres','pg_ctl','initdb'})
                allowed=allowed or (executable==Path(sys.executable).resolve() and
                    len(argv)>=4 and str(Path(argv[1]).resolve())==str(Path(__file__).resolve()) and argv[2]=='--hold')
                if not allowed:
                    FORBIDDEN.append('unexpected_subprocess');raise RuntimeError('subprocess outside lifecycle allowlist')
            if event in {'os.chmod','os.chown'}:
                path=Path(args[0]).absolute()
                if not path.is_relative_to(self.root):
                    FORBIDDEN.append('shared_permission_change');raise RuntimeError('shared permissions forbidden')
        sys.addaudithook(audit)

    def create(self,name,parent=None):
        path=(parent or self.root)/name
        path.mkdir(mode=0o700,exist_ok=False)
        initial=list(path.iterdir())
        lease=OwnedSandbox(path);self.leases[name]=lease
        stat=path.stat()
        self.resources[name]={'pgdata':str(path),'initial_empty':not initial,
            'after_owner_metadata_empty':not list(path.iterdir()),'device':stat.st_dev,'inode':stat.st_ino,
            'owner':lease.owner,'real_pg':True,'closed':False}
        self.event('allocated',resource=name,pgdata=str(path))
        started=time.perf_counter()
        server=lease.start(pgserver);server.__enter__()
        info=server.get_postmaster_info();process=info.process
        socket_dir=info.socket_dir
        if socket_dir is None or not socket_dir.resolve().is_relative_to(self.root):
            raise RuntimeError('server socket outside owned run root')
        children=process.children(recursive=True)
        meta=self.resources[name]
        meta.update({'pg_pid':process.pid,'pg_created':process.create_time(),
            'pid_file_started':lease.identity['started'],'command':process.cmdline(),
            'socket_directory':str(socket_dir),'port':info.port,
            'socket_files':[str(p) for p in socket_dir.glob('.s.PGSQL.*')],
            'children':[{'pid':p.pid,'created':p.create_time()} for p in children],
            'startup_seconds':round(time.perf_counter()-started,6)})
        conn=psycopg.connect(host=str(socket_dir),port=info.port,dbname='postgres',user='postgres',
            sslmode='disable',connect_timeout=5,autocommit=True,options='-c statement_timeout=3000')
        lease.connection=conn
        check=conn.execute("SELECT 1, inet_server_addr() IS NULL, current_setting('listen_addresses')").fetchone()
        if check!=(1,True,''):raise RuntimeError('unexpected query or TCP listener')
        meta['select_1']=True;meta['tcp_listen_addresses']='';meta['unix_connection_confirmed']=True
        self.event('started-and-queried',resource=name,pg_pid=process.pid,elapsed=meta['startup_seconds'])
        return lease,conn

    def verify_closed(self,name):
        lease=self.leases[name];meta=self.resources[name]
        process=(lease.identity or {}).get('process')
        checks={'closed':lease.closed,'directory_removed':not lease.path.exists(),
            'owner_sidecar_removed':not lease.marker.exists(),
            'pg_pid_file_removed':not (lease.path/'postmaster.pid').exists(),
            'master_exited':process is not None and not process.is_running(),
            'registry_removed':lease.path not in pgserver.PostgresServer._instances,
            'atexit_unregistered':str(lease.path) not in self.active_callbacks.values(),
            'socket_files_removed':all(not Path(p).exists() for p in meta['socket_files'])}
        child_observations=[]
        for child in meta['children']:
            try:
                current=psutil.Process(child['pid'])
                same_identity=current.create_time()==child['created']
                exited=not same_identity or not current.is_running()
                child_observations.append({**child,'same_identity':same_identity,'exited':exited})
            except psutil.NoSuchProcess:child_observations.append({**child,'exited':True})
        checks['captured_children_exited']=all(row['exited'] for row in child_observations)
        meta.update({'closed':lease.closed,'cleanup_checks':checks,'children_after':child_observations,
            'shutdown_receipt':lease.shutdown_receipt})
        if not all(checks.values()):raise RuntimeError('post-close resource check failed: '+name)
        self.event('cleanup-verified',resource=name,checks=checks)

    def close(self,name,caller='direct'):
        lease=self.leases[name];started=time.perf_counter()
        if caller=='evaluator':
            from eval_business import _cleanup_owned_sandbox
            _cleanup_owned_sandbox(str(lease.path),lease.server,lease.connection,lease.owner,lease=lease)
        else:lease.close()
        self.resources[name]['close_seconds']=round(time.perf_counter()-started,6)
        self.verify_closed(name)
        lease.close()
        self.resources[name]['repeat_close_noop']=True

    def preserve_error(self,name,error,label):
        lease=self.leases[name]
        diagnostic=error.diagnostic_path
        copy=OUT/'failures'/f'{name}-{label}.json'
        copy.parent.mkdir(exist_ok=True)
        copy.write_bytes(diagnostic.read_bytes())
        self.event('expected-environment-error',resource=name,case=label,
            error_type=type(error).__name__,cause_type=type(error.__cause__).__name__,
            directory_preserved=lease.path.exists(),diagnostic=str(copy.relative_to(OUT)),
            shutdown_receipt=lease.shutdown_receipt,real_stop=True)

    def retire_diagnostic(self,name):
        lease=self.leases[name]
        path=lease.path.with_name(lease.path.name+'.cleanup-failure.json')
        if not lease.closed:raise RuntimeError('cannot retire diagnostic while cleanup pending')
        if path.exists():path.unlink()  # exact self-written sidecar already archived

    def holder_case(self):
        lease,conn=self.create('cooperative-holder')
        exit_file=PRIVATE/'helper-exit.json'
        child=subprocess.Popen([sys.executable,str(Path(__file__).resolve()),'--hold',str(lease.path),str(exit_file)],
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=(PRIVATE/'helper-stderr.log').open('w'),text=True,bufsize=1)
        self.helpers.append(child);self.save()
        if not select.select([child.stdout],[],[],15)[0]:raise RuntimeError('helper ready timed out; retained for diagnosis')
        ready=json.loads(child.stdout.readline())
        if ready.get('pg_pid')!=lease.identity['pid'] or child.pid not in lease.server.global_process_id_list.get():
            raise RuntimeError('helper identity/handle mismatch')
        self.event('auxiliary-ready',helper=ready)
        try:lease.close(conn)
        except OracleEnvironmentError as error:
            if lease.shutdown_receipt is not None or not lease.path.exists() or not lease.identity['process'].is_running():
                raise RuntimeError('holder refusal did not preserve live sandbox') from error
            self.preserve_error('cooperative-holder',error,'other-handle')
        else:raise RuntimeError('other handle was not rejected')
        child.stdin.write('release\n');child.stdin.flush()
        child.wait(timeout=10)
        if child.returncode!=0:raise RuntimeError('helper release failed')
        self.event('auxiliary-released',returncode=child.returncode,exit_observation=json.loads(exit_file.read_text()))
        self.close('cooperative-holder');self.retire_diagnostic('cooperative-holder')

    def close_error_case(self):
        lease,conn=self.create('connection-close-proxy')
        class ControlledCloseFailure:
            attempts=0
            def close(proxy):
                proxy.attempts+=1
                if proxy.attempts==1:raise OSError('controlled proxy close exception')
                conn.close()
        proxy=ControlledCloseFailure();primary=ValueError('controlled upstream exception')
        try:lease.close(proxy,original_error=primary)
        except OracleEnvironmentError as error:
            if error.original_error is not primary or lease.shutdown_receipt is None or not lease.path.exists():
                raise RuntimeError('close failure state mismatch') from error
            self.preserve_error('connection-close-proxy',error,'controlled-connection-close')
            self.events[-1]['substituted_component']='only connection.close proxy; real PG/stop/psutil/files'
        else:raise RuntimeError('proxy close exception was swallowed')
        self.close('connection-close-proxy');self.retire_diagnostic('connection-close-proxy')

    def reward_case(self):
        from rewards import SQLReward
        row={'id':'lifecycle-only','split':'train','expected_kind':'clarify'}
        oracle=SQLReward([row],ROOT/'sandbox/consumer01',{'schema_digest':'lifecycle-only'},fixtures=['fx_a','fx_b'])
        parent=self.root/'reward-parent';parent.mkdir(mode=0o700)
        st=parent.stat();oracle.directory=str(parent);oracle.directory_identity=(st.st_dev,st.st_ino)
        for fixture in ['fx_a','fx_b']:
            lease,conn=self.create(fixture,parent)
            oracle.sandboxes[fixture]=lease;oracle.servers.append(lease.server);oracle.connections[fixture]=conn
        oracle.ready=True
        started=time.perf_counter();oracle.close();elapsed=time.perf_counter()-started
        for fixture in ['fx_a','fx_b']:self.verify_closed(fixture)
        oracle.close()
        if parent.exists() or oracle.servers or oracle.connections or oracle.sandboxes:
            raise RuntimeError('reward close left owned references')
        self.event('reward-caller-close',elapsed_seconds=round(elapsed,6),parent_removed=True,
            real_close=True,boundary='caller close integration only; explicit real leases, no __enter__/setup_fixture/scoring/model')

    def execute(self):
        self.create('normal-evaluator');self.close('normal-evaluator','evaluator')
        self.create('normal-direct');self.close('normal-direct')
        self.holder_case();self.close_error_case();self.reward_case()
        self.finish()

    def finish(self):
        if any(not lease.closed for lease in self.leases.values()):raise RuntimeError('unclosed resources')
        if self.active_callbacks:raise RuntimeError('owned atexit callbacks remain')
        if any(proc.poll() is None for proc in self.helpers):raise RuntimeError('owned auxiliary remains')
        current=self.root.stat()
        if (current.st_dev,current.st_ino)!=self.root_identity:raise RuntimeError('run root identity changed')
        (self.root/'run-owner.json').unlink();self.root.rmdir()
        self.data['status']='completed_awaiting_coordinator_reverification'
        self.data['finished_at']=now();self.data['run_root_removed']=not self.root.exists()
        self.data['maximum_observed_close_seconds']=max(
            [m.get('close_seconds',0) for m in self.resources.values()]+[0])
        self.event('completed',resources=len(self.resources),root_removed=not self.root.exists())

    def failed(self,error):
        self.data['status']='unexpected_failure_retained'
        self.data['failure_type']=type(error).__name__
        self.data['failure_chain']=traceback.format_exc()
        dump(OUT/'unexpected-RED.json',self.data)
        (PRIVATE/'unexpected-RED.log').write_text(traceback.format_exc())
        self.event('unexpected-failure',error_type=type(error).__name__,pending=self.data['pending_cleanup'])


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--hold',nargs=2)
    args=parser.parse_args()
    if args.hold:
        hold(*args.hold);return
    run=Run()
    try:run.execute()
    except BaseException as error:
        run.failed(error)
        # Retain Python objects/registry for a scoped repair instead of exiting
        # and abandoning a running server. No automatic kill or recursive reset.
        print('PAUSED: inspect registry; commands: status, retry <resource>, finish, exit-retaining',flush=True)
        while True:
            command=sys.stdin.readline().strip()
            if not command:
                time.sleep(1);continue
            try:
                if command=='status':run.save();print(json.dumps(run.data),flush=True)
                elif command.startswith('retry '):
                    import importlib,owned_pg
                    importlib.reload(owned_pg)
                    name=command.split(' ',1)[1]
                    run.leases[name].__class__=owned_pg.OwnedSandbox
                    run.close(name);run.retire_diagnostic(name)
                elif command=='finish':run.finish();break
                elif command=='exit-retaining':run.save();break
                else:print('unrecognized command',flush=True)
            except BaseException as retry_error:run.failed(retry_error)
    if run.data.get('pending_cleanup'):raise SystemExit(2)


if __name__=='__main__':main()
