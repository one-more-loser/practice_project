"""Full self-check for data/sft_v1 (no model, no GPU).

Checks counts, unique IDs, group isolation, template count, 01/03 validator,
executes every query label against the FX-A sandbox through eval_business,
and compares the frozen test query rows against independent expected results.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "training"))
sys.path.insert(0, str(ROOT / "sandbox" / "runner"))
import eval_business as eb  # noqa: E402
from validator import validate  # noqa: E402
import sqlglot  # noqa: E402

DIGEST = "509990f5bdbafd1025ec769d6e1f3b3ad41462562f0c867ccd61ddc09caf64b9"
EXPECTED_TEST = {
    # independent business values on fx_a; order of columns follows the label SQL
    "E01-01": [[25500]], "E01-02": [[25500]], "E01-03": [[25500]], "E01-04": [[25500]],
    "E02-01": [["OI1", "sku_1", 1, 0], ["OI2", "sku_2", 1, 0]], "E02-02": [["OI1", "sku_1", 1, 0], ["OI2", "sku_2", 1, 0]],
    "E02-03": [["OI1", "sku_1", 1, 0], ["OI2", "sku_2", 1, 0]], "E02-04": [["OI1", "sku_1", 1, 0], ["OI2", "sku_2", 1, 0]],
    "E03-01": [["SH2", "SF", "carrier_signed", "2026-11-30T17:00:00+00:00", "2026-12-02T02:00:00+00:00"]],
    "E03-02": [["SH2", "SF", "carrier_signed", "2026-11-30T17:00:00+00:00", "2026-12-02T02:00:00+00:00"]],
    "E03-03": [["SH2", "SF", "carrier_signed", "2026-11-30T17:00:00+00:00", "2026-12-02T02:00:00+00:00"]],
    "E03-04": [["SH2", "SF", "carrier_signed", "2026-11-30T17:00:00+00:00", "2026-12-02T02:00:00+00:00"]],
}


def read_rows(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def gold(row: dict) -> dict:
    k = row["expected_kind"]
    if k == "query":
        obj = {"kind": "query", "sql": row["sql"], "parameter_names": row["parameter_names"]}
    elif k == "clarify":
        obj = {"kind": "clarify", "missing": row["clarify_slots"], "reason_code": row["reason_code"]}
    else:
        obj = {"kind": "unsupported", "reason_code": row["reason_code"]}
    return {"id": row["id"], "generated": json.dumps(obj, ensure_ascii=False, separators=(",", ":"))}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data/sft_v1")
    ap.add_argument("--consumer01-root", default=str(ROOT.parent / "01-ecommerce-customer-service-agent"))
    ap.add_argument("--output", default="outputs/sft-v1-check.json")
    args = ap.parse_args()
    d = ROOT / args.data
    split = {s: read_rows(d / f"{s}.jsonl") for s in ("train", "dev", "test")}
    errors: list[str] = []
    counts = {s: len(v) for s, v in split.items()}
    want = {"train": 152, "dev": 12, "test": 20}
    if counts != want:
        errors.append(f"counts {counts} != {want}")
    allrows = [r for v in split.values() for r in v]
    ids = [r["id"] for r in allrows]
    if len(ids) != len(set(ids)):
        errors.append("duplicate ids")
    groups: dict[str, set[str]] = {}
    for s, rs in split.items():
        for r in rs:
            groups.setdefault(r["group_id"], set()).add(s)
    cross = {g: s for g, s in groups.items() if len(s) > 1}
    if cross:
        errors.append(f"groups cross splits: {cross}")
    if len({r["template_id"] for r in split["train"]}) != 19:
        errors.append("train template count != 19")
    if len({r["group_id"] for r in split["train"]}) != 19:
        errors.append("train group count != 19")
    kinds = {s: {k: sum(r["expected_kind"] == k for r in rs) for k in ("query", "clarify", "unsupported")} for s, rs in split.items()}
    if kinds["train"] != {"query": 120, "clarify": 24, "unsupported": 8}:
        errors.append(f"train kinds {kinds['train']}")
    if kinds["test"] != {"query": 12, "clarify": 4, "unsupported": 4}:
        errors.append(f"test kinds {kinds['test']}")

    qrows = [r for r in allrows if r["expected_kind"] == "query"]

    def fp(sql: str) -> str:
        return sqlglot.parse_one(sql, read="postgres").sql(dialect="postgres", normalize=True)

    by_fp = {}
    for r in qrows:
        by_fp.setdefault(fp(r["sql"]), []).append(r)
    duplicates = {k: [(r["id"], r["split"]) for r in v] for k, v in by_fp.items() if len(v) > 1}
    cross_fp = {k: v for k, v in duplicates.items() if len({s for _, s in v}) > 1}
    if cross_fp:
        errors.append(f"cross-split SQL fingerprints: {list(cross_fp)[:5]}")
    families = {}
    for r in qrows:
        fam = r["template_id"]
        families.setdefault(fam, []).append(r)
    cross_family = {f: [(r["id"], r["split"], fp(r["sql"])) for r in rs]
                    for f, rs in families.items()
                    if len({r["split"] for r in rs}) > 1 and len({fp(r["sql"]) for r in rs}) > 1}
    if cross_family:
        errors.append(f"same business family with divergent SQL across splits: {list(cross_family)[:5]}")

    items = [{"sql": r["sql"], "schema_revision": "CS-QUERY-B2-v1", "schema_digest": DIGEST} for r in qrows]
    v01 = eb.validator01(items, Path(args.consumer01_root).resolve())
    bad01 = [(r["id"], o) for r, o in zip(qrows, v01) if not o["ok"]]
    if bad01:
        errors.append(f"01 validator failures: {bad01[:5]}")
    bad03 = [(r["id"], validate(r["sql"], r["parameter_names"]).code) for r in qrows
             if not validate(r["sql"], r["parameter_names"]).ok]
    if bad03:
        errors.append(f"03 validator failures: {bad03[:5]}")

    # Frozen test through eval_business (gold static oracle first).
    with tempfile.TemporaryDirectory(prefix="03-check-") as td:
        testpath = d / "test.jsonl"
        predpath = Path(td) / "gold.json"
        outpath = Path(td) / "business.json"
        predpath.write_text(json.dumps({"predictions": [gold(r) for r in split["test"]]}, ensure_ascii=False), encoding="utf-8")
        cmd = [sys.executable, str(ROOT / "training" / "eval_business.py"),
               "--test", str(testpath), "--predictions", str(predpath),
               "--output", str(outpath), "--consumer01-root", args.consumer01_root]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            errors.append(f"eval_business rc={proc.returncode}: {proc.stderr[-500:]}")
            biz = {}
        else:
            biz = json.loads(outpath.read_text(encoding="utf-8"))
            failed = [x["id"] for x in biz["results"] if not x.get("pass")]
            if failed:
                errors.append(f"business failures: {failed[:10]}")
            test_rec = {x["id"]: x for x in biz["results"]}
            for rid, expected in EXPECTED_TEST.items():
                rec = test_rec.get(rid)
                if not rec or rec.get("candidate_rows") != expected:
                    errors.append(f"{rid} expected {expected}, got {rec and rec.get('candidate_rows')}")

    # Single self-owned PG path: 144 query labels x 3 fixtures. No dependency
    # on run.PGDATA and no second server.
    import pgserver
    import run as sandbox_run
    executed = len(qrows)
    executed_db = 0
    fixture_stats = {}
    with tempfile.TemporaryDirectory(prefix="03-check-pg-") as pgdir:
        srv = pgserver.get_server(Path(pgdir), cleanup_mode="delete")
        srv.__enter__()
        conn = sandbox_run.psycopg.connect(srv.get_uri(), autocommit=True)
        try:
            for fixture in ("fx_a", "fx_b", "fx_c"):
                sandbox_run.setup_fixture(conn, fixture)
                ok = 0
                for r in qrows:
                    got_rows = sandbox_run.run_query(conn, r["sql"], eb._params(r), eb.SCOPE)["rows"]
                    if fixture == "fx_a" and r.get("expected") is not None:
                        got = eb.canon(got_rows, r.get("comparison") == "ordered")
                        want = eb.canon(r["expected"]["rows"], r.get("comparison") == "ordered")
                        if got != want:
                            errors.append(f"{r['id']} fx_a static mismatch")
                    ok += 1
                fixture_stats[fixture] = ok
                executed_db += ok
        finally:
            conn.close()
            srv.cleanup()

    pgdata_used = pgdir
    train_fps = {fp(r["sql"]) for r in split["train"] if r["expected_kind"] == "query"}
    report = {"data": str(d), "counts": counts, "kinds": kinds,
              "groups": len(groups), "train_groups": len({r["group_id"] for r in split["train"]}),
              "templates_train": len({r["template_id"] for r in split["train"]}),
              "train_sql_fingerprints": len(train_fps),
              "sql_fingerprints": len(by_fp), "duplicates": duplicates,
              "cross_split_sql": list(cross_fp), "cross_split_family": list(cross_family),
              "queries_executed": executed, "queries_executed_db": executed_db,
              "pgdata": pgdata_used,
              "fixture_cases": fixture_stats,
              "errors": errors, "ok": not errors,
              "business_summary": (biz or {}).get("summary")}
    out = ROOT / args.output
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if errors:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
