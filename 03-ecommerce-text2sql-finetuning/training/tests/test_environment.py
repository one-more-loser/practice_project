"""File integrity and honest preflight regression tests; no GPU/model required."""
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "check_environment.py"
spec = importlib.util.spec_from_file_location("environment_check", SCRIPT)
environment = importlib.util.module_from_spec(spec)
spec.loader.exec_module(environment)


class EnvironmentTests(unittest.TestCase):
    def model_dir(self, root):
        root.joinpath("config.json").write_text(json.dumps({"model_type": "qwen3", "hidden_size": 4096,
                                                          "num_hidden_layers": 36}))
        for name in ("tokenizer.json", "tokenizer_config.json"):
            root.joinpath(name).write_text("{}")
        header = json.dumps({"weight": {"dtype": "F32", "shape": [1], "data_offsets": [0, 4]}}).encode()
        root.joinpath("model.safetensors").write_bytes(len(header).to_bytes(8, "little") + header + b"1234")

    def test_truncated_shard_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.model_dir(root)
            self.assertTrue(environment.inspect_model(root)["ok"])
            shard = root / "model.safetensors"
            shard.write_bytes(shard.read_bytes()[:-1])
            result = environment.inspect_model(root)
            self.assertFalse(result["ok"])
            self.assertIn("payload size", result["errors"][0])

    def test_index_requires_all_shards_and_rejects_path_escape(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.model_dir(root)
            root.joinpath("model.safetensors.index.json").write_text(json.dumps({
                "weight_map": {"w1": "missing.safetensors", "w2": "../outside.safetensors"}}))
            result = environment.inspect_model(root)
            self.assertFalse(result["ok"])
            self.assertEqual(len(result["errors"]), 2)
            self.assertTrue(any("escapes" in e for e in result["errors"]))

    def test_no_model_offline_outputs_json_without_false_success(self):
        config = SCRIPT.parent / "configs/rtx_pro_6000_96gb.json"
        proc = subprocess.run([sys.executable, str(SCRIPT), "--config", str(config), "--offline", "--no-model"],
                              capture_output=True, text=True, timeout=60)
        self.assertIn(proc.returncode, (0, 1))
        report = json.loads(proc.stdout)
        self.assertFalse(report["ready_for_training"])
        self.assertFalse(report["network_used"])
        self.assertFalse(report["weights_loaded"])
        self.assertEqual(next(c for c in report["checks"] if c["name"] == "model_files")["status"], "skipped")

    def test_import_does_not_import_gpu_or_model_libraries(self):
        code = ("import runpy, sys; runpy.run_path(sys.argv[1], run_name='environment_import'); "
                "assert 'torch' not in sys.modules; assert 'transformers' not in sys.modules")
        proc = subprocess.run([sys.executable, "-c", code, str(SCRIPT)], capture_output=True, text=True, timeout=10)
        self.assertEqual(proc.returncode, 0, proc.stderr)


if __name__ == "__main__":
    unittest.main()
