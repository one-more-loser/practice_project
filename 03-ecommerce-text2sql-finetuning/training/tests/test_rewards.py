"""Real PG reward checks (no model/GPU). Requires the sandbox Python environment."""
import json
import sys
import unittest
from unittest.mock import patch
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from rewards import SQLReward, equivalent
from common import answer, load_jsonl


class RewardUnitTest(unittest.TestCase):
    def test_bags_keep_duplicates(self):
        self.assertTrue(equivalent([[1], [2], [1]], [[2], [1], [1]]))
        self.assertFalse(equivalent([[1], [1]], [[1]]))
        self.assertFalse(equivalent([[1], [2]], [[2], [1]], ordered=True))
        self.assertTrue(equivalent([[None], [2]], [[2], [None]]))

    def test_reject_nontrain(self):
        with self.assertRaises(ValueError):
            SQLReward([{"id": "heldout", "split": "test"}], ".", {})


class RealPostgresRewardTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rows = load_jsonl(ROOT.parent / "data/sft_v1/train.jsonl")
        cls.oracle = SQLReward(cls.rows, ROOT.parent / "sandbox/consumer01",
                              ROOT.parent / "data/sft_v1/manifest.json")
        cls.oracle.__enter__()

    @classmethod
    def tearDownClass(cls):
        directory = cls.oracle.directory
        cls.oracle.close()
        assert not Path(directory).exists()

    def test_gold_scores_one_on_all_fixtures(self):
        for template in ("T01", "T04", "T05", "T08"):
            row = next(r for r in self.rows if r["template_id"] == template)
            got = self.oracle.score(row, answer(row))
            self.assertEqual(got["reward"], 1., (row["id"], got))
            self.assertEqual(got["fixture_matches"], dict.fromkeys(self.oracle.fixtures, True))

    def test_wrong_kind_wrapped_json_and_illegal_sql_zero(self):
        row = next(r for r in self.rows if r["expected_kind"] == "query")
        for text in ('{"kind":"unsupported","reason_code":"x"}',
                     "```json\n" + answer(row) + "\n```",
                     json.dumps({"kind": "query", "sql": "DELETE FROM cs_order_self",
                                 "parameter_names": row["parameter_names"]})):
            self.assertEqual(self.oracle.score(row, text)["reward"], 0.)

    def test_valid_wrong_business_result_zero(self):
        row = next(r for r in self.rows if r["template_id"] == "T01")
        obj = json.loads(answer(row))
        obj["sql"] = obj["sql"].replace("count(*)", "count(*) + 1")
        got = self.oracle.score(row, json.dumps(obj))
        self.assertEqual(got["reward"], 0.)
        self.assertEqual(got["status"], "executed")

    def test_nonquery_protocol(self):
        for kind in ("clarify", "unsupported"):
            row = next(r for r in self.rows if r["expected_kind"] == kind)
            self.assertEqual(self.oracle.score(row, answer(row))["reward"], 1.)

    def test_modified_row_cannot_supply_trusted_context(self):
        row = dict(self.rows[0], parameters={"owner": "cust_b"})
        with self.assertRaises(ValueError):
            self.oracle.score(row, answer(row))

    def test_candidate_is_reexecuted_even_for_identical_text(self):
        row = self.rows[0]
        with patch.object(self.oracle.runner, "run_query",
                          wraps=self.oracle.runner.run_query) as execute:
            self.assertEqual(self.oracle.score(row, answer(row))["reward"], 1.)
            self.assertEqual(self.oracle.score(row, answer(row))["reward"], 1.)
            self.assertEqual(execute.call_count, 2 * len(self.oracle.fixtures))

    def test_gold_invalid_hard_fails_and_closes(self):
        bad = dict(self.rows[0], sql="DELETE FROM cs_order_self")
        oracle = SQLReward([bad], self.oracle.consumer01_root, self.oracle.manifest)
        with self.assertRaises(RuntimeError):
            oracle.__enter__()
        self.assertFalse(oracle.ready)
        self.assertFalse(oracle.connections)

    def test_gold_execution_error_hard_fails_and_cleans_owned_cluster(self):
        oracle = SQLReward([self.rows[0]], self.oracle.consumer01_root, self.oracle.manifest)
        with patch.object(self.oracle.runner, "run_query", side_effect=RuntimeError("gold DB failure")):
            with self.assertRaisesRegex(RuntimeError, "gold DB failure"):
                oracle.__enter__()
        self.assertIsNone(oracle.directory)
        self.assertFalse(oracle.connections)
        self.assertFalse(oracle.servers)


if __name__ == "__main__":
    unittest.main()
