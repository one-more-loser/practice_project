"""Model-free orchestration regressions, not evidence of trained model quality."""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'training'))
from training import pipeline
from training.compare_runs import compare
from training.eval_sft import load_predictions
from training.inference import generate_rows
from scoring import score_rows
from provenance import evaluation_identity


class PipelineTests(unittest.TestCase):
    def test_total_budget_is_shared_across_stages(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            calls = []
            def runner(command, log, timeout):
                calls.append(timeout)
                (directory / (command[-1] + '.json')).write_text('{}')
                return 0, False
            with patch.object(pipeline.time, 'monotonic', side_effect=[0, 1, 4]):
                with self.assertRaisesRegex(TimeoutError, 'total time budget'):
                    pipeline.execute_plan(self.make_plan(directory), {'base_eval', 'base_business'},
                                          directory / 'state.json', 'v1', 3, runner)
            self.assertEqual(calls, [2])
            state = json.loads((directory / 'state.json').read_text())
            self.assertEqual(state['stages']['base_eval']['status'], 'complete')
            self.assertEqual(state['stages']['base_business']['status'], 'failed')

    def test_real_process_timeout_returns_failure_and_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / 'timeout.log'
            rc, timed_out = pipeline.run_process(
                [sys.executable, '-c', 'import time; print("started", flush=True); time.sleep(60)'],
                log, 1.0)
            self.assertTrue(timed_out)
            self.assertNotEqual(rc, 0)
            self.assertIn('started', log.read_text())

    def test_inference_left_padding_eos_batch_and_exact_length_boundary(self):
        class Tensor:
            def __init__(self, values):
                self.values = values
                self.shape = (len(values), len(values[0]))
            def to(self, device):
                return self
            def __iter__(self):
                return iter(self.values)
        class Tokenizer:
            pad_token_id = 9
            eos_token_id = 9
            padding_side = 'right'
            def __call__(self, texts, **kwargs):
                assert kwargs['add_special_tokens'] is False
                if isinstance(texts, str):
                    length = 2 if '\nshort\n' in texts else (4 if '\nexact\n' in texts else 5)
                    return {'input_ids': [1] * length}
                self.assert_left = self.padding_side == 'left'
                return {'input_ids': Tensor([[9, 9, 1, 1], [1, 1, 1, 1]]),
                        'attention_mask': Tensor([[0, 0, 1, 1], [1, 1, 1, 1]]),
                        'token_type_ids': Tensor([[0, 0, 0, 0], [0, 0, 0, 0]])}
            def decode(self, tokens, **kwargs):
                assert tokens == [7, 9], 'Must slice at padded input width'
                assert kwargs['skip_special_tokens']
                return '{"kind":"unsupported","reason_code":"x"}'
        class Model:
            def eval(self):
                return self
            def parameters(self):
                return iter([types.SimpleNamespace(device='cpu')])
            def generate(self, **kwargs):
                assert 'token_type_ids' not in kwargs
                assert kwargs['eos_token_id'] == 9
                assert kwargs['pad_token_id'] == 9
                assert kwargs['attention_mask'].values[0] == [0, 0, 1, 1]
                assert kwargs['do_sample'] is False
                assert 'temperature' not in kwargs
                return [row + [7, 9] for row in kwargs['input_ids']]
        tokenizer = Tokenizer()
        cfg = {'model': {'local_files_only': True},
               'evaluation': {'batch_size': 3, 'max_prompt_tokens': 4, 'max_new_tokens': 2}}
        rows = [{'id': name, 'question': name} for name in ('short', 'exact', 'long')]
        fake_torch = types.ModuleType('torch')
        fake_torch.inference_mode = contextlib.nullcontext
        with patch.dict(sys.modules, {'torch': fake_torch}):
            records = generate_rows(cfg, rows, loader=lambda *args, **kwargs: (Model(), tokenizer))
        self.assertTrue(tokenizer.assert_left)
        self.assertFalse(records[1]['prompt_truncated'])
        self.assertTrue(records[0]['parse_diagnostics']['strict_json'])
        self.assertEqual(records[2]['skipped'], 'prompt_exceeds_max_prompt_tokens')
        self.assertEqual(records[2]['generated'], '')

    def test_cached_parse_and_protocol_fields_are_not_truth(self):
        rows = [{'id': 'x', 'template_id': 'N1', 'expected_kind': 'unsupported', 'reason_code': 'no'}]
        result = score_rows(rows, [{'id': 'x', 'generated': 'not json',
                                    'strict_json': True, 'protocol_ok': True}],
                            query_validator=lambda sql, params: True)
        self.assertFalse(result['records'][0]['strict_json'])
        self.assertFalse(result['records'][0]['protocol_ok'])

    def make_plan(self, directory):
        return [{'stage': name, 'command': [sys.executable, name], 'artifacts': [str(directory / (name + '.json'))]}
                for name in ('base_eval', 'base_business')]

    def test_resume_requires_same_inputs_code_exit_and_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            plan = self.make_plan(directory)
            calls = []
            def runner(command, log, timeout):
                name = command[-1]
                calls.append(name)
                (directory / (name + '.json')).write_text(json.dumps({'stage': name}))
                return 0, False
            state = directory / 'state.json'
            selected = {'base_eval', 'base_business'}
            pipeline.execute_plan(plan, selected, state, 'input-code-v1', 3, runner)
            self.assertEqual(len(calls), 2)
            pipeline.execute_plan(plan, selected, state, 'input-code-v1', 3, runner)
            self.assertEqual(len(calls), 2)
            (directory / 'base_eval.json').write_text('tampered')
            pipeline.execute_plan(plan, selected, state, 'input-code-v1', 3, runner)
            self.assertEqual(calls[-1], 'base_eval')
            pipeline.execute_plan(plan, selected, state, 'input-code-v2', 3, runner)
            self.assertEqual(calls[-2:], ['base_eval', 'base_business'])

    def test_failure_stops_dependency_and_records_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            calls = []
            def runner(command, log, timeout):
                calls.append(command)
                return -9, True
            state = directory / 'state.json'
            with self.assertRaisesRegex(RuntimeError, 'does not shut down AutoDL'):
                pipeline.execute_plan(self.make_plan(directory), {'base_eval', 'base_business'}, state, 'v1', 1, runner)
            data = json.loads(state.read_text())
            self.assertEqual(len(calls), 1)
            self.assertEqual(data['stages']['base_eval']['status'], 'failed')
            self.assertTrue(data['stages']['base_eval']['timed_out'])
            self.assertNotIn('base_business', data['stages'])

    def test_missing_dependency_cannot_be_skipped_by_selection(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            with self.assertRaisesRegex(RuntimeError, 'include it in --stages'):
                pipeline.execute_plan(self.make_plan(directory), {'base_business'}, directory / 'state.json', 'v1', 1)

    def test_stage_groups_include_oracle(self):
        self.assertEqual(pipeline.select_stages(['base,sft_eval', 'grpo_eval']),
                         {'base_eval', 'base_business', 'sft_eval', 'sft_business', 'grpo_eval', 'grpo_business', 'compare'})
        with self.assertRaises(ValueError):
            pipeline.select_stages(['typo'])

    def test_dry_run_no_runtime_import_or_output_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'must-not-exist'
            cfg = {'model': {'path': '/missing/8b', 'local_files_only': True},
                   'data': {'test': 'data/sft_v1/test.jsonl'}, 'runtime': {'output_dir': str(out)}}
            fake = types.ModuleType('config')
            fake.load_config = lambda path, overrides: cfg
            fake.resolve_path = Path
            with patch.dict(sys.modules, {'config': fake}), patch.object(sys, 'argv', ['pipeline', '--config', 'x.json', '--dry-run']), contextlib.redirect_stdout(io.StringIO()) as output:
                pipeline.main()
            data = json.loads(output.getvalue())
            self.assertEqual(len(data['plan']), 9)
            self.assertFalse(out.exists())
            commands = [item['command'] for item in data['plan']]
            self.assertIn('training.train_grpo', commands[5])
            self.assertIn('--sft-adapter', commands[5])
            self.assertTrue(all('trainer' not in ' '.join(command).lower() for command in commands))

    def test_predictions_records_and_predictions_formats(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'preds.json'
            for field in ('records', 'predictions'):
                path.write_text(json.dumps({field: [{'id': 'x', 'generated': 'bad', 'strict_json': True}]}))
                self.assertEqual(load_predictions(path)[0]['generated'], 'bad')
            path.write_text(json.dumps([{'id': 'x'}, {'id': 'x'}]))
            with self.assertRaisesRegex(ValueError, 'Duplicate'):
                load_predictions(path)

    def test_compare_fixed_denominators_and_regressions(self):
        rows = [{'id': 'a', 'template_id': 'T1', 'question': 'a?', 'expected_kind': 'query'},
                {'id': 'b', 'template_id': 'T2', 'question': 'b?', 'expected_kind': 'clarify'}]
        reports = {name: {'summary': {'oracle_error': False}, 'evaluation_identity': evaluation_identity(rows), 'results': [
            {'id': 'a', 'pass': name != 'base'}, {'id': 'b', 'pass': name != 'grpo'}]}
            for name in ('base', 'sft', 'grpo')}
        result = compare(rows, reports)
        self.assertEqual(result['categories']['all']['total'], 2)
        self.assertEqual(result['categories']['all']['sft'], 2)
        self.assertEqual(result['records'][1]['grpo_vs_sft'], -1)
        reports['grpo']['results'].pop()
        with self.assertRaisesRegex(ValueError, 'exactly match'):
            compare(rows, reports)


if __name__ == '__main__':
    unittest.main()
