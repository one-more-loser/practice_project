"""Owned PG lifecycle. pgserver handle release is NOT a shutdown receipt.

Only this module removes owned PGDATA. The dependency uses cleanup_mode=None,
including its constructor's atexit hook. No terminate/kill fallback is used.
Real PostgreSQL stop/wait integration remains a separately authorized check.
"""
from __future__ import annotations

import atexit
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import socket
import uuid

from oracle_errors import OracleEnvironmentError


def cleanup_error(path, server, connection, owner, failures, original_error=None,
                  shutdown_confirmed=False, service_identity=None):
    """Keep live handles and every cause, even when the sidecar cannot be written."""
    path = Path(path)
    error = OracleEnvironmentError(f'owned sandbox cleanup unconfirmed or failed; pgdata={path}')
    error.owner, error.server_handle, error.connection_handle = owner, server, connection
    error.original_error = original_error
    error.service_identity = service_identity
    error.cleanup_errors = tuple(exc for _, exc in failures)
    error.diagnostic_path = path.with_name(path.name + '.cleanup-failure.json')
    diagnostic = {'owner': owner, 'postgres_identity': service_identity,
        'shutdown_confirmed': shutdown_confirmed,
        'data_directory_preserved': path.exists(),
        'original_error_type': type(original_error).__name__ if original_error else None,
        'failures': [{'stage': stage, 'type': type(exc).__name__}
                     for stage, exc in failures],
        'server_handle_available': server is not None,
        'manual_action': 'retain PGDATA; inspect owner, PID identity, handle list and server log; no automatic kill'}
    try:
        temporary = error.diagnostic_path.with_suffix('.json.tmp-' + uuid.uuid4().hex)
        temporary.write_text(json.dumps(diagnostic, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        os.replace(temporary, error.diagnostic_path)
    except BaseException as exc:
        error.diagnostic_write_error = exc
        error.add_note('diagnostic write failed: ' + type(exc).__name__)
    cause = failures[-1][1] if failures else original_error
    if cause is not None:
        raise error from cause
    raise error


class OwnedSandbox:
    """One fresh project directory, one server identity, explicit stop then removal.

    Assumes cooperative pgserver users under its interprocess lock, not hostile
    local processes replacing PID files. Ambiguous or lost identity retains data.
    """
    def __init__(self, path):
        self.path = Path(path).absolute()
        if self.path.is_symlink() or self.path.resolve() != self.path:
            raise OracleEnvironmentError('owned PGDATA must be a direct absolute directory')
        stat = self.path.stat()
        self.directory_identity = (stat.st_dev, stat.st_ino)
        self.owner = {'data_directory': str(self.path), 'pid': os.getpid(),
            'host': socket.gethostname(), 'token': uuid.uuid4().hex,
            'created_at_utc': datetime.now(timezone.utc).isoformat()}
        # initdb requires empty PGDATA. Ownership metadata lives beside it.
        self.marker = self.path.with_name(self.path.name + '.03-owner.json')
        with self.marker.open('x', encoding='utf-8') as file:
            json.dump(self.owner, file)
        self.server = self.connection = None
        self.identity = None
        self.closed = False
        self.shutdown_receipt = None

    def verify_directory(self):
        stat = self.path.stat()
        if (self.path.is_symlink() or self.path.resolve() != self.path
                or (stat.st_dev, stat.st_ino) != self.directory_identity
                or json.loads(self.marker.read_text()) != self.owner):
            raise RuntimeError('owned directory identity changed')

    def start(self, pgserver):
        self.verify_directory()
        # None prevents both implicit deletion and dependency fallback signals,
        # including atexit after a constructor that raises without returning.
        self.server = pgserver.get_server(self.path, cleanup_mode=None)
        if self.server.cleanup_mode is not None:
            raise RuntimeError('unexpected existing server cleanup mode')
        self.identity = self._capture_identity()
        return self.server

    def _pid_record(self):
        lines = (self.path / 'postmaster.pid').read_text().splitlines()
        if len(lines) < 3:
            raise RuntimeError('incomplete postmaster identity')
        return int(lines[0]), Path(lines[1]).resolve(), int(lines[2])

    def _capture_identity(self):
        from pgserver._commands import POSTGRES_BIN_PATH
        server = self.server
        self.verify_directory()
        if Path(server.pgdata).resolve() != self.path or server.cleanup_mode is not None:
            raise RuntimeError('server is not the owned non-destructive handle')
        process = server.get_postmaster_info().process
        pid, directory, started = self._pid_record()
        if process is None or process.pid != pid or pid <= 1 or pid == os.getpid() or directory != self.path:
            raise RuntimeError('postmaster PID/directory identity mismatch')
        created = process.create_time()
        command = process.cmdline()
        executable = str((POSTGRES_BIN_PATH / ('postgres.exe' if os.name == 'nt' else 'postgres')).resolve())
        if (not process.is_running() or abs(created - started) > 2
                or str(Path(process.exe()).resolve()) != executable
                or not any(command[i] == '-D' and Path(command[i+1]).resolve() == self.path
                           for i in range(len(command)-1))):
            raise RuntimeError('postmaster process identity unverifiable')
        return {'pid': pid, 'created': created, 'started': started,
                'executable': executable, 'process': process}

    def _stop_and_release(self):
        from pgserver._commands import pg_ctl
        server = self.server
        if server is None or self.identity is None:
            raise RuntimeError('no captured server identity; retain directory')
        # Do not use cleanup(stop/delete): its fallback kill lacks a final wait.
        with server._lock:
            self.verify_directory()
            if server.cleanup_mode is not None or server.global_process_id_list.get() != [os.getpid()]:
                raise RuntimeError('other/missing server handles; shutdown unconfirmed')
            if not isinstance(server._count, int) or server._count > 1:
                raise RuntimeError('additional local context handles; retain directory')
            current = self._capture_identity()
            if any(current[key] != self.identity[key] for key in ('pid','created','started','executable')):
                raise RuntimeError('postmaster identity changed or PID reused')
            master = self.identity['process']
            children = master.children(recursive=True)
            tracked = [(proc, proc.create_time()) for proc in [master, *children]]
            # Successful waited fast shutdown is the PostgreSQL protocol proof;
            # captured parent + descendants and PID-file removal are corroboration.
            # Never substitute pid_exists(False), cleanup(None), or a kill return.
            stop_output = pg_ctl(['-w', '-t', '10', '-m', 'fast', 'stop'], pgdata=self.path,
                                 user=server.system_user, timeout=15)
            if not isinstance(stop_output, str):
                raise RuntimeError('invalid pg_ctl completion protocol')
            for process, created in tracked:
                # psutil Process retains PID+creation-time identity. Do not signal
                # or wait on a reused PID; it is an ambiguous teardown instead.
                if process.is_running() and process.create_time() != created:
                    raise RuntimeError('tracked process PID reused')
                process.wait(timeout=2)
                if process.is_running():
                    raise RuntimeError('tracked process still alive')
            if (self.path / 'postmaster.pid').exists():
                raise RuntimeError('postmaster PID file remains after stop')
            self.verify_directory()
            receipt = {'pg_ctl_waited_fast_stop': True,
                'postmaster_pid': self.identity['pid'], 'postmaster_created': self.identity['created'],
                'tracked_pids_waited': [p.pid for p, _ in tracked], 'pid_file_removed': True}
        # cleanup() acquires the same non-reentrant lock. It only releases this
        # handle now; None is not itself evidence of shutdown.
        if server.cleanup() is False:
            raise RuntimeError('dependency refused handle release')
        with server._lock:
            self.verify_directory()
            if server.global_process_id_list.get() != []:
                raise RuntimeError('handles appeared during release; retain directory')
            if server._instances.get(self.path) is not None:
                raise RuntimeError('server registry still retains a handle')
            # Lock held through deletion, excluding cooperative constructors.
            # No dependency atexit callback may later signal/delete this PGDATA.
            atexit.unregister(server._cleanup)
            return receipt

    def close(self, connection=None, original_error=None):
        if self.closed:
            return
        if connection is not None:
            self.connection = connection
        failures = []
        if self.connection is not None:
            try:
                self.connection.close()
                self.connection = None
            except BaseException as exc:
                failures.append(('connection_close', exc))
        try:
            # If an earlier attempt stopped/released successfully but connection
            # close failed, never rerun cleanup against an already released handle.
            if self.shutdown_receipt is None:
                receipt = self._stop_and_release()
                if not isinstance(receipt, dict) or receipt.get('pg_ctl_waited_fast_stop') is not True:
                    raise RuntimeError('missing explicit shutdown receipt')
                self.shutdown_receipt = receipt
        except BaseException as exc:
            failures.append(('server_stop_or_release', exc))
        if not failures:
            try:
                with self.server._lock:
                    self.verify_directory()
                    if self.server.global_process_id_list.get() != []:
                        raise RuntimeError('new handles before removal')
                    if (self.path / 'postmaster.pid').exists():
                        raise RuntimeError('postmaster PID file reappeared before removal')
                    shutil.rmtree(str(self.path))
                    self.marker.unlink()
                self.closed = True
                return
            except BaseException as exc:
                failures.append(('residue_removal', exc))
        cleanup_error(self.path, self.server, self.connection, self.owner, failures,
                      original_error, shutdown_confirmed=self.shutdown_receipt is not None,
                      service_identity={key: value for key, value in (self.identity or {}).items()
                                        if key != 'process'})
