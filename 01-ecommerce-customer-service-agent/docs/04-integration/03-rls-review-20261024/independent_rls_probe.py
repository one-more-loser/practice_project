"""01-owned disposable PostgreSQL probe. Never connects to existing profiles or 03.

Replays source 0011 then 0012 verbatim over source-shaped tables. Deliberately
omits FKs to tables outside this RLS slice; not a full migration/lifecycle test.
All mutations are guarded by a fresh cluster path, random marker and port.
"""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from sqlalchemy import create_engine, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable
from sqlalchemy.engine import URL

HERE = Path(__file__).resolve().parent
ROOT = next(p for p in HERE.parents if (p / "backend/src/cs_agent").is_dir())
sys.path.insert(0, str(ROOT / "backend/src"))
from cs_agent.db.models import Base
from cs_agent.core.context import Role, TrustedContext
from cs_agent.query.executor import SQLExecutor

TABLES = ("customer_order", "order_item", "order_shipment", "wallet_account", "wallet_entry", "product", "sku", "inventory")
KEYS = dict(zip(TABLES, ("order_id", "order_item_id", "shipment_id", "account_id", "entry_id", "product_id", "sku_id", "sku_id")))
VIEWS = dict(zip(("cs_order_self", "cs_order_item_self", "cs_shipment_self", "cs_wallet_summary_self", "cs_wallet_entry_self", "cs_product_public"), ("order_id", "order_item_id", "shipment_id", "account_id", "entry_id", "product_id")))
NAMES = {"source_batch", "store", "subject", *TABLES}
MARKER = "cs01_rls_review_" + secrets.token_hex(8)
RUNBASE = Path("C:/Users/27997/.codex/visualizations/2026/10/21/01a15d31-2974-74b2-ba9e-cbb18c618df3/cs01-rls-review-runtime")
PRIVATE = RUNBASE / MARKER
DATA = PRIVATE / "pgdata"
BIN = RUNBASE / "pgsql/bin"
NOW = datetime(2026, 10, 24, 4, tzinfo=UTC)
REPORT = {"review_id": "CS03-RLS-REVIEW-20261024-v1", "started_at": datetime.now(UTC).isoformat(), "scope": "fresh 01-owned Windows PostgreSQL only; source 0011/0012; no existing profile/03 writes", "cases": [], "limitations": ["source-shaped 11-table RLS slice, no external FKs/triggers/full lifecycle migration", "direct limited login and real SQLExecutor.context; no HTTP/model workload", "03 PostgreSQL16.2 report compared to local PostgreSQL17.11, not a PG16 rerun"]}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def command(args):
    # Windows pg_ctl children may inherit pipes; use a regular log file so a
    # healthy long-running server cannot keep communicate() waiting for EOF.
    output = PRIVATE / "last-command.txt"
    with output.open("w", encoding="utf-8") as stream:
        result = subprocess.run([str(x) for x in args], stdout=stream, stderr=subprocess.STDOUT, timeout=35, creationflags=subprocess.CREATE_NO_WINDOW)
    rendered = output.read_text(encoding="utf-8", errors="replace")
    if result.returncode:
        # Commands never contain passwords; log still remains in the private cluster.
        (PRIVATE / "command-error.txt").write_text(rendered, encoding="utf-8")
        raise RuntimeError("private cluster command failed; see private command-error.txt")
    return rendered.strip()


def guard(c):
    actual = c.execute(text("SELECT current_setting('data_directory'),current_setting('port'),current_setting('cluster_name'),current_database() ")).one()
    assert Path(actual[0]).resolve() == DATA.resolve()
    assert actual[1] == str(PORT) and actual[2] == MARKER and actual[3] == "postgres"


