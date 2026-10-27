"""Run the 03 isolated 6-8 question PG batch.

No model, no GPU, no 01/02 database. The runner starts an embedded
PostgreSQL 16 cluster under /tmp, applies the 03 DDL and a synthetic fixture,
validates hand-written SQL with the 03-local validator, executes it as a
restricted NOBYPASSRLS role with session-scoped identity, and writes JSON
evidence. It is a target-level harness, not 01's production executor.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pgserver
import psycopg

ROOT = Path(__file__).resolve().parents[2]
SANDBOX = ROOT / "sandbox"
SQL = SANDBOX / "sql"
FIXTURES = SANDBOX / "fixtures"
CASES = SANDBOX / "cases" / "miniloop.json"
RESULTS = SANDBOX / "results"
PGDATA = Path(os.environ.get("PGSERVER_DATA", "/tmp/03-sql-pgdata"))
ROLE = "csq_agent_query"
# 0012 adds cs.start/cs.end. The fixture window is deliberately broad for
# the "all orders" case; cases with explicit start_at/end_at bind the same
# window into RLS so the closed RLS window and half-open business window
# intersect visibly. This is a test reference window, not the product default.
DEFAULT_WINDOW = {"start": "2026-09-01T00:00:00+00:00", "end": "2026-12-31T23:59:59+00:00"}
DEFAULT_SCOPE = {"owner": "cust_a", "store": "store_a", "batch": "batch_1", "generation": 1, "role": "customer", **DEFAULT_WINDOW}

sys.path.insert(0, str(SANDBOX / "runner"))
from validator import to_psycopg, validate  # noqa: E402


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def norm(value):
    if isinstance(value, Decimal):
        if value == value.to_integral_value():
            return int(value)
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, list):
        return [norm(v) for v in value]
    if isinstance(value, tuple):
        return [norm(v) for v in value]
    return value


def split_statements(text: str) -> list[str]:
    # DDL/fixture inputs contain no semicolons inside string literals.
    text = re.sub(r"--[^\n]*", "", text)
    parts = []
    for raw in text.split(";"):
        stmt = raw.strip()
        if stmt:
            parts.append(stmt)
    return parts


def apply_file(conn, path: Path) -> None:
    for stmt in split_statements(path.read_text(encoding="utf-8")):
        conn.execute(stmt)


def start_pg():
    PGDATA.mkdir(parents=True, exist_ok=True)
    os.chmod(PGDATA, 0o700)
    srv = pgserver.get_server(PGDATA, cleanup_mode=os.environ.get("PGSERVER_CLEANUP") or None)
    srv.__enter__()
    return srv


def setup_fixture(conn, fixture: str) -> None:
    conn.execute("DROP SCHEMA IF EXISTS app CASCADE")
    apply_file(conn, SQL / "00_roles.sql")
    apply_file(conn, SQL / "01_schema.sql")
    apply_file(conn, FIXTURES / f"{fixture}.sql")


def run_query(conn, sql: str, params: dict, scope: dict) -> dict:
    scope = dict(scope)
    for key in ("start", "end"):
        scope.setdefault(key, DEFAULT_WINDOW[key])
    if "start_at" in params and "end_at" in params:
        scope["start"] = str(params["start_at"])
        scope["end"] = str(params["end_at"])
    # Transaction-scoped role/config; a failing statement rolls back instead
    # of committing a half-applied transaction.
    with conn.transaction():
        conn.execute(f"SET LOCAL ROLE {ROLE}")
        conn.execute("SELECT set_config('search_path','app, pg_catalog', true)")
        conn.execute("SELECT set_config('TimeZone','UTC', true)")
        conn.execute("SET LOCAL statement_timeout = '2000ms'")
        conn.execute("SET LOCAL lock_timeout = '500ms'")
        for key, value in scope.items():
            conn.execute("SELECT set_config(%s, %s, true)", (f"cs.{key}", str(value)))
        # Server-side cap: the client-side fetchmany is not a resource limit.
        guarded = f"SELECT * FROM ({sql}) AS _q LIMIT 51"
        cur = conn.execute(to_psycopg(guarded), params)
        rows = cur.fetchmany(51)
        if len(rows) > 50:
            raise RuntimeError("row_limit_exceeded")
        columns = [d.name for d in cur.description] if cur.description else []
        data = [[norm(v) for v in row] for row in rows]
        # Scope is evaluator-injected. If a query changed cs.*, fail the case
        # rather than trusting RLS/validator to have prevented it.
        for key, value in scope.items():
            got = conn.execute("SELECT current_setting(%s, true)", (f"cs.{key}",)).fetchone()[0]
            if str(got) != str(value):
                raise RuntimeError(f"scope_changed:{key}")
    return {"columns": columns, "rows": data, "row_count": len(data)}


def check_params(case: dict) -> str:
    names = set(case.get("parameter_names") or [])
    declared = set(case.get("params") or {})
    if names != declared:
        return "invalid_parameters"
    if {"start_at", "end_at"} & names:
        if not {"start_at", "end_at"} <= names:
            return "invalid_window"
        try:
            start = datetime.fromisoformat(case["params"]["start_at"])
            end = datetime.fromisoformat(case["params"]["end_at"])
        except (KeyError, ValueError):
            return "invalid_window"
        if start.tzinfo is None or end.tzinfo is None:
            return "invalid_window"
        if end <= start:
            return "invalid_window"
        if end - start > timedelta(days=366):
            return "invalid_window"
    return "ok"


def compare(case: dict, fixture: str, actual: dict) -> tuple[bool, object]:
    expected = case["expected"]["values"][fixture]
    if case["expected"]["sku"] == "scalar":
        got = actual["rows"][0][0] if actual["rows"] else None
        return got == expected, got
    if case["expected"]["sku"] == "row":
        got = actual["rows"][0] if actual["rows"] else None
        return got == expected, got
    if case["expected"]["sku"] == "rows":
        return actual["rows"] == expected, actual["rows"]
    return False, None


def main() -> int:
    RESULTS.mkdir(parents=True, exist_ok=True)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    srv = start_pg()
    conn = psycopg.connect(srv.get_uri(), autocommit=True)
    cases_doc = json.loads(CASES.read_text(encoding="utf-8"))
    all_results = []
    security_results = []
    anomalies = []

    pg_version = conn.execute("SELECT version()").fetchone()[0]
    for fixture in ("fx_a", "fx_b", "fx_c"):
        setup_fixture(conn, fixture)
        for case in cases_doc["cases"]:
            case_id = case["id"]
            declared = case.get("parameter_names", [])
            if case["expected_kind"] == "unsupported":
                has_sql = bool(case.get("sql"))
                all_results.append({
                    "case": case_id, "fixture": fixture, "kind": "unsupported",
                    "ast_ok": not has_sql, "executed": False,
                    "result_match": None,
                    "pass": not has_sql,
                    "assertion": case["business_assertion"],
                    "note": "no SQL present; unsupported is asserted without execution",
                })
                continue
            v = validate(case["sql"], declared)
            if not v.ok:
                all_results.append({
                    "case": case_id, "fixture": fixture, "kind": "query",
                    "ast_ok": False, "ast_code": v.code, "ast_message": v.message,
                    "executed": False, "pass": False,
                })
                continue
            pcode = check_params(case)
            if pcode != "ok":
                all_results.append({
                    "case": case_id, "fixture": fixture, "kind": "query",
                    "ast_ok": True, "param_code": pcode, "executed": False, "pass": False,
                })
                continue
            try:
                params = dict(case["params"])
                params.update(case.get("params_by_fixture", {}).get(fixture, {}))
                actual = run_query(conn, case["sql"], params, DEFAULT_SCOPE)
                match, got = compare(case, fixture, actual)
                alt_match = None
                alt_rows = None
                alt_ast_ok = None
                if case.get("alt_sql"):
                    av = validate(case["alt_sql"], declared)
                    alt_ast_ok = av.ok
                    if av.ok:
                        alt = run_query(conn, case["alt_sql"], params, DEFAULT_SCOPE)
                        alt_rows = alt["rows"]
                        alt_match = alt_rows == actual["rows"]
                passed = match and (alt_match is not False)
                all_results.append({
                    "case": case_id, "fixture": fixture, "kind": "query",
                    "ast_ok": True, "executed": True,
                    "ast_sha256": v.ast_sha256, "parameter_names": v.parameter_names,
                    "columns": actual["columns"], "row_count": actual["row_count"],
                    "actual": got, "expected": case["expected"]["values"][fixture],
                    "result_match": match,
                    "alt_ast_ok": alt_ast_ok, "alt_result_match": alt_match,
                    "alt_rows": alt_rows,
                    "pass": passed,
                    "assertion": case["business_assertion"],
                })
            except Exception as exc:  # noqa: BLE001
                all_results.append({
                    "case": case_id, "fixture": fixture, "kind": "query",
                    "ast_ok": True, "executed": False, "pass": False,
                    "error": f"{type(exc).__name__}: {exc}",
                })

    # RLS / role evidence on fx_c. An anomaly is recorded, not silently failed.
    setup_fixture(conn, "fx_c")
    role_attrs = conn.execute(
        "SELECT rolname, rolsuper, rolbypassrls, rolcreatedb, rolcreaterole "
        "FROM pg_roles WHERE rolname = %s", (ROLE,)
    ).fetchone()
    security_results.append({"id": "ROLE-01", "classification": "role", "actual": list(role_attrs),
                             "expected": [ROLE, False, False, False, False],
                             "pass": list(role_attrs[1:]) == [False, False, False, False]})
    for item in cases_doc["security"]:
        if item["classification"] == "rls_scope":
            scope = dict(DEFAULT_SCOPE)
            scope.update(item.get("scope", {}))
            if not item.get("scope"):
                scope = {"role": "customer"}
            try:
                v = validate(item["sql"], [])
                if not v.ok:
                    raise RuntimeError(f"rls_sql_invalid:{v.code}:{v.message}")
                actual = run_query(conn, item["sql"], {}, scope)
                got = actual["rows"][0][0] if actual["rows"] else None
                ok = got == item["expected"]
                result = {
                    "id": item["id"], "classification": item["classification"],
                    "description": item["description"], "scope": scope,
                    "expected": item["expected"], "actual": got,
                    "pass": ok, "status": "pass" if ok else "anomaly",
                }
                if not ok:
                    anomalies.append(result)
                security_results.append(result)
            except Exception as exc:  # noqa: BLE001
                security_results.append({
                    "id": item["id"], "classification": item["classification"],
                    "pass": False, "error": f"{type(exc).__name__}: {exc}",
                    "status": "error",
                })
        elif item["classification"] == "validator":
            try:
                v = validate(item["sql"], item.get("parameter_names", []))
                ok = v.code == item["expected_code"]
                security_results.append({
                    "id": item["id"], "classification": "validator",
                    "description": item["description"], "expected_code": item["expected_code"],
                    "actual_code": v.code, "pass": ok,
                    "status": "pass" if ok else "fail",
                })
                if not ok:
                    anomalies.append(security_results[-1])
            except Exception as exc:  # noqa: BLE001
                security_results.append({"id": item["id"], "pass": False, "error": str(exc)})
        elif item["classification"] == "parameters":
            start = datetime(2026, 1, 1, tzinfo=timezone.utc)
            days = item.get("window_days", 367)
            end = start + timedelta(days=days)
            fake = {"parameter_names": ["start_at", "end_at"],
                    "params": {"start_at": start.isoformat(), "end_at": end.isoformat()}}
            code = check_params(fake)
            ok = code == item["expected_code"]
            security_results.append({
                "id": item["id"], "classification": "parameters",
                "description": item["description"], "window_days": days,
                "expected_code": item["expected_code"], "actual_code": code,
                "pass": ok, "status": "pass" if ok else "fail",
            })

    summary = {
        "cases_total": len(all_results),
        "cases_pass": sum(1 for r in all_results if r.get("pass")),
        "cases_fail": [r["case"] + "/" + r["fixture"] for r in all_results if not r.get("pass")],
        "security_total": len(security_results),
        "security_pass": sum(1 for r in security_results if r.get("pass")),
        "security_fail": [r.get("id") for r in security_results if not r.get("pass")],
        "anomalies": anomalies,
    }
    log = {
        "run_id": run_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": "sandbox/runner/run.py; hand-written SQL candidates; no model",
        "environment": {
            "pg_version": pg_version,
            "pgdata": str(PGDATA),
            "role": role_attrs,
            "sqlglot": __import__("sqlglot").__version__,
            "psycopg": psycopg.__version__,
            "python": sys.version,
            "pgserver": __import__("importlib.metadata", fromlist=["version"]).version("pgserver"),
        },
        "hashes": {
            p.name: sha(p) for p in [
                SQL / "00_roles.sql", SQL / "01_schema.sql",
                FIXTURES / "fx_a.sql", FIXTURES / "fx_b.sql", FIXTURES / "fx_c.sql",
                CASES, SANDBOX / "runner" / "validator.py",
                SANDBOX / "runner" / "run.py", SANDBOX / "requirements.txt",
            ]
        },
        "summary": summary,
        "cases": all_results,
        "security": security_results,
    }
    out = RESULTS / f"run-{run_id}.json"
    out.write_text(json.dumps(log, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"run": str(out), "summary": summary}, ensure_ascii=False, indent=2))
    conn.close()
    clean = not summary["cases_fail"] and not summary["security_fail"] and not summary["anomalies"]
    return 0 if clean else 1


if __name__ == "__main__":
    raise SystemExit(main())
