"""Business-result evaluation for Base/SFT predictions.

Strict JSON only. For each frozen test row:
- parse the raw `generated` string (cached strict/lenient fields are ignored);
- 03 validator + real 01 validator from --consumer01-root;
- execute candidate and gold SQL in a fresh temporary 03 PG sandbox with the
  row's recorded window/parameters and trusted customer scope;
- compare candidate to gold rows (bag/ordered) and to static expected;
- fixed denominators: 12 query / 4 clarify / 4 unsupported.

It never trains or connects 01/02 databases. Any missing 01 source/dependency,
01 validator failure, or gold/static oracle mismatch is a hard error, not a
skipped pass.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "training"))
sys.path.insert(0, str(ROOT / "sandbox" / "runner"))
from common import load_jsonl  # noqa: E402
from scoring import protocol_ok, score_rows, strict_json, index_predictions, prediction_object, prediction_error  # noqa: E402
from validator import validate  # noqa: E402
from oracle_errors import OracleEnvironmentError, is_candidate_sql_error
from provenance import evaluation_identity
from owned_pg import OwnedSandbox, cleanup_error

SCOPE = {"owner": "cust_a", "store": "store_a", "batch": "batch_1", "generation": 1, "role": "customer"}
FIXTURE = "fx_a"


def canon(rows, ordered: bool = False):
    out = []
    for row in rows:
        vals = []
        for v in row:
            if isinstance(v, datetime):
                v = v.astimezone(timezone.utc).isoformat()
            elif isinstance(v, Decimal):
                v = int(v) if v == v.to_integral_value() else str(v)
            vals.append(v)
        out.append(tuple(vals))
    # Typed/mixed SQL values (NULL, text, integer) have no Python total order.
    return out if ordered else sorted(out, key=lambda row: json.dumps(row, ensure_ascii=False))


def rows_equal(left, right, ordered=False):
    # Canonical JSON keeps bool distinct from integer; Python True == 1 does not.
    encode = lambda rows: [json.dumps(row, ensure_ascii=False, allow_nan=False)
                           for row in canon(rows, ordered=True)]
    a, b = encode(left), encode(right)
    return a == b if ordered else Counter(a) == Counter(b)


def load_predictions(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get('records', data.get('predictions'))
    index_predictions(data)
    return data


def validator01(sqls: list[dict], root: Path) -> list[dict]:
    src = root / "backend" / "src"
    vendor = ROOT / "sandbox" / "vendor" / "sqlglot30"
    if not src.exists():
        raise RuntimeError(f"01 source not found: {src}")
    if not vendor.exists():
        raise RuntimeError(f"sqlglot 30 vendor not found: {vendor}")
    script = r"""
import json, sys
from cs_agent.query.validator import validate_query
items=json.load(sys.stdin)
out=[]
for it in items:
    try:
        validate_query(it["sql"], schema_revision=it.get("schema_revision"), schema_digest=it.get("schema_digest"))
        out.append({"ok": True})
    except Exception as e:
        out.append({"ok": False, "code": type(e).__name__, "message": str(e)[:300]})