def capture(name):
    path = ROOT / "backend/migrations/versions" / (name + ".py")
    spec = importlib.util.spec_from_file_location("probe_" + name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    statements = []
    module.op = SimpleNamespace(execute=lambda sql: statements.append(str(sql)))
    module.upgrade()
    (HERE / (name + "-captured.sql")).write_text(";\n\n".join(statements) + ";\n", encoding="utf-8")
    return statements


def catalog(c):
    return [dict(r) for r in c.execute(text("SELECT tablename,policyname,permissive,roles,cmd,qual,with_check FROM pg_policies WHERE schemaname='public' ORDER BY tablename,policyname")).mappings() if r["tablename"] in TABLES]


def fixture(c):
    tables = Base.metadata.tables
    for batch, gen, status in (("batch_1", 1, "active"), ("batch_2", 2, "active"), ("batch_dead", 1, "inactive")):
        c.execute(tables["source_batch"].insert().values(batch_id=batch, generation=gen, seed=24, fingerprint="0"*64, manifest_revision="rls-review-v1", status=status))
    for store, status in (("store_a", "active"), ("store_b", "inactive")):
        c.execute(tables["store"].insert().values(store_id=store, name=store, status=status, data_batch_id="batch_1", batch_generation=1))
    for owner in ("cust_a", "cust_b", "cust_batch2", "cust_dead"):
        c.execute(tables["subject"].insert().values(subject_id=owner, store_id="store_a", subject_type="customer", login_name=owner, password_digest="not-a-login", data_batch_id="batch_1", batch_generation=1))
    groups = (("A1", "cust_a", "store_a", "batch_1", 1), ("A2", "cust_b", "store_a", "batch_1", 1), ("C3", "cust_a", "store_b", "batch_1", 1), ("G2", "cust_batch2", "store_a", "batch_2", 2), ("DEAD", "cust_dead", "store_a", "batch_dead", 1))
    recorded = []
    for label, owner, store, batch, gen in groups:
        common = dict(store_id=store, data_batch_id=batch, batch_generation=gen, created_at=NOW-timedelta(days=2), updated_at=NOW, source_kind="synthetic", version=1)
        rows = {
            "product": dict(product_id="P_"+label, name=label, category="test", description="synthetic RLS fixture", image_slug="test", status="active", source_revision="rls-v1", source_fingerprint="0"*64),
            "sku": dict(sku_id="S_"+label, product_id="P_"+label, specifications={}, price_minor=1000, currency="CNY", price_version=1, status="active"),
            "inventory": dict(sku_id="S_"+label, stock_qty=10, reserved_qty=0),
            "customer_order": dict(order_id=label, owner_id=owner, display_no="TEST-"+label, quote_id="Q_"+label, trade_status="paid", fulfillment_status="in_transit", currency="CNY", goods_minor=1000, discount_minor=0, freight_minor=0, payable_minor=1000, paid_minor=1000, refunded_minor=0, refunded_goods_minor=0, refunded_freight_minor=0, policy_snapshot={}, current_shipping_id="SHIP_"+label, change_count=0, payment_expires_at=NOW, paid_at=NOW-timedelta(days=2)),
            "order_item": dict(order_item_id="I_"+label, order_id=label, sku_id="S_"+label, product_name=label, specifications={}, image_slug="test", unit_price_minor=1000, ordered_qty=1, goods_minor=1000, discount_minor=0, allocated_minor=1000, paid_goods_minor=1000, unit_allocations=[1000], allocation_revision="test-v1", refunded_qty=0, reserved_refund_qty=0, refunded_goods_minor=0, reserved_refund_minor=0),
            "order_shipment": dict(shipment_id="H_"+label, order_id=label, operation_id="OP_"+label, shipping_snapshot_id="SHIP_"+label, carrier="synthetic", tracking_no="TRACK_"+label, status="in_transit", shipped_at=NOW-timedelta(days=1), tracking_updated_at=NOW, tracking_events=[]),
            "wallet_account": dict(account_id="W_"+label, owner_id=owner, currency="CNY", balance_minor=1000, ledger_sequence=1, status="active"),
            "wallet_entry": dict(entry_id="E_"+label, account_id="W_"+label, owner_id=owner, sequence=1, kind="topup", currency="CNY", amount_minor=1000, balance_before_minor=0, balance_after_minor=1000, business_ref="REF_"+label, occurred_at=NOW-timedelta(days=2), recorded_at=NOW),
        }
        for name, values in rows.items():
            row = {**common, **values}
            c.execute(tables[name].insert().values(**row))
            recorded.append({"table": name, "values": row})
    (HERE / "synthetic-fixture.json").write_text(json.dumps(recorded, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    REPORT["fixture_digest"] = digest(recorded)


def context(c, owner="cust_a", store="store_a", batch="batch_1", gen=1):
    ctx = TrustedContext(owner, store, Role.CUSTOMER, "probe-session", 1, batch, gen, "probe-request")
    SQLExecutor.context(c, ctx, NOW, {"start_at": NOW-timedelta(days=30), "end_at": NOW})


def snapshot(c):
    result = {}
    for name, key in KEYS.items():
        result[name] = list(c.scalars(text(f"SELECT {key} FROM public.{name} ORDER BY {key}")))
    for name, key in VIEWS.items():
        result[name] = list(c.scalars(text(f"SELECT {key} FROM csq.{name} ORDER BY {key}")))
    return result


def case(name, scope=None, *, expect_empty=False, expected=None, connection=None, rollback=False):
    c = connection or query.connect()
    try:
        transaction = c.begin()
        if scope is not None:
            context(c, **scope)
        actual = snapshot(c)
        passed = all(not rows for rows in actual.values()) if expect_empty else actual == expected if expected is not None else True
        row = {"name": name, "scope": scope, "backend_pid": c.scalar(text("SELECT pg_backend_pid()")), "rows": actual, "passed": passed}
        REPORT["cases"].append(row)
        transaction.rollback() if rollback else transaction.commit()
        return actual
    finally:
        if connection is None:
            c.close()


def expected(label, public_labels=None):
    public_labels = public_labels or [label]
    r = {"customer_order":[label], "order_item":["I_"+label], "order_shipment":["H_"+label], "wallet_account":["W_"+label], "wallet_entry":["E_"+label], "product":sorted("P_"+i for i in public_labels), "sku":sorted("S_"+i for i in public_labels), "inventory":sorted("S_"+i for i in public_labels)}
    for name, table in zip(VIEWS, ("customer_order", "order_item", "order_shipment", "wallet_account", "wallet_entry", "product")):
        r[name] = r[table]
    return r


def compare_deployed(current):
    file = HERE / "deployed-policy-catalog.json"
    if not file.exists():
        return
    canonical = {(r["tablename"], r["policyname"]): {k:r[k] for k in ("permissive", "roles", "cmd", "qual", "with_check")} for r in current}
    comparisons = {}
    for db, value in json.loads(file.read_text(encoding="utf-8"))["databases"].items():
        deployed = {(r["tablename"], r["policyname"]): {k:r[k] for k in ("permissive", "roles", "cmd", "qual", "with_check")} for r in value.get("policies", [])}
        comparisons[db] = {"policy_count": len(deployed), "matching": [f"{t}.{p}" for (t,p),v in canonical.items() if deployed.get((t,p)) == v], "different_or_missing": [f"{t}.{p}" for (t,p),v in canonical.items() if deployed.get((t,p)) != v], "extra": [f"{t}.{p}" for t,p in deployed.keys()-canonical.keys()], "rls_enabled_forced_all_8": len(value.get("table_flags", [])) == 8 and all(r["relrowsecurity"] and r["relforcerowsecurity"] for r in value["table_flags"])}
    REPORT["deployed_comparison"] = comparisons


admin = query = None
started = False
try:
    PRIVATE.mkdir(parents=True, exist_ok=False)
    password = secrets.token_urlsafe(24)
    pwfile = PRIVATE / "bootstrap.pw"
    pwfile.write_text(password, encoding="ascii")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        PORT = sock.getsockname()[1]
    command([BIN / "initdb.exe", "-D", DATA, "-U", "cs01_rls_probe_admin", "--auth-local=trust", "--auth-host=scram-sha-256", "--pwfile", pwfile, "--encoding=UTF8", "--locale=C"])
    pwfile.unlink()
    command([BIN / "pg_ctl.exe", "-D", DATA, "-l", PRIVATE / "postgres.log", "-w", "-t", "15", "-o", f"-h 127.0.0.1 -p {PORT} -c cluster_name={MARKER} -c max_connections=12 -c shared_buffers=16MB", "start"])
    started = True
    admin = create_engine(URL.create("postgresql+psycopg", username="cs01_rls_probe_admin", password=password, host="127.0.0.1", port=PORT, database="postgres"), hide_parameters=True, pool_size=1, max_overflow=0, connect_args={"connect_timeout":3})
    query_password = secrets.token_urlsafe(24)
    with admin.begin() as c:
        guard(c)
        REPORT["server"] = {"version":c.scalar(text("SELECT version()")), "cluster_marker": MARKER, "port": PORT, "fresh_path": str(DATA)}
        for role in ("cs_agent_owner_b0", "cs_agent_app_b0"):
            c.exec_driver_sql(f"CREATE ROLE {role} NOLOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOINHERIT")
        c.exec_driver_sql(f"CREATE ROLE cs_agent_query_b2 LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOINHERIT PASSWORD '{query_password}'")
        c.exec_driver_sql("ALTER ROLE cs_agent_query_b2 SET default_transaction_read_only=on")
        omitted = []
        for table in Base.metadata.sorted_tables:
            if table.name not in NAMES:
                continue
            keep = []
            for fk in table.foreign_key_constraints:
                if all(element.target_fullname.split('.')[0] in NAMES for element in fk.elements):
                    keep.append(fk)
                else:
                    omitted.append({"table":table.name,"targets":[e.target_fullname for e in fk.elements]})
            c.exec_driver_sql(str(CreateTable(table, include_foreign_key_constraints=keep).compile(dialect=postgresql.dialect())))
        REPORT["omitted_external_fks"] = omitted
        fixture(c)
        statements11, statements12 = capture("0011_controlled_query"), capture("0012_query_scope")
        for statement in statements11:
            c.exec_driver_sql(statement)
        REPORT["catalog_0011"] = catalog(c)
    query = create_engine(URL.create("postgresql+psycopg", username="cs_agent_query_b2", password=query_password, host="127.0.0.1", port=PORT, database="postgres"), hide_parameters=True, pool_size=1, max_overflow=0, connect_args={"connect_timeout":3,"options":"-c default_transaction_read_only=on -c timezone=UTC -c statement_timeout=2000 -c lock_timeout=500"})
    with query.connect() as c:
        REPORT["query_role"] = dict(c.execute(text("SELECT current_user,rolsuper,rolbypassrls,rolinherit FROM pg_roles WHERE rolname=current_user")).mappings().one())
    case("0011_inactive_B_another_A_active_expected_historical_leak", {"store":"store_b"}, expected=expected("C3"))
    with query.connect() as c, c.begin():
        context(c, store="store_b")
        REPORT["explain_0011"] = c.execute(text("EXPLAIN (FORMAT JSON) SELECT order_id FROM csq.cs_order_self")).scalar_one()
    with admin.begin() as c:
        guard(c)
        for statement in statements12:
            c.exec_driver_sql(statement)
        REPORT["catalog_0012"] = catalog(c)
        compare_deployed(REPORT["catalog_0012"])
    case("0012_active_A_own_rows", {}, expected=expected("A1", ["A1", "A2"]))
    case("0012_inactive_B_another_A_active", {"store":"store_b"}, expect_empty=True)
    case("0012_other_owner_A", {"owner":"cust_b"}, expected=expected("A2", ["A1", "A2"]))
    public_only = expected("A1", ["A1", "A2"])
    for name in ("customer_order", "order_item", "order_shipment", "wallet_account", "wallet_entry", *tuple(VIEWS)[:5]):
        public_only[name] = []
    case("0012_unknown_owner_private_empty_public_catalog_visible", {"owner":"not_owner"}, expected=public_only)
    case("0012_other_store_no_data", {"store":"store_unknown"}, expect_empty=True)
    case("0012_other_batch_unknown", {"batch":"batch_unknown"}, expect_empty=True)
    case("0012_wrong_generation", {"gen":2}, expect_empty=True)
    case("0012_matching_second_batch_generation", {"owner":"cust_batch2", "batch":"batch_2", "gen":2}, expected=expected("G2"))
    case("0012_inactive_batch", {"owner":"cust_dead", "batch":"batch_dead"}, expect_empty=True)
    case("0012_no_context", None, expect_empty=True)
    # Partial GUC cases distinguish missing date/owner/role from entirely absent context.
    with query.connect() as c:
        for missing in ("start", "end", "owner", "store", "batch", "generation", "role"):
            with c.begin():
                context(c)
                c.execute(text("SELECT set_config(:key,'',true)"), {"key":"cs."+missing})
                rows = snapshot(c)
                if missing in {"owner", "role"}:
                    wanted = public_only
                elif missing in {"start", "end"}:
                    wanted = expected("A1", ["A1", "A2"])
                    for name in ("customer_order", "order_item", "order_shipment", "wallet_entry", "cs_order_self", "cs_order_item_self", "cs_shipment_self", "cs_wallet_entry_self"):
                        wanted[name] = []
                else:
                    wanted = {name:[] for name in rows}
                REPORT["cases"].append({"name":"0012_missing_"+missing,"rows":rows,"passed":rows==wanted})
    with admin.begin() as c:
        guard(c)
        c.exec_driver_sql("UPDATE public.store SET status='inactive' WHERE store_id='store_a'")
    case("0012_all_stores_inactive", {}, expect_empty=True)
    with admin.begin() as c:
        guard(c)
        c.exec_driver_sql("UPDATE public.store SET status='active' WHERE store_id='store_a'")
    # One physical connection: commit, rollback, SQL error, then unbound transactions.
    with query.connect() as c:
        case("reuse_A_commit", {}, expected=expected("A1", ["A1", "A2"]), connection=c)
        case("reuse_no_context_after_commit", None, expect_empty=True, connection=c)
        case("reuse_B_rollback", {"store":"store_b"}, expect_empty=True, connection=c, rollback=True)
        case("reuse_no_context_after_rollback", None, expect_empty=True, connection=c)
        try:
            with c.begin():
                context(c)
                c.exec_driver_sql("SELECT 1/0")
        except Exception as exc:
            REPORT["expected_injected_error_type"] = type(exc).__name__
        case("reuse_no_context_after_error_rollback", None, expect_empty=True, connection=c)
        case("reuse_A_rebound", {}, expected=expected("A1", ["A1", "A2"]), connection=c)
    with query.connect() as c, c.begin():
        context(c, store="store_b")
        REPORT["explain_0012"] = c.execute(text("EXPLAIN (FORMAT JSON) SELECT order_id FROM csq.cs_order_self")).scalar_one()
    REPORT["result"] = "passed" if all(row["passed"] for row in REPORT["cases"]) else "failed"
except Exception as exc:
    REPORT["result"] = "harness_error"
    REPORT["error"] = {"type":type(exc).__name__, "message":str(exc)[:400] if isinstance(exc, (AssertionError, RuntimeError)) else "details suppressed; inspect source-shaped fixture privately"}
finally:
    if query is not None:
        query.dispose()
    if admin is not None:
        admin.dispose()
    if started:
        try:
            assert DATA.parent == PRIVATE and PRIVATE.parent == RUNBASE
            command([BIN / "pg_ctl.exe", "-D", DATA, "-w", "-t", "15", "-m", "fast", "stop"])
            with socket.socket() as sock:
                REPORT["cleanup"] = {"owned_cluster_stopped":True,"port_closed":sock.connect_ex(("127.0.0.1", PORT)) != 0}
        except Exception as exc:
            REPORT["cleanup"] = {"owned_cluster_stopped":False,"error_type":type(exc).__name__}
    REPORT["finished_at"] = datetime.now(UTC).isoformat()
    if (PRIVATE / "bootstrap.pw").exists():
        (PRIVATE / "bootstrap.pw").unlink()
    (HERE / "independent-rls-result.json").write_text(json.dumps(REPORT, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps({"result":REPORT.get("result"),"cases":len(REPORT["cases"]),"passed":sum(r["passed"] for r in REPORT["cases"]),"deployed_comparison":REPORT.get("deployed_comparison"),"cleanup":REPORT.get("cleanup"),"error":REPORT.get("error")}, ensure_ascii=False))
sys.exit(0 if REPORT.get("result") == "passed" else 1)
