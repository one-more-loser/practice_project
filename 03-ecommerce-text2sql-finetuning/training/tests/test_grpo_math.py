"""Run with python -m unittest discover -s training/tests -p test_grpo_math.py."""
import importlib.util
import json
import math
import sys
import random
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from grpo_math import normalize_group


class GroupMathTest(unittest.TestCase):
    def test_truncated_sampling_rejected(self):
        from config import load_config
        from train_grpo import check_train
        cfg = load_config()
        cfg["grpo"]["top_p"] = .95
        with self.assertRaisesRegex(ValueError, "requires top_p=1"):
            check_train(cfg)

    def test_constant_group_is_zero(self):
        self.assertEqual(normalize_group([0, 0, 0]), [0, 0, 0])
        self.assertEqual(normalize_group([1, 1]), [0, 0])

    def test_population_normalization(self):
        self.assertEqual(normalize_group([0, 1]), [-1, 1])
        out = normalize_group([0, 0, 1])
        self.assertAlmostEqual(sum(out), 0)
        self.assertAlmostEqual(sum(x*x for x in out) / len(out), 1)

    def test_invalid_group(self):
        for group in ([1], [], [0, math.inf], [0, math.nan]):
            with self.assertRaises(ValueError):
                normalize_group(group)


@unittest.skipUnless(importlib.util.find_spec("torch"), "PyTorch not installed")
class TorchMathTest(unittest.TestCase):
    def test_budget_failure_and_zero_variance_are_reported_honestly(self):
        import torch
        import torch._dynamo  # initialize optimizer runtime before temporary module isolation
        from types import SimpleNamespace
        from config import load_config
        from lora import inject_lora, adapter_parameters, save_adapter
        import train_grpo

        with tempfile.TemporaryDirectory(prefix="03-grpo-control-test-") as directory:
            cfg = load_config()
            cfg["model"]["torch_dtype"] = "float32"
            cfg["grpo"].update(max_steps=1, batch_size=1, gradient_accumulation_steps=1,
                               num_generations=2, updates_per_rollout=1, weight_decay=1.)
            cfg["runtime"].update(device="cpu", output_dir=str(Path(directory) / "out"),
                                   max_seconds=1e-12)
            model = inject_lora(torch.nn.Sequential(torch.nn.Linear(3, 3)),
                                {"rank": 2, "alpha": 4, "dropout": 0., "target_modules": ["0"]})
            source = Path(directory) / "source"
            save_adapter(model, source, extra={"stage": "synthetic_test"})
            fake_rewards = SimpleNamespace(SQLReward=MagicMock())
            with patch.dict(sys.modules, {"rewards": fake_rewards}), \
                 patch("runtime.load_policy", return_value=(model, None)):
                with self.assertRaisesRegex(RuntimeError, "time budget reached"):
                    train_grpo.train(cfg, [{"id": "synthetic"}], source, None, ".")
                summary = json.loads((Path(directory) / "out/run-summary.json").read_text())
                self.assertEqual(summary["status"], "time_budget_reached")
                self.assertTrue((Path(directory) / "out/checkpoint-0/checkpoint.pt").exists())
                self.assertFalse((Path(directory) / "out/adapter").exists())

                cfg["runtime"]["max_seconds"] = 0  # zero means unlimited, not immediate expiry
                parameters = [p for _, p in adapter_parameters(model)]
                log_probability = lambda *_: sum(p.sum() for p in parameters).reshape(1, 1)
                value = log_probability().detach()
                sample = {"ids": torch.tensor([[1, 2]]), "prompt_length": 1,
                          "old": value, "reference": value, "mask": torch.ones(1, 1),
                          "advantage": 0., "completion_tokens": 1}
                group = {"id": "synthetic", "rewards": [0., 0.], "zero_variance": True,
                         "statuses": ["protocol_failed", "protocol_failed"]}
                with patch("train_grpo.collect_rollout", return_value=([sample, sample], group)), \
                     patch("train_grpo.token_log_probs", side_effect=log_probability):
                    result = train_grpo.train(cfg, [{"id": "synthetic"}], source, None, ".")
                self.assertEqual(result["status"], "completed")
                self.assertTrue(result["parameter_update_verified"])  # weight decay alone
                self.assertEqual(result["nonconstant_reward_groups"], 0)
                self.assertFalse(result["learning_signal"])
                self.assertFalse(result["training_verified"])

    def test_float16_is_explicitly_rejected(self):
        from config import load_config
        from train_grpo import check_train
        cfg = load_config()
        cfg["model"]["torch_dtype"] = "float16"
        with self.assertRaisesRegex(ValueError, "BF16/FP32 only"):
            check_train(cfg)

    def test_native_lora_reference_update_and_checkpoint_resume(self):
        import torch
        from types import SimpleNamespace
        from lora import inject_lora, adapter_parameters, freeze_reference, reference_policy
        from runtime import save_checkpoint, load_checkpoint
        from grpo_math import grpo_loss, token_log_probs

        class TinyLM(torch.nn.Module):
            def __init__(self):
                super().__init__()
                self.embedding = torch.nn.Embedding(13, 8)
                self.proj = torch.nn.Linear(8, 13)
                self._native_supports_logits_to_keep = True

            def forward(self, input_ids, attention_mask, use_cache=False, logits_to_keep=None):
                hidden = self.embedding(input_ids)
                if logits_to_keep:
                    hidden = hidden[:, -logits_to_keep:]
                return SimpleNamespace(logits=self.proj(hidden))

        torch.manual_seed(17)
        model = inject_lora(TinyLM(), {"rank": 2, "alpha": 4, "dropout": 0.,
                                      "target_modules": ["proj"]})
        with torch.no_grad():
            model.proj.lora_B.normal_()  # synthetic nonzero SFT starting adapter
        freeze_reference(model)
        ids = torch.tensor([[1, 2, 3, 4, 5], [1, 2, 7, 8, 9]])
        attention = torch.ones_like(ids)
        with torch.no_grad():
            old = token_log_probs(model, ids, attention, 3)
            with reference_policy(model):
                reference = token_log_probs(model, ids, attention, 3)
        self.assertEqual(list(old.shape), [2, 2])
        full_logits = model(ids, attention).logits[:, 2:-1]
        expected = full_logits.float().log_softmax(-1).gather(-1, ids[:, 3:, None]).squeeze(-1)
        self.assertTrue(torch.allclose(old, expected))
        tempered = token_log_probs(model, ids, attention, 3, temperature=.7)
        expected_tempered = (full_logits.float() / .7).log_softmax(-1).gather(
            -1, ids[:, 3:, None]).squeeze(-1)
        self.assertTrue(torch.allclose(tempered, expected_tempered))
        self.assertFalse(torch.allclose(tempered, old))
        # Grouped rollout likelihoods must equal individual completion forwards.
        individual = torch.cat([token_log_probs(model, ids[i:i + 1], attention[i:i + 1],
                                                 3, temperature=.7)
                                for i in range(ids.shape[0])])
        self.assertTrue(torch.allclose(tempered, individual, atol=1e-6))
        with reference_policy(model):
            reference_group = token_log_probs(model, ids, attention, 3, temperature=.7)
            reference_individual = torch.cat([
                token_log_probs(model, ids[i:i + 1], attention[i:i + 1], 3, temperature=.7)
                for i in range(ids.shape[0])])
        self.assertTrue(torch.allclose(reference_group, reference_individual, atol=1e-6))
        parameters = [p for _, p in adapter_parameters(model)]
        optimizer = torch.optim.AdamW(parameters, lr=.01, weight_decay=0.)
        current = token_log_probs(model, ids, attention, 3)
        loss, _ = grpo_loss(current, old, reference, torch.tensor([-1., 1.]),
                            torch.ones_like(current), .04, .2)
        loss.backward()
        optimizer.step()
        self.assertFalse(torch.equal(token_log_probs(model, ids, attention, 3), old))
        with reference_policy(model):
            self.assertTrue(torch.equal(token_log_probs(model, ids, attention, 3), reference))
        before = [p.detach().clone() for p in parameters]
        with tempfile.TemporaryDirectory(prefix="03-grpo-checkpoint-test-") as directory:
            save_checkpoint(directory, model, optimizer, 1, extra={"stage": "synthetic_math_test"})
            expected_random, expected_torch = random.random(), torch.rand(3)
            with torch.no_grad():
                for p in parameters:
                    p.add_(2)
            state = load_checkpoint(directory, model, optimizer)
            self.assertEqual(state["step"], 1)
            self.assertEqual(random.random(), expected_random)
            self.assertTrue(torch.equal(torch.rand(3), expected_torch))
            self.assertTrue(all(torch.equal(a, b) for a, b in zip(before, parameters)))
            with reference_policy(model):
                self.assertTrue(torch.equal(token_log_probs(model, ids, attention, 3), reference))

    def test_eos_and_padding(self):
        import torch
        from grpo_math import completion_mask
        tokens = torch.tensor([[4, 2, 2, 2], [3, 4, 5, 6]])
        self.assertEqual(completion_mask(tokens, 2, 2).tolist(),
                         [[1, 1, 0, 0], [1, 1, 1, 1]])
        self.assertEqual(completion_mask(torch.tensor([[4, 2, 0]]), 2, 0).tolist(),
                         [[1, 1, 0]])

    def test_clipping_kl_mask_and_detach(self):
        import torch
        from grpo_math import grpo_loss
        logp = torch.tensor([[math.log(2), 99.0], [math.log(0.5), 99.0]],
                            requires_grad=True)
        old = torch.zeros_like(logp, requires_grad=True)
        ref = torch.zeros_like(logp, requires_grad=True)
        advantage = torch.tensor([1., -1.], requires_grad=True)
        loss, stats = grpo_loss(logp, old, ref, advantage,
                               torch.tensor([[1., 0.], [1., 0.]]), 0., .2)
        self.assertAlmostEqual(loss.item(), -.2, places=5)
        loss.backward()
        self.assertIsNone(old.grad)
        self.assertIsNone(ref.grad)
        self.assertIsNone(advantage.grad)
        self.assertEqual(logp.grad[:, 1].tolist(), [0., 0.])
        self.assertEqual(stats["clip_fraction"], 1.)

    def test_kl_zero_at_reference(self):
        import torch
        from grpo_math import grpo_loss
        p = torch.tensor([[-2., -3.]], requires_grad=True)
        loss, stats = grpo_loss(p, p.detach(), p.detach(), torch.zeros(1),
                               torch.ones_like(p), .1, .2)
        self.assertEqual(loss.item(), 0.)
        self.assertEqual(stats["kl"], 0.)


if __name__ == "__main__":
    unittest.main()