print(json.dumps(out, ensure_ascii=False))
"""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(vendor) + os.pathsep + str(src)
    proc = subprocess.run([sys.executable, "-c", script], input=json.dumps(sqls, ensure_ascii=False),
                          text=True, capture_output=True, env=env, timeout=30)
    if proc.returncode != 0:
        raise RuntimeError(f"01 validator subprocess failed rc={proc.returncode}: {proc.stderr[-500:]}")
    out = json.loads(proc.stdout)
    if len(out) != len(sqls):
        raise RuntimeError(f"01 validator returned {len(out)} for {len(sqls)} items")
    return out


def _cleanup_owned_sandbox(pgdir, srv, conn, owner, original_error=None, lease=None):
    if lease is not None and lease.server is srv and lease.path == Path(pgdir).absolute():
        lease.close(conn, original_error=original_error)
        return
    # An unregistered/legacy handle (including cleanup returning None) cannot
    # prove process identity. Do not invoke its stop/delete policy.
    failures = [('ownership', RuntimeError('no verified project sandbox lease'))]
    if conn is not None:
        try:
            conn.close()
        except BaseException as exc:
            failures.append(('connection_close', exc))
    cleanup_error(pgdir, srv, conn, owner, failures, original_error)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", default="data/sft_v1/test.jsonl")
    ap.add_argument("--predictions", required=True)
    ap.add_argument("--output", default="outputs/eval-business.json")
    ap.add_argument("--consumer01-root", default=str(ROOT.parent / "01-ecommerce-customer-service-agent"))
    args = ap.parse_args()

    rows = load_jsonl(ROOT / args.test)
    preds = load_predictions(ROOT / args.predictions)
    pred_by_id = index_predictions(preds, rows)
    root01 = Path(args.consumer01_root).resolve()
    if not root01.exists():
        raise SystemExit(f"consumer01 root missing: {root01}")

    import run as sandbox_run
    import pgserver
    pgdir = tempfile.mkdtemp(prefix="03-eval-pg-")
    owner = {'data_directory': str(Path(pgdir).resolve()), 'pid': os.getpid(),
             'host': socket.gethostname(), 'created_at_utc': datetime.now(timezone.utc).isoformat()}
    # An imported runner may already have PGDATA pointing to another cluster.
    # Select our owned path explicitly; never mutate or reuse runner.PGDATA.
    srv = None
    lease = None
    conn = None
    results = []
    try:
        startup_stage = 'server_create'
        try:
            lease = OwnedSandbox(pgdir)
            srv = lease.start(pgserver)
            startup_stage = 'server_enter'
            srv.__enter__()
            startup_stage = 'connection_open'
            conn = sandbox_run.psycopg.connect(srv.get_uri(), autocommit=True)
            startup_stage = 'fixture_setup'
            sandbox_run.setup_fixture(conn, FIXTURE)
        except Exception as exc:
            raise OracleEnvironmentError('business evaluation sandbox initialization failed at ' + startup_stage) from exc
        query_rows = [r for r in rows if r["expected_kind"] == "query"]
        gold_rows = {}
        gold_columns = {}
        gold_errors = []
        for row in query_rows:
            params = _params(row)
            try:
                result = sandbox_run.run_query(conn, row["sql"], params, SCOPE)
                got = result["rows"]
                want = canon(row["expected"]["rows"], row.get("comparison") == "ordered")
                ok = (rows_equal(got, row['expected']['rows'], row.get('comparison') == 'ordered')
                      and result['columns'] == row['expected']['columns'])
                gold_rows[row["id"]] = got
                gold_columns[row["id"]] = result['columns']
                if not ok:
                    gold_errors.append({"id": row["id"], "error": "gold_static_mismatch"})
            except Exception as exc:  # noqa: BLE001
                gold_errors.append({"id": row["id"], "error": f"{type(exc).__name__}: {exc}"})
        if gold_errors:
            _write(args.output, {"summary": {"oracle_error": True, "gold_errors": gold_errors,
                                             "rows": len(rows)}, "results": []})
            raise SystemExit(f"oracle_error: {len(gold_errors)} gold rows do not match static expected")

        sql_items = []
        for row in rows:
            pred = pred_by_id.get(row["id"], {})
            obj = prediction_object(pred)
            if row["expected_kind"] == "query" and protocol_ok(row, obj):
                sql_items.append({"id": row["id"], "sql": obj["sql"],
                                  "schema_revision": row["schema_revision"],
                                  "schema_digest": row["schema_digest"]})
        v01 = {it["id"]: v for it, v in zip(sql_items, validator01(sql_items, root01))}

        for row in rows:
            pred = pred_by_id.get(row["id"], {})
            obj = prediction_object(pred)
            rec = {
                "id": row["id"], "template_id": row["template_id"],
                "expected_kind": row["expected_kind"],
                "pred_kind": obj.get("kind") if isinstance(obj, dict) else None,
                "strict_json": obj is not None,
                "generated": pred.get("generated", ""),
                "prediction_error": prediction_error(pred),
                "protocol_ok": bool(protocol_ok(row, obj)),
                "pass": False,
            }
            if row["expected_kind"] == "query":
                if not rec["protocol_ok"]:
                    rec["query_status"] = "protocol_failed"
                    results.append(rec)
                    continue
                sql, declared = obj["sql"], obj["parameter_names"]
                params = _params(row)
                if sorted(params) != sorted(declared):
                    rec.update({"query_status": "parameter_mismatch", "pass": False})
                    results.append(rec)
                    continue
                v3 = validate(sql, declared)
                v1 = v01.get(row["id"], {"ok": False, "code": "not_submitted"})
                rec.update({"sql": sql, "parameter_names": declared,
                            "validator03": v3.ok, "validator03_code": v3.code,
                            "validator01": bool(v1.get("ok")),
                            "validator01_code": v1.get("code") or ""})
                if not (v3.ok and v1.get("ok")):
                    rec.update({"query_status": "validation_failed", "pass": False})
                    results.append(rec)
                    continue
                try:
                    cand = sandbox_run.run_query(conn, sql, params, SCOPE)
                    ordered = row.get("comparison") == "ordered"
                    rec.update({
                        "candidate_rows": canon(cand["rows"], ordered)[:50],
                        "gold_rows": canon(gold_rows[row["id"]], ordered)[:50],
                        "result_match": rows_equal(cand['rows'], gold_rows[row['id']], ordered),
                        "static_match": rows_equal(cand['rows'], row['expected']['rows'], ordered),
                        "candidate_columns": cand['columns'],
                        "gold_columns": gold_columns[row['id']],
                        "columns_match": cand['columns'] == gold_columns[row['id']] == row['expected']['columns'],
                        "query_status": "executed",
                    })
                    rec["pass"] = bool(rec["result_match"] and rec["static_match"] and rec['columns_match'])
                except Exception as exc:  # noqa: BLE001
                    if not is_candidate_sql_error(exc):
                        raise OracleEnvironmentError('evaluation executor environment failed') from exc
                    rec.update({"query_status": "execution_failed",
                                "error": f"{type(exc).__name__}: {exc}", "pass": False})
                results.append(rec)
            elif row["expected_kind"] == "clarify":
                rec["pass"] = rec["protocol_ok"]
                rec["query_status"] = "n/a"
                results.append(rec)
            else:
                rec["pass"] = rec["protocol_ok"]
                rec["query_status"] = "n/a"
                results.append(rec)
    finally:
        _cleanup_owned_sandbox(pgdir, srv or (lease.server if lease else None), conn, owner,
                               original_error=sys.exception(), lease=lease)

    struct = score_rows(rows, preds, query_validator=lambda sql, p: bool(validate(sql, p).ok))
    c = Counter()
    for rec in results:
        c[f"{rec['expected_kind']}_pass"] += int(bool(rec["pass"]))
        if rec["expected_kind"] == "query":
            c["query_01_valid"] += int(rec.get("validator01") is True)
            c["query_03_valid"] += int(bool(rec.get("validator03")))
            c["query_exec"] += int(rec.get("query_status") == "executed")
            c["query_match"] += int(bool(rec.get("result_match")))
            c["query_static"] += int(bool(rec.get("static_match")))
    total = Counter(r["expected_kind"] for r in rows)
    summary = {
        **{k: v for k, v in struct["summary"].items() if k not in ("per_template",)},
        "test": args.test, "predictions": args.predictions,
        "consumer01_root": str(root01),
        "rows": len(rows),
        "query_total": total["query"], "clarify_total": total["clarify"], "unsupported_total": total["unsupported"],
        "query_business_pass": c["query_pass"],
        "query_business_pass_rate": round(c["query_pass"] / max(total["query"], 1), 4),
        "query_result_match_rate": round(c["query_match"] / max(total["query"], 1), 4),
        "query_static_match_rate": round(c["query_static"] / max(total["query"], 1), 4),
        "query_01_valid_rate": round(c["query_01_valid"] / max(total["query"], 1), 4),
        "query_03_valid_rate": round(c["query_03_valid"] / max(total["query"], 1), 4),
        "query_exec_rate": round(c["query_exec"] / max(total["query"], 1), 4),
        "clarify_pass_rate": round(c["clarify_pass"] / max(total["clarify"], 1), 4),
        "unsupported_pass_rate": round(c["unsupported_pass"] / max(total["unsupported"], 1), 4),
        "oracle_error": False,
        "gold_checked": len(query_rows),
        "boundary": "strict JSON only; 03 temp PG; 01 validator from source; gold checked before candidates; no 01/02 DB",
    }
    _write(args.output, {"summary": summary, "results": results,
                         "evaluation_identity": evaluation_identity(rows,root01)})
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def _params(row: dict) -> dict:
    params = {}
    window = row.get("window") or {}
    if window:
        params.update({"start_at": window["start"], "end_at": window["end"]})
    params.update(row.get("parameters") or {})
    return params


def _write(path: str, data: dict) -> None:
    out = ROOT / path
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
