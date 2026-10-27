"""COORD-03-R1-001: real main with Mock PG handles and a deletion spy only."""
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT/'training'),str(ROOT/'sandbox/runner')]
import eval_business
from owned_pg import OwnedSandbox
from oracle_errors import OracleEnvironmentError


class BusinessCleanupR1(unittest.TestCase):
    def run_case(self, failure=None, cleanup_fails=False, close_fails=False,
                 remove_fails=False, false_confirmation=False):
        with tempfile.TemporaryDirectory(prefix='03-r1-mock-test-') as parent:
            owned=Path(parent)/'fake-owned-pgdata'
            owned.mkdir()
            (owned/'synthetic-sentinel').write_text('NOT PostgreSQL data')
            server, connection = MagicMock(), MagicMock()
            server.global_process_id_list.get.return_value=[]
            server.cleanup.return_value = False if false_confirmation else None
            primary=ValueError('synthetic '+str(failure)+' failure')
            cleanup_error=RuntimeError('simulated shutdown unconfirmed')
            close_error=RuntimeError('simulated connection close failure')
            remove_error=OSError('simulated residue removal failure')
            if cleanup_fails: server.cleanup.side_effect=cleanup_error
            if close_fails: connection.close.side_effect=close_error
            create=MagicMock(return_value=server)
            connect=MagicMock(return_value=connection)
            setup=MagicMock()
            if failure=='create':create.side_effect=primary
            if failure=='enter':server.__enter__.side_effect=primary
            if failure=='connect':connect.side_effect=primary
            if failure=='setup':setup.side_effect=primary
            fake_run=SimpleNamespace(psycopg=SimpleNamespace(connect=connect),setup_fixture=setup)
            fake_pg=SimpleNamespace(get_server=create)
            def start(lease, factory):
                lease.server=factory.get_server(lease.path,cleanup_mode=None)
                return lease.server
            def verified_stop(lease):
                # This suite tests main's exception plumbing; process proof is
                # explicitly substituted, NOT inferred from MagicMock.cleanup.
                if lease.server is None:raise RuntimeError('no captured handle')
                if lease.server.cleanup() is False:raise RuntimeError('release refused')
                return {'pg_ctl_waited_fast_stop':True,'test_only_mock_receipt':True}
            with patch.dict(sys.modules,{'run':fake_run,'pgserver':fake_pg}), \
                 patch.object(OwnedSandbox,'start',new=start), \
                 patch.object(OwnedSandbox,'_stop_and_release',new=verified_stop), \
                 patch.object(eval_business,'load_jsonl',return_value=[]), \
                 patch.object(eval_business,'load_predictions',return_value=[]), \
                 patch.object(eval_business,'validator01',return_value=[]), \
                 patch.object(eval_business,'evaluation_identity',return_value={'mock':'no model'}), \
                 patch.object(eval_business,'_write') as write, \
                 patch.object(eval_business.tempfile,'mkdtemp',return_value=str(owned)), \
                 patch.object(eval_business.shutil,'rmtree') as remove, \
                 patch.object(sys,'argv',['eval','--predictions','unused.json',
                    '--consumer01-root',str(ROOT/'sandbox/consumer01')]):
                if remove_fails: remove.side_effect=remove_error
                caught=None
                try:eval_business.main()
                except BaseException as exc:caught=exc
                if cleanup_fails or close_fails or failure=='create' or false_confirmation:
                    self.assertFalse(remove.called,'unconfirmed shutdown must never delete PGDATA')
                else:
                    remove.assert_called_once_with(str(owned))
                self.assertEqual(server.cleanup.call_count,0 if failure=='create' else 1)
                self.assertEqual(connection.close.call_count,0 if failure in {'create','enter','connect'} else 1)
                if failure or cleanup_fails or close_fails or remove_fails or false_confirmation:
                    self.assertIsInstance(caught,OracleEnvironmentError)
                    write.assert_not_called()
                else:
                    self.assertIsNone(caught)
                    self.assertFalse(write.call_args.args[1]['summary']['oracle_error'])
                if cleanup_fails or close_fails or remove_fails or failure=='create' or false_confirmation:
                    self.assertEqual(caught.owner['data_directory'],str(owned))
                    self.assertGreater(caught.owner['pid'],0)
                    self.assertTrue(caught.diagnostic_path.is_file())
                    diagnostic=json.loads(caught.diagnostic_path.read_text())
                    self.assertEqual(diagnostic['owner'],caught.owner)
                    self.assertEqual(diagnostic['shutdown_confirmed'],not(cleanup_fails or failure=='create' or false_confirmation))
                if cleanup_fails:
                    self.assertIs(caught.__cause__,cleanup_error)
                    self.assertIs(caught.server_handle,server)
                if close_fails and not cleanup_fails:
                    self.assertIs(caught.__cause__,close_error)
                if failure:
                    # Inspect the explicit causes/contexts and original-error
                    # attribute without executing any cleanup retry.
                    stack,seen=[caught],set()
                    while stack:
                        error=stack.pop()
                        if error is None or id(error) in seen:continue
                        seen.add(id(error))
                        stack.extend([error.__cause__,error.__context__,getattr(error,'original_error',None)])
                    self.assertIn(id(primary),seen,'the original failure was lost')
                self.assertTrue((owned/'synthetic-sentinel').exists())

    def test_coordinator_red_setup_and_shutdown_failure_keeps_pgdata(self):
        self.run_case('setup',cleanup_fails=True)

    def test_normal_completion_cleans_owned_residue(self):
        self.run_case()

    def test_server_creation_failure_keeps_unconfirmed_directory(self):
        self.run_case('create')

    def test_enter_failure_with_confirmed_cleanup(self):
        self.run_case('enter')

    def test_enter_and_cleanup_failure_keeps_directory(self):
        self.run_case('enter',cleanup_fails=True)

    def test_connection_failure_with_confirmed_cleanup(self):
        self.run_case('connect')

    def test_connection_close_failure_still_stops_server_and_reports_environment(self):
        self.run_case(close_fails=True)

    def test_setup_failure_with_confirmed_cleanup_preserves_primary_cause(self):
        self.run_case('setup')

    def test_cleanup_failure_after_otherwise_success_is_not_a_success(self):
        self.run_case(cleanup_fails=True)

    def test_residue_removal_failure_is_not_ignored(self):
        self.run_case('setup',remove_fails=True)

    def test_explicit_false_cleanup_confirmation_keeps_directory(self):
        self.run_case(false_confirmation=True)


if __name__=='__main__':
    unittest.main()
