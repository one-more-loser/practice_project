"""Preflight the evaluation contract without loading a model.

Runs five groups through eval_business.py (fresh temp PG, 01 validator):
gold, one wrong kind, invalid text, invalid text with fake caches, Markdown-
wrapped correct JSON. Also asserts bag/ordered comparison semantics.

Usage:
  sandbox/.venv/bin/python training/test_preflight.py \
    --test data/sft/test.jsonl --consumer01-root ../01-ecommerce-customer-service-agent \
    --output-dir outputs/preflight/source01
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def strict_obj(row: dict) -> dict:
    k = row["expected_kind"]
    if k == "query":
        return {"kind": "query", "sql": row["sql"], "parameter_names": row["parameter_names"]}
    if k == "clarify":
        return {"kind": "clarify", "missing": row["clarify_slots"], "reason_code": row["reason_code"]}
    return {"kind": "unsupported", "reason_code": row["reason_code"]}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", default="data/sft/test.jsonl")
    ap.add_argument("--consumer01-root", required=True)
    ap.add_argument("--output-dir", required=True)
    args = ap.parse_args()
    rows = [json.loads(x) for x in (ROOT / args.test).read_text(encoding="utf-8").splitlines()]
    outdir = ROOT / args.output_dir
    outdir.mkdir(parents=True, exist_ok=True)

    gold = [{"id": r["id"], "generated": json.dumps(strict_obj(r), ensure_ascii=False, separators=(",", ":"))} for r in rows]
    groups = {}
    groups["g1_gold"] = gold

    bad_kind = [dict(x) for x in gold]
    first_query = next(r for r in rows if r["expected_kind"] == "query")
    for p in bad_kind:
        if p["id"] == first_query["id"]:
            p["generated"] = json.dumps({"kind": "clarify", "missing": [], "reason_code": "wrong_kind"})
    groups["g2_wrong_kind"] = bad_kind

    groups["g3_invalid"] = [{"id": r["id"], "generated": "not json at all"} for r in rows]

    fake_cache = []
    for r, p in zip(rows, gold):
        fake_cache.append({"id": r["id"], "generated": "not json at all",
                           "strict_json": strict_obj(r), "lenient_json": strict_obj(r)})
    groups["g4_invalid_with_cache"] = fake_cache

    groups["g5_markdown"] = [
        {"id": r["id"], "generated": "```json\n" + json.dumps(strict_obj(r), ensure_ascii=False) + "\n```"}
        for r in rows
    ]

    expected = {
        "g1_gold": {"strict_json_rate": 1.0, "protocol_rate_all": 1.0, "query_protocol_rate": 1.0, "query_business_pass_rate": 1.0},
        "g2_wrong_kind": {"strict_json_rate": 1.0, "protocol_rate_all": 0.95, "query_protocol_rate": 0.9167, "query_business_pass_rate": 0.9167},
        "g3_invalid": {"strict_json_rate": 0.0, "protocol_rate_all": 0.0, "query_protocol_rate": 0.0, "query_business_pass_rate": 0.0},
        "g4_invalid_with_cache": {"strict_json_rate": 0.0, "protocol_rate_all": 0.0, "query_protocol_rate": 0.0, "query_business_pass_rate": 0.0},
        "g5_markdown": {"strict_json_rate": 0.0, "protocol_rate_all": 0.0, "query_protocol_rate": 0.0, "query_business_pass_rate": 0.0},
    }

    failures = []
    reports = {}
    for name, preds in groups.items():
        pred_path = outdir / f"{name}.predictions.json"
        out_path = outdir / f"{name}.json"
        pred_path.write_text(json.dumps({"predictions": preds}, ensure_ascii=False, indent=2), encoding="utf-8")
        cmd = [sys.executable, str(ROOT / "training" / "eval_business.py"),
               "--test", str(ROOT / args.test), "--predictions", str(pred_path),
               "--output", str(out_path), "--consumer01-root", args.consumer01_root]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            failures.append(f"{name}: eval_business rc={proc.returncode} {proc.stderr[-300:]}")
            continue
        s = json.loads(out_path.read_text(encoding="utf-8"))["summary"]
        reports[name] = {k: s.get(k) for k in ("strict_json_rate", "lenient_json_rate", "protocol_rate_all",
                                               "query_total", "query_protocol_correct", "query_protocol_rate",
                                               "query_business_pass_rate", "gold_checked")}
        for k, want in expected[name].items():
            got = s.get(k)
            if not isinstance(got, (int, float)) or abs(float(got) - float(want)) > 1e-9:
                failures.append(f"{name}.{k}: got {got!r}, want {want}")
        if s.get("query_total") != 12:
            failures.append(f"{name}.query_total: {s.get('query_total')} != 12")
        if s.get("gold_checked") != 12:
            failures.append(f"{name}.gold_checked: {s.get('gold_checked')} != 12")
        if name == "g5_markdown" and s.get("lenient_json_rate") != 1.0:
            failures.append("g5.lenient_json_rate != 1.0")

    # Bag/ordered unit checks (no DB): duplicate rows and reversed order.
    from eval_business import canon
    if canon([[1], [1]]) == canon([[1]]):
        failures.append("bag comparison dropped a duplicate row")
    if canon([[1], [2]], ordered=True) == canon([[2], [1]], ordered=True):
        failures.append("ordered comparison accepted reversed rows")

    report = {"test": args.test, "consumer01_root": args.consumer01_root,
              "groups": reports, "failures": failures, "ok": not failures}
    (outdir / "preflight-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
