"""Numerical tests use random tiny Qwen3, never network/8B/GPU."""
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import torch
from config import load_config
from create_smoke_model import create_smoke_model
from lora import adapter_parameters, adapter_state_dict
from runtime import load_policy
from train_sft import collate, completion_loss, epoch_batches, tokenize_example, train


class NativeSFTTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        torch.set_num_threads(1)
        cls.temp = tempfile.TemporaryDirectory(prefix='03-native-test-')
        cls.root = Path(cls.temp.name)
        create_smoke_model(cls.root / 'model')
        cls.cfg = load_config('training/configs/smoke_cpu.json')
        cls.cfg['model']['path'] = str(cls.root / 'model')
        cls.cfg['adapter']['dropout'] = 0.15
        cls.cfg['sft'].update(max_steps=3, gradient_accumulation_steps=2, save_every=1,
                              max_seq_len=1152, warmup_ratio=0.0)
        cls.cfg['runtime'].update(max_seconds=5, gradient_checkpointing=False)
        cls.rows = [{'id': f'toy-{i}', 'question': '删除订单' + '？' * i,
                     'expected_kind': 'unsupported', 'reason_code': 'write_not_allowed'} for i in range(5)]
        manifest_files = {}
        for name, rows in [('train', cls.rows), ('dev', [{**cls.rows[0], 'id':'toy-dev'}])]:
            rows = [{**r,'split':name} for r in rows]
            path = cls.root / f'{name}.jsonl'
            path.write_text(''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in rows), encoding='utf-8')
            cls.cfg['data'][name] = str(path)
            import hashlib
            manifest_files[name] = {'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
        manifest_path = cls.root/'manifest.json'
        manifest_path.write_text(json.dumps({'files':manifest_files}))
        cls.cfg['data']['manifest'] = str(manifest_path)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_actual_qwen_update_reload_and_frozen_base(self):
        out = self.root / 'train'
        run = train(copy.deepcopy(self.cfg), out)
        self.assertEqual(run['global_step'], 3)
        self.assertTrue(run['weight_update']['changed'])
        self.assertTrue(run['weight_update']['all_finite'])
        base, tok = load_policy(self.cfg, trainable=False)
        loaded, _ = load_policy(self.cfg, out / 'adapter', trainable=False)
        second, _ = load_policy(self.cfg, out / 'adapter', trainable=False)
        batch = collate([tokenize_example(tok, self.rows[0], 1152)], tok.pad_token_id, 'cpu')
        with torch.no_grad():
            actual = loaded(input_ids=batch['input_ids'], attention_mask=batch['attention_mask']).logits
            same = second(input_ids=batch['input_ids'], attention_mask=batch['attention_mask']).logits
        self.assertTrue(torch.equal(actual, same))
        base_parameters = dict(base.named_parameters())
        for name, param in loaded.named_parameters():
            if '.lora_' not in name:
                self.assertTrue(torch.equal(param, base_parameters[name]), name)

    def test_chunked_completion_ce_matches_full_and_padding_mask(self):
        cfg = copy.deepcopy(self.cfg)
        cfg['adapter']['dropout'] = 0.0
        model, tok = load_policy(cfg)
        batch = collate([tokenize_example(tok, r, 1152) for r in self.rows[:2]], tok.pad_token_id, 'cpu')
        observed, count = completion_loss(model, batch, chunk_tokens=3)
        logits = model(input_ids=batch['input_ids'], attention_mask=batch['attention_mask'], use_cache=False).logits
        expected = torch.nn.functional.cross_entropy(logits[:, :-1].reshape(-1, logits.shape[-1]),
                                                     batch['labels'][:, 1:].reshape(-1), ignore_index=-100)
        self.assertTrue(torch.allclose(observed, expected, atol=1e-6))
        self.assertEqual(count, int((batch['labels'] != -100).sum()))
        observed.backward()
        self.assertTrue(all(p.grad is None for n, p in model.named_parameters() if '.lora_' not in n))
        self.assertTrue(any(p.grad is not None and bool(p.grad.abs().sum()) for _, p in adapter_parameters(model)))

    def test_interrupted_resume_matches_uninterrupted_bitwise(self):
        class Clock:
            value = -2
            def __call__(self):
                self.value += 2
                return self.value
        partial = self.root / 'partial'
        with patch('train_sft.time.monotonic', side_effect=Clock()):
            with self.assertRaisesRegex(RuntimeError, 'time limit'):
                train(copy.deepcopy(self.cfg), partial)
        state = torch.load(partial / 'checkpoint-last/checkpoint.pt', weights_only=True)
        self.assertEqual(state['step'], 1)
        resumed = train(copy.deepcopy(self.cfg), partial, partial / 'checkpoint-last')
        complete = self.root / 'uninterrupted'
        train(copy.deepcopy(self.cfg), complete)
        after = torch.load(partial / 'adapter/adapter.pt', weights_only=True)
        expected = torch.load(complete / 'adapter/adapter.pt', weights_only=True)
        self.assertTrue(all(torch.equal(after[k], expected[k]) for k in after))
        self.assertEqual(resumed['global_step'], 3)
        # Learning-rate edits must not silently reinterpret an old optimizer state.
        changed = copy.deepcopy(self.cfg)
        changed['sft']['learning_rate'] *= 2
        with self.assertRaisesRegex(ValueError, 'configuration changed'):
            train(changed, self.root / 'invalid-resume', partial / 'checkpoint-last')

    def test_accumulated_token_weighted_gradient_matches_larger_batch(self):
        cfg = copy.deepcopy(self.cfg)
        cfg['adapter']['dropout'] = 0.0
        whole, tok = load_policy(cfg)
        micro = copy.deepcopy(whole)
        rows = [self.rows[0], {'id': 'clarify', 'question': '查订单', 'expected_kind': 'clarify',
                              'clarify_slots': ['start_at', 'end_at'], 'reason_code': 'missing_time'}]
        encoded = [tokenize_example(tok, r, 1152) for r in rows]
        loss, count = completion_loss(whole, collate(encoded, tok.pad_token_id, 'cpu'))
        loss.backward()
        for row in encoded:
            part, tokens = completion_loss(micro, collate([row], tok.pad_token_id, 'cpu'))
            (part * tokens / count).backward()
        for (_, actual), (_, expected) in zip(adapter_parameters(whole), adapter_parameters(micro)):
            self.assertTrue(torch.allclose(actual.grad, expected.grad, atol=2e-7, rtol=2e-5))

    def test_real_qwen_gradient_checkpointing_has_adapter_gradients(self):
        cfg = copy.deepcopy(self.cfg)
        cfg['runtime']['gradient_checkpointing'] = True
        model, tok = load_policy(cfg)
        self.assertTrue(model.is_gradient_checkpointing)
        batch = collate([tokenize_example(tok, self.rows[0], 1152)], tok.pad_token_id, 'cpu')
        loss, _ = completion_loss(model, batch)
        loss.backward()
        self.assertTrue(any(p.grad is not None and bool(p.grad.abs().sum()) for _, p in adapter_parameters(model)))
        self.assertTrue(all(p.grad is None for n, p in model.named_parameters() if '.lora_' not in n))

    def test_sampler_is_complete_deterministic_and_does_not_mutate_rng(self):
        import random
        rows = [{'input_ids': [0] * n} for n in [9, 2, 4, 1, 8]]
        state = random.getstate()
        a = epoch_batches(rows, 2, 123, 1, 4)
        self.assertEqual(a, epoch_batches(rows, 2, 123, 1, 4))
        self.assertEqual(sorted(i for b in a for i in b), list(range(5)))
        self.assertEqual(random.getstate(), state)

    def test_epoch_mode_counts_each_bucket_tail_batch(self):
        cfg = copy.deepcopy(self.cfg)
        cfg['sft'].update(max_steps=-1, epochs=1, batch_size=3,
                          length_bucket_size=4, gradient_accumulation_steps=1)
        run = train(cfg, self.root / 'epoch-bucket-tails')
        # Five examples: bucket of four -> [3,1], bucket of one -> [1].
        self.assertEqual(run['target_steps'], 3)
        self.assertEqual(run['global_step'], 3)


if __name__ == '__main__':
    unittest.main()
