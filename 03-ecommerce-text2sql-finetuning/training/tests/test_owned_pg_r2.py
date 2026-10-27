"""R2 real installed methods + synthetic files/process primitives; never PG."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT/'training'), str(ROOT/'sandbox/runner')]
import owned_pg
from owned_pg import OwnedSandbox
from oracle_errors import OracleEnvironmentError
from pgserver import PostgresServer
import pgserver._commands as commands
import pgserver.postgres_server as dependency
import rewards
from rewards import SQLReward


class Handles:
    def __init__(self, values): self.values = values
    def get(self): return self.values.copy()
    def get_and_remove(self, value):
        before = self.get()
        if value in self.values: self.values.remove(value)
        return before


@contextmanager
def scenario(parent=None, name='owned'):
    with tempfile.TemporaryDirectory(prefix='03-r2-files-only-') as temporary:
        path = Path(parent or temporary)/name
        path.mkdir()
        lease = OwnedSandbox(path)
        (path/'postmaster.pid').write_text(f'300001\n{path}\n1000\n')
        process, child = MagicMock(), MagicMock()
        process.pid, child.pid = 300001, 300002
        process.create_time.return_value = 1000.25
        child.create_time.return_value = 1001.0
        process.exe.return_value = str(commands.POSTGRES_BIN_PATH/'postgres')
        process.cmdline.return_value = ['postgres','-D',str(path)]
        process.children.return_value = [child]
        state = {'alive': True}
        process.is_running.side_effect = child.is_running.side_effect = lambda: state['alive']
        server = object.__new__(PostgresServer)  # deliberately no __init__/atexit/start
        server.pgdata, server.cleanup_mode, server.system_user = path, None, None
        server._lock, server._count = MagicMock(), 1
        server.global_process_id_list = Handles([os.getpid()])
        server._instances = {path: server}
        server._postmaster_info = SimpleNamespace(process=process)
        factory = SimpleNamespace(get_server=MagicMock(return_value=server))
        def stop(*args, **kwargs):
            state['alive'] = False
            (path/'postmaster.pid').unlink()
            return ''
        with patch.object(commands,'pg_ctl',side_effect=stop) as ctl:
            lease.start(factory)
            yield SimpleNamespace(path=path, lease=lease, server=server, process=process,
                child=child, state=state, ctl=ctl, factory=factory)


class OwnedPGR2(unittest.TestCase):
    def assert_preserved(self, case, **kwargs):
        with patch.object(owned_pg.shutil,'rmtree') as remove:
            with self.assertRaises(OracleEnvironmentError) as caught:
                case.lease.close(**kwargs)
        remove.assert_not_called()
        self.assertTrue(case.path.exists())
        self.assertIs(caught.exception.server_handle, case.server)
        return caught.exception

    def test_real_dependency_other_handle_returns_none_without_stop(self):
        with scenario() as c, patch.object(dependency,'pg_ctl') as dep_stop, \
                patch.object(dependency.shutil,'rmtree') as remove:
            c.server.global_process_id_list = Handles([os.getpid(), 300003])
            self.assertIsNone(c.server.cleanup())
            dep_stop.assert_not_called();remove.assert_not_called()
            self.assertTrue(c.state['alive'])
            error = self.assert_preserved(c)
            self.assertIn('handles', str(error.__cause__))
            c.ctl.assert_not_called()

    def test_real_dependency_already_released_returns_none_not_stop(self):
        with scenario() as c:
            c.server.global_process_id_list = Handles([])
            self.assertIsNone(c.server.cleanup())
            self.assert_preserved(c)
            c.ctl.assert_not_called()

    def test_other_handle_blocks_signals_and_deletion(self):
        with scenario() as c:
            c.server.global_process_id_list.values.append(300003)
            self.assert_preserved(c);c.ctl.assert_not_called()
            c.process.terminate.assert_not_called();c.process.kill.assert_not_called()

    def test_dependency_false_after_stop_retains_on_repeated_close(self):
        with scenario() as c, patch.object(c.server,'cleanup',return_value=False):
            self.assert_preserved(c)
            self.assertIsNone(c.lease.shutdown_receipt)
            self.assert_preserved(c)
            self.assertEqual(c.ctl.call_count,1)

    def test_dependency_cleanup_raises_keeps_original_cause(self):
        cause=ValueError('business failure')
        with scenario() as c, patch.object(c.server,'cleanup',side_effect=OSError('release failed')):
            error=self.assert_preserved(c,original_error=cause)
            self.assertIs(error.original_error,cause)
            self.assertIsInstance(error.__cause__,OSError)

    def test_waited_owned_exit_then_remove_and_repeat_noop(self):
        with scenario() as c:
            c.lease.close()
            self.assertFalse(c.path.exists())
            self.assertTrue(c.lease.closed)
            c.process.wait.assert_called_once_with(timeout=2)
            c.child.wait.assert_called_once_with(timeout=2)
            c.lease.close();self.assertEqual(c.ctl.call_count,1)
            self.assertIsNone(c.factory.get_server.call_args.kwargs['cleanup_mode'])

    def test_stop_return_without_exit_is_not_shutdown(self):
        with scenario() as c:
            c.ctl.side_effect=None;c.ctl.return_value=''
            self.assert_preserved(c)

    def test_child_still_alive_keeps_pgdata(self):
        with scenario() as c:
            c.child.is_running.side_effect=None;c.child.is_running.return_value=True
            self.assert_preserved(c)

    def test_unknown_process_identity_no_stop(self):
        with scenario() as c:
            c.process.exe.return_value='/unknown/postgres'
            self.assert_preserved(c);c.ctl.assert_not_called()

    def test_reused_pid_no_stop(self):
        with scenario() as c:
            c.process.create_time.return_value=1001.25
            self.assert_preserved(c);c.ctl.assert_not_called()

    def test_absent_identity_not_inferred_from_pid_exists(self):
        with scenario() as c, patch('psutil.pid_exists',return_value=False) as exists:
            c.lease.identity=None
            self.assert_preserved(c);c.ctl.assert_not_called();exists.assert_not_called()

    def test_parent_process_not_running_cannot_prove_children_gone(self):
        with scenario() as c:
            c.process.is_running.side_effect=None;c.process.is_running.return_value=False
            self.assert_preserved(c);c.ctl.assert_not_called()

    def test_changed_pidfile_no_signal(self):
        with scenario() as c:
            (c.path/'postmaster.pid').write_text(f'300004\n{c.path}\n1000\n')
            self.assert_preserved(c);c.ctl.assert_not_called()

    def test_marker_changed_no_stop(self):
        with scenario() as c:
            c.lease.marker.write_text('{}')
            self.assert_preserved(c);c.ctl.assert_not_called()

    def test_extra_local_enter_handle_no_stop(self):
        with scenario() as c:
            c.server._count=2
            self.assert_preserved(c);c.ctl.assert_not_called()

    def test_stop_error_no_fallback_kill(self):
        with scenario() as c:
            c.ctl.side_effect=TimeoutError('waited stop timed out')
            self.assert_preserved(c)
            c.process.terminate.assert_not_called();c.process.kill.assert_not_called()

    def test_connection_close_error_still_attempts_verified_stop(self):
        with scenario() as c:
            conn=MagicMock();conn.close.side_effect=OSError('connection close')
            error=self.assert_preserved(c,connection=conn)
            c.ctl.assert_called_once()
            self.assertTrue(error.diagnostic_path.is_file())
            self.assertTrue(json.loads(error.diagnostic_path.read_text())['shutdown_confirmed'])
            self.assertIs(error.connection_handle,conn)

    def test_rmtree_failure_not_success_and_receipt_can_retry(self):
        with scenario() as c, patch.object(owned_pg.shutil,'rmtree',side_effect=OSError('delete denied')):
            with self.assertRaises(OracleEnvironmentError):c.lease.close()
            self.assertFalse(c.lease.closed);self.assertTrue(c.path.exists())

    def test_diagnostic_write_failure_retains_exception_and_directory(self):
        with scenario() as c, patch.object(Path,'write_text',side_effect=OSError('disk full')):
            c.lease.identity=None
            error=self.assert_preserved(c)
            self.assertIsInstance(error.diagnostic_write_error,OSError)
            self.assertIsInstance(error.__cause__,RuntimeError)

    def test_creation_failure_no_handle_retains_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'creation-failure';path.mkdir()
            lease=OwnedSandbox(path)
            factory=SimpleNamespace(get_server=MagicMock(side_effect=ValueError('create failure')))
            with self.assertRaises(ValueError):lease.start(factory)
            with self.assertRaises(OracleEnvironmentError) as caught:lease.close()
            self.assertTrue(path.exists());self.assertIsNone(caught.exception.server_handle)
            self.assertIsNone(factory.get_server.call_args.kwargs['cleanup_mode'])

    def test_reward_partial_fixture_failure_keeps_failed_handles_parent(self):
        with tempfile.TemporaryDirectory() as parent, scenario(parent,'fx_a') as a, scenario(parent,'fx_b') as b:
            # Each scenario owns its ctl stub. One dispatcher keeps both fake
            # processes distinct; all process operations remain synthetic.
            def stop(*args,pgdata,**kwargs):
                c=a if pgdata==a.path else b
                c.state['alive']=False;(c.path/'postmaster.pid').unlink();return ''
            b.server.global_process_id_list.values.append(300003)
            reward=object.__new__(SQLReward)
            reward.ready=True;reward.directory=parent
            st=Path(parent).stat();reward.directory_identity=(st.st_dev,st.st_ino)
            reward.servers=[a.server,b.server];reward.server=None
            reward.connections={};reward.sandboxes={'fx_a':a.lease,'fx_b':b.lease}
            primary=ValueError('original rollout')
            with patch.object(commands,'pg_ctl',side_effect=stop):
                with self.assertRaises(OracleEnvironmentError) as caught:reward.close(original_error=primary)
            self.assertIs(caught.exception.original_error,primary)
            self.assertEqual(caught.exception.failed_fixtures,('fx_b',))
            self.assertEqual(reward.servers,[b.server]);self.assertEqual(reward.directory,parent)
            self.assertFalse(a.path.exists());self.assertTrue(b.path.exists())
            self.assertEqual(set(reward.sandboxes),{'fx_b'})

    def test_reward_legacy_unknown_handle_not_cleaned_or_erased(self):
        with tempfile.TemporaryDirectory() as parent:
            parent=str(Path(parent)/'unknown');Path(parent).mkdir()
            reward=object.__new__(SQLReward);server=MagicMock()
            reward.directory=parent;reward.servers=[server];reward.server=None
            reward.connections={};reward.ready=True
            with self.assertRaises(OracleEnvironmentError):reward.close()
            server.cleanup.assert_not_called()
            self.assertTrue(Path(reward.directory).exists());self.assertEqual(reward.servers,[server])

    def test_metadata_does_not_make_initdb_directory_nonempty(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'empty';path.mkdir()
            lease=OwnedSandbox(path)
            self.assertEqual(list(path.iterdir()),[])
            self.assertEqual(lease.marker.parent,path.parent)

    def test_pg_ctl_false_is_not_valid_completion(self):
        with scenario() as c:
            c.ctl.side_effect=None;c.ctl.return_value=False
            self.assert_preserved(c)

    def test_wait_timeout_is_not_success(self):
        with scenario() as c:
            c.child.wait.side_effect=TimeoutError('child has not exited')
            self.assert_preserved(c)

    def test_handle_release_normal_return_but_remaining_handles_retains(self):
        with scenario() as c:
            def release():
                c.server.global_process_id_list.values=[300003]
                return None
            with patch.object(c.server,'cleanup',side_effect=release):
                self.assert_preserved(c)
            self.assertIsNone(c.lease.shutdown_receipt)

    def test_reward_creation_failure_keeps_partial_fixture_lease_and_primary(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent=Path(tmp)/'parent';parent.mkdir()
            path=parent/'fx_a';path.mkdir()
            lease=OwnedSandbox(path)
            reward=object.__new__(SQLReward);reward.server=None;reward.ready=False
            reward.directory=str(parent);reward.connections={};reward.servers=[]
            reward.sandboxes={'fx_a':lease}
            primary=ValueError('constructor error')
            with self.assertRaises(OracleEnvironmentError) as caught:reward.close(original_error=primary)
            self.assertIs(caught.exception.original_error,primary)
            self.assertEqual(caught.exception.failed_fixtures,('fx_a',))
            self.assertTrue(path.exists());self.assertIs(reward.sandboxes['fx_a'],lease)
            self.assertTrue(caught.exception.diagnostic_path.exists())

    def test_reward_parent_unknown_child_cannot_be_recursively_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent=Path(tmp)/'parent';parent.mkdir();(parent/'unknown').mkdir()
            reward=object.__new__(SQLReward);reward.server=None;reward.ready=False
            reward.directory=str(parent);reward.connections={};reward.servers=[];reward.sandboxes={}
            st=parent.stat();reward.directory_identity=(st.st_dev,st.st_ino)
            with self.assertRaises(OracleEnvironmentError):reward.close()
            self.assertTrue((parent/'unknown').exists())

    def test_reward_enter_creation_failure_preserves_original_and_failed_fixture(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent=Path(tmp)/'reward';parent.mkdir()
            row={'id':'clarify','split':'train','expected_kind':'clarify'}
            reward=SQLReward([row],ROOT/'sandbox/consumer01',{'schema_digest':'toy'})
            primary=ValueError('synthetic constructor failure')
            factory=SimpleNamespace(get_server=MagicMock(side_effect=primary))
            with patch.object(rewards.tempfile,'mkdtemp',return_value=str(parent)), \
                 patch.object(rewards.importlib,'import_module',return_value=SimpleNamespace()), \
                 patch.object(rewards,'validator01',return_value=[]), \
                 patch.dict(sys.modules,{'pgserver':factory}):
                with self.assertRaises(OracleEnvironmentError) as caught:reward.__enter__()
            self.assertIs(caught.exception.original_error,primary)
            self.assertEqual(set(reward.sandboxes),{'fx_a'})
            self.assertTrue((parent/'fx_a').exists())
            self.assertFalse(reward.ready)

    def test_saved_receipt_does_not_allow_restarted_server_deletion(self):
        with scenario() as c:
            conn=MagicMock();conn.close.side_effect=OSError('close failure')
            self.assert_preserved(c,connection=conn)
            self.assertIsNotNone(c.lease.shutdown_receipt)
            conn.close.side_effect=None
            (c.path/'postmaster.pid').write_text('new server identity')
            self.assert_preserved(c)
            self.assertEqual(c.ctl.call_count,1)

    def test_reward_repeated_normal_close_noop(self):
        reward=object.__new__(SQLReward)
        reward.directory=None;reward.servers=[];reward.connections={};reward.sandboxes={};reward.server=None
        reward.close();reward.close()


if __name__=='__main__':unittest.main()
