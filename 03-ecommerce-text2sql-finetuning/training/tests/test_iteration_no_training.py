"""ITER-20261025-01 safe regressions: no torch/models/PG/network/optimizer."""
import copy
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'training'))
sys.path.insert(0, str(ROOT / 'sandbox/runner'))
from common import answer
from config import load_config
from scoring import score_rows, strict_json, protocol_ok, index_predictions, prediction_object
from grpo_math import normalize_group
from output_lock import exclusive_output
from provenance import (base_identity, file_sha256, validate_checkpoint_metadata,
                        validate_adapter_metadata, evaluation_identity)
import pipeline
import eval_business
from rewards import SQLReward
from oracle_errors import OracleEnvironmentError


class PredictionChecks(unittest.TestCase):
    row = {'id': 'toy', 'template_id': 'toy', 'expected_kind': 'unsupported', 'reason_code': 'write'}

    def score(self, preds):
        return score_rows([self.row], preds, lambda *_: True)

    def test_duplicate_unknown_missing_and_malformed_ids(self):
        good = {'id': 'toy', 'generated': answer(self.row)}
        with self.assertRaisesRegex(ValueError, 'Duplicate'):
            self.score([{'id': 'toy', 'generated': 'bad'}, good])
        with self.assertRaisesRegex(ValueError, 'Unknown'):
            self.score([good, {'id': 'alien', 'generated': 'bad'}])
        for value in [None, {}, [], 12, '']:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.score([{'id': value}])
        result = self.score([])
        self.assertEqual(result['summary']['unsupported_total'], 1)
        self.assertEqual(result['summary']['protocol_correct'], 0)

    def test_raw_json_types_duplicates_constants_wrapping_and_multidoc(self):
        for bad in [None, 1, [], '{}{}', '```json\n{}\n```',
                    '{"kind":"query","kind":"unsupported"}', '{"x":NaN}', '{"x":Infinity}', 'x'*32769]:
            with self.subTest(bad=str(bad)[:40]):
                self.assertIsNone(strict_json(bad))
        self.assertEqual(strict_json(' {"kind":"query"} '), {'kind':'query'})
        with self.assertRaisesRegex(ValueError, 'string'):
            self.score([{'id': 'toy', 'generated': {}}])

    def test_failures_remain_in_denominator_even_with_correct_cached_text(self):
        for flag in ['skipped', 'prompt_truncated', 'generation_truncated', 'timed_out', 'error']:
            with self.subTest(flag=flag):
                pred = {'id':'toy', 'generated':answer(self.row), flag:True, 'protocol_ok':True}
                result = self.score([pred])
                self.assertEqual(result['summary']['unsupported_total'], 1)
                self.assertEqual(result['summary']['protocol_correct'], 0)
                self.assertIsNone(prediction_object(pred))

    def test_mutually_exclusive_response_fields(self):
        self.assertFalse(protocol_ok(self.row, {'kind':'unsupported','reason_code':'write','sql':'DELETE FROM x'}))
        clarify = {'expected_kind':'clarify','clarify_slots':['order_id'],'reason_code':'missing_order'}
        self.assertFalse(protocol_ok(clarify, {'kind':'clarify','missing':['order_id'],
                                             'reason_code':'missing_order','parameter_names':[]}))
        query = {'expected_kind':'query','parameter_names':['start_at','end_at']}
        self.assertFalse(protocol_ok(query, {'kind':'query','sql':'SELECT 1', 'parameter_names':['start_at','start_at']}))

    def test_all_query_failures_keep_frozen_class_denominators(self):
        rows = [json.loads(x) for x in (ROOT/'data/sft_v1/test.jsonl').read_text().splitlines()]
        result = score_rows(rows, [], lambda *_: True)['summary']
        self.assertEqual((result['query_total'], result['clarify_total'], result['unsupported_total']), (12,4,4))
        self.assertEqual(result['protocol_correct'], 0)

    def test_business_loader_rejects_duplicate_before_database_import(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'preds.json'
            path.write_text(json.dumps([{'id':'x'}, {'id':'x'}]))
            with self.assertRaisesRegex(ValueError, 'Duplicate'):
                eval_business.load_predictions(path)

    def test_canonical_bags_with_null_and_heterogeneous_sql_values(self):
        self.assertEqual(eval_business.canon([[None], [1], ['1'], [None]]),
                         eval_business.canon([['1'], [None], [None], [1]]))
        self.assertNotEqual(eval_business.canon([[None],[None]]), eval_business.canon([[None]]))

    def test_same_ids_with_changed_gold_or_oracle_are_not_comparable(self):
        from compare_runs import compare
        row = {**self.row, 'question':'toy question'}
        rows = [row]
        reports = {name:{'summary':{'oracle_error':False},'evaluation_identity':evaluation_identity(rows),
                          'results':[{'id':'toy','pass':True}]} for name in ('base','sft','grpo')}
        self.assertEqual(compare(rows,reports)['categories']['all']['total'],1)
        changed = [{**row,'question':'different question, same ID'}]
        with self.assertRaisesRegex(ValueError,'identity differs'):
            compare(changed,reports)
        reports['grpo']['evaluation_identity'] = {'rows_sha256':'tampered'}
        with self.assertRaisesRegex(ValueError,'identity differs'):
            compare(rows,reports)


class RewardAndSandboxChecks(unittest.TestCase):
    def test_fixture_configuration_and_failed_cleanup_do_not_touch_other_clusters(self):
        oracle, row = self.make_oracle()
        subset = SQLReward([row], ROOT/'sandbox/consumer01', {'schema_digest':'toy'}, fixtures=['fx_a','fx_b'])
        self.assertEqual(subset.fixtures, ('fx_a','fx_b'))
        with self.assertRaises(ValueError):
            SQLReward([row], ROOT/'sandbox/consumer01', {'schema_digest':'toy'}, fixtures=['fx_a','../shared'])
        with tempfile.TemporaryDirectory() as tmp:
            oracle.directory = str(Path(tmp)/'owned')
            Path(oracle.directory).mkdir()
            first, second = MagicMock(), MagicMock()
            first.cleanup.side_effect = RuntimeError('owned shutdown failure')
            oracle.connections = {}
            oracle.servers = [first, second]
            with self.assertRaisesRegex(OracleEnvironmentError, 'cleanup'):
                oracle.close()
            second.cleanup.assert_not_called()
            self.assertTrue(Path(oracle.directory).exists())
            self.assertEqual(oracle.servers, [first, second])
    def make_oracle(self):
        row = {'id':'toy-query', 'split':'train', 'expected_kind':'query', 'schema_revision':'CS-QUERY-B2-v1',
               'sql':'SELECT count(*) AS n FROM cs_order_self', 'parameter_names':[]}
        oracle = SQLReward([row], ROOT/'sandbox/consumer01', {'schema_digest':'toy-digest'})
        oracle.ready = True
        oracle.connections = {'fx_a': object()}
        oracle.gold = {(row['id'], 'fx_a'): [[7]]}
        oracle.gold_columns = {(row['id'], 'fx_a'): ['n']}
        oracle.runner = SimpleNamespace(run_query=MagicMock(return_value={'rows': [[7]], 'columns':['n']}))
        return oracle, row

    def score_context(self):
        return patch.multiple('rewards', validate=MagicMock(return_value=SimpleNamespace(ok=True)),
                              validator01=MagicMock(return_value=[{'ok':True}]))

    def test_environment_loss_aborts_instead_of_becoming_negative_reward(self):
        oracle, row = self.make_oracle()
        class BrokenConnection(Exception):
            sqlstate = '08006'
        oracle.runner.run_query.side_effect = BrokenConnection()
        with self.score_context(), self.assertRaises(OracleEnvironmentError):
            oracle.score(row, answer(row))

    def test_candidate_timeout_and_wrong_business_results_get_zero(self):
        oracle, row = self.make_oracle()
        class CandidateTimeout(Exception):
            sqlstate = '57014'
        with self.score_context():
            oracle.runner.run_query.side_effect = CandidateTimeout()
            self.assertEqual(oracle.score(row, answer(row))['reward'], 0)
            oracle.runner.run_query.side_effect = None
            oracle.runner.run_query.return_value = {'rows': [[999]], 'columns':['n']}
            self.assertEqual(oracle.score(row, answer(row))['reward'], 0)

    def test_matching_values_with_wrong_columns_or_boolean_type_fail(self):
        oracle, row = self.make_oracle()
        with self.score_context():
            oracle.runner.run_query.return_value = {'rows':[[7]],'columns':['wrong_alias']}
            self.assertEqual(oracle.score(row, answer(row))['reward'], 0)
        self.assertFalse(eval_business.rows_equal([[True]],[[1]]))
        self.assertFalse(eval_business.rows_equal([[0]],[[None]]))

    def test_all_empty_fixtures_cannot_reward_an_always_empty_candidate(self):
        oracle, row = self.make_oracle()
        oracle.uninformative = {row['id']}
        oracle.gold[(row['id'],'fx_a')] = []
        with self.score_context():
            result = oracle.score(row, answer(row))
        self.assertEqual(result, {'reward':0.0, 'status':'insufficient_fixture_discrimination'})
        oracle.runner.run_query.assert_not_called()

    def test_candidates_not_cached_or_trusted_parameter_values_changed(self):
        oracle, row = self.make_oracle()
        with self.score_context():
            self.assertEqual(oracle.score(row, answer(row))['reward'], 1)
            self.assertEqual(oracle.score(row, answer(row))['reward'], 1)
            changed = {**row, 'parameters': {'order_id': 'someone_else'}}
            with self.assertRaisesRegex(ValueError, 'registered'):
                oracle.score(changed, answer(row))
        self.assertEqual(oracle.runner.run_query.call_count, 2)

    def test_eval_uses_owned_path_and_cleans_even_when_fixture_setup_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'preds.json'
            path.write_text('[]')
            server, conn = MagicMock(), MagicMock()
            server.cleanup_mode=None
            server.global_process_id_list.get.return_value=[]
            fake_pg = SimpleNamespace(get_server=MagicMock(return_value=server))
            fake_run = SimpleNamespace(PGDATA=Path('/must-not-use-existing-cluster'),
                psycopg=SimpleNamespace(connect=MagicMock(return_value=conn)),
                setup_fixture=MagicMock(side_effect=RuntimeError('fixture setup failed')))
            argv = ['eval', '--predictions', str(path), '--consumer01-root', str(ROOT/'sandbox/consumer01')]
            from owned_pg import OwnedSandbox
            with patch.dict(sys.modules, {'pgserver':fake_pg, 'run':fake_run}), patch.object(sys,'argv',argv), \
                    patch.object(OwnedSandbox,'_capture_identity',return_value={'test_only':True}), \
                    patch.object(OwnedSandbox,'_stop_and_release',return_value={'pg_ctl_waited_fast_stop':True}) as stopped:
                with self.assertRaisesRegex(OracleEnvironmentError, 'fixture_setup') as caught:
                    eval_business.main()
            self.assertIsInstance(caught.exception.__cause__, RuntimeError)
            self.assertEqual(str(caught.exception.__cause__), 'fixture setup failed')
            owned = fake_pg.get_server.call_args.args[0]
            self.assertNotEqual(owned, fake_run.PGDATA)
            self.assertFalse(owned.exists())
            stopped.assert_called_once()
            self.assertIsNone(fake_pg.get_server.call_args.kwargs['cleanup_mode'])
            conn.close.assert_called_once()


class ResumeAndOwnershipChecks(unittest.TestCase):
    def test_lock_reentry_exception_cleanup_and_foreign_owner_preservation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with exclusive_output(root):
                with self.assertRaisesRegex(RuntimeError, 'already locked'):
                    with exclusive_output(root):
                        self.fail('second writer entered')
            self.assertFalse((root/'.pipeline.lock').exists())
            with self.assertRaisesRegex(ValueError, 'synthetic'):
                with exclusive_output(root):
                    raise ValueError('synthetic')
            self.assertFalse((root/'.pipeline.lock').exists())
            with exclusive_output(root):
                (root/'.pipeline.lock').write_text('{"token":"foreign"}')
            self.assertTrue((root/'.pipeline.lock').exists())

    def test_nested_pipeline_writer_rejected_before_second_runner(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan = [{'stage':'base_eval','command':['mock'], 'artifacts':[str(root/'result.json')]}]
            def runner(*_):
                with self.assertRaisesRegex(RuntimeError,'already locked'):
                    pipeline.execute_plan(plan, {'base_eval'}, root/'state.json','toy',3,
                                          runner=lambda *_: self.fail('nested runner executed'))
                (root/'result.json').write_text('{}')
                return 0, False
            state = pipeline.execute_plan(plan, {'base_eval'}, root/'state.json','toy',3,runner)
            self.assertEqual(state['stages']['base_eval']['status'],'complete')

    def test_checkpoint_and_adapter_metadata_bind_actual_identity(self):
        identity = {'base':{'weights':'hash-a'},'training':{'schema':'schema-a','code':'code-a'}}
        state = dict(format='native-training-v1', step=3, adapter={}, optimizer={}, scheduler=None,
                     rng_python=[], rng_torch=[], rng_cuda=[], extra={}, identity=identity)
        validate_checkpoint_metadata(state, identity)
        for bad in [{**state,'step':-1}, {**state,'step':True}, {**state,'format':'old'},
                    {k:v for k,v in state.items() if k != 'identity'}]:
            with self.assertRaises(ValueError):
                validate_checkpoint_metadata(bad, identity)
        with self.assertRaisesRegex(ValueError,'identity changed'):
            validate_checkpoint_metadata(state, {**identity,'base':{'weights':'replaced'}})
        cfg = dict(rank=2, alpha=4, dropout=0.1, target_modules=['q_proj'])
        metadata = dict(format='native-lora-v1',base_identity={'sha':'a'},**cfg)
        validate_adapter_metadata(metadata,cfg,{'sha':'a'})
        for bad in [{**metadata,'dropout':0.2},{**metadata,'base_identity':{'sha':'b'}}]:
            with self.assertRaises(ValueError):
                validate_adapter_metadata(bad,cfg,{'sha':'a'})

    def test_local_base_identity_detects_same_path_content_replacement(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'model.safetensors').write_bytes(b'opaque-not-loaded-weights-a')
            (root/'config.json').write_text('{}')
            cfg = {'model': {'path':str(root),'revision':'local'}}
            before = base_identity(cfg)
            (root/'model.safetensors').write_bytes(b'opaque-not-loaded-weights-b')
            self.assertNotEqual(before, base_identity(cfg))

    def test_checkpoint_integrity_fails_before_any_model_state_mutation(self):
        import runtime
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'checkpoint.pt').write_bytes(b'corrupt-never-loaded')
            (root/'checkpoint-integrity.json').write_text('{"sha256":"wrong"}')
            fake_torch = SimpleNamespace(load=MagicMock())
            fake_lora = SimpleNamespace(load_adapter_state_dict=MagicMock())
            with patch.dict(sys.modules,{'torch':fake_torch,'lora':fake_lora}):
                with self.assertRaisesRegex(ValueError, 'integrity'):
                    runtime.load_checkpoint(root, SimpleNamespace())
            fake_torch.load.assert_not_called()
            fake_lora.load_adapter_state_dict.assert_not_called()


class ConfigurationAndMathChecks(unittest.TestCase):
    def test_sft_rejects_heldout_and_modified_manifest_without_loading_model(self):
        from train_sft import check_sft_inputs
        cfg = load_config()
        train, dev = check_sft_inputs(cfg)
        self.assertEqual((len(train),len(dev)),(152,12))
        bad = copy.deepcopy(cfg)
        bad['data']['train'] = cfg['data']['test']
        with self.assertRaisesRegex(ValueError,'distinct'):
            check_sft_inputs(bad)
        with tempfile.TemporaryDirectory() as tmp:
            altered = Path(tmp)/'train.jsonl'
            altered.write_text(json.dumps({'id':'copy','split':'test','question':'toy'})+'\n')
            bad['data']['train'] = str(altered)
            with self.assertRaisesRegex(ValueError,'split labels'):
                check_sft_inputs(bad)

    def test_business_prompt_rejects_sqlite_profile(self):
        from common import prompt
        with self.assertRaisesRegex(ValueError,'customer_self only'):
            prompt({'question':'toy','profile':'generic_sqlite'})

    def test_nonfinite_negative_budgets_and_implicit_download_are_rejected(self):
        for override in ['sft.learning_rate=NaN','sft.learning_rate=Infinity',
                         'pipeline.timeout_seconds=-1','grpo.max_steps=-1','sft.max_steps=true',
                         'model.local_files_only=false']:
            with self.subTest(override=override), self.assertRaises(ValueError):
                load_config(overrides=[override])

    def test_group_population_normalization_extreme_finite_values(self):
        self.assertEqual(normalize_group([1e308,1e308]),[0,0])
        self.assertEqual(normalize_group([-1e308,1e308]),[-1,1])
        for pair in [[1e-300,2e-300],[0,1]]:
            a,b=normalize_group(pair)
            self.assertAlmostEqual(a,-1)
            self.assertAlmostEqual(b,1)


if __name__ == '__main__':
    unittest.main()
