"""Two small regressions for the data checker itself (no model/GPU).

1) copy a train query into dev with a new id/group_id -> must fail cross-split SQL;
2) put a different-SQL row of the same business family (template T04) into dev
   -> must fail the family/SQL divergence check.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run_case(name: str, mutate) -> tuple[bool, str]:
    tmp = Path(tempfile.mkdtemp(prefix=f"03-checker-{name}-"))
    try:
        for f in ("train.jsonl", "dev.jsonl", "test.jsonl", "manifest.json"):
            shutil.copy2(ROOT / "data" / "sft_v1" / f, tmp / f)
        rows = [json.loads(x) for x in (tmp / "dev.jsonl").read_text(encoding="utf-8").splitlines()]
        mutate(rows)
        (tmp / "dev.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, str(ROOT / "training" / "check_dataset.py"), "--data", str(tmp),
             "--consumer01-root", str(ROOT / "sandbox" / "consumer01"),
             "--output", str(tmp / "out.json")],
            capture_output=True, text=True,
        )
        out = (tmp / "out.json").read_text() if (tmp / "out.json").exists() else proc.stdout + proc.stderr
        return proc.returncode != 0, out
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> None:
    train = [json.loads(x) for x in (ROOT / "data" / "sft_v1" / "train.jsonl").read_text(encoding="utf-8").splitlines()]
    sample = next(r for r in train if r["expected_kind"] == "query")

    def dup(rows):
        r = dict(sample)
        r["id"] = "REG-DUP-01"
        r["group_id"] = "G-REG-DUP"
        r["split"] = "dev"
        rows.append(r)

    def family(rows):
        r = next(x for x in rows if x["id"].startswith("DV-D03"))
        r["template_id"] = "T04"
        r["group_id"] = "G-REG-FAMILY"
        r["sql"] = "SELECT order_id FROM cs_order_self WHERE created_at >= :start_at AND created_at < :end_at AND trade_status = 'paid' AND fulfillment_status = 'unshipped'"
        r["parameter_names"] = ["start_at", "end_at"]

    ok1, out1 = run_case("dup", dup)
    ok2, out2 = run_case("family", family)
    print(json.dumps({"duplicate_sql_regression_rejected": ok1,
                      "family_divergence_regression_rejected": ok2,
                      "dup_contains_cross": "cross-split" in out1,
                      "family_contains_cross": "business family" in out2}, ensure_ascii=False, indent=2))
    if not (ok1 and ok2 and "cross-split" in out1 and "business family" in out2):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
