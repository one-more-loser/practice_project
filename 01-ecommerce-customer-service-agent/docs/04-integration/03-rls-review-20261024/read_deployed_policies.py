"""Read deployed 01 policy catalogs only, no business rows, DDL, GUC identity or writes."""
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import create_engine, text

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "backend/src/cs_agent").is_dir())
sys.path.insert(0, str(ROOT / "backend/src"))
from cs_agent.config import Settings
from cs_agent.db.session import verify_target
from cs_agent.runtime_profiles import profile_values

TABLES = ("customer_order", "order_item", "order_shipment", "wallet_account", "wallet_entry", "product", "sku", "inventory")
REPORT = {"captured_at": datetime.now(UTC).isoformat(), "mode": "read-only REPEATABLE READ; metadata only; no business row query", "databases": {}}
_, values = profile_values("debian")
for key, database in (("CS_MIGRATION_URL", "cs_agent_b0"), ("CS_TEST_MIGRATION_URL", "cs_agent_b0_test"), ("CS_UPGRADE_MIGRATION_URL", "cs_agent_b0_upgrade")):
    engine = create_engine(values[key], hide_parameters=True, pool_size=1, max_overflow=0, isolation_level="REPEATABLE READ",
        connect_args={"connect_timeout": 3, "options": "-c default_transaction_read_only=on -c statement_timeout=5000 -c lock_timeout=500"})
    try:
        verify_target(engine, Settings(expected_database=database, isolation_file=Path(values["CS_ISOLATION_FILE"])), migration=True)
        with engine.connect() as connection:
            identity = dict(connection.execute(text("SELECT current_database() AS database,current_user AS role,current_setting('transaction_read_only') AS read_only,current_setting('server_version') AS server_version,current_setting('transaction_isolation') AS isolation")).mappings().one())
            assert identity["database"] == database and identity["read_only"] == "on"
            rows = [dict(row) for row in connection.execute(text("SELECT schemaname,tablename,policyname,permissive,roles,cmd,qual,with_check FROM pg_policies WHERE schemaname='public' ORDER BY tablename,policyname")).mappings() if row["tablename"] in TABLES]
            flags = [dict(row) for row in connection.execute(text("SELECT c.relname,c.relrowsecurity,c.relforcerowsecurity,pg_get_userbyid(c.relowner) AS owner FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND c.relkind='r' ORDER BY c.relname")).mappings() if row["relname"] in TABLES]
            role = dict(connection.execute(text("SELECT rolname,rolsuper,rolbypassrls,rolcreaterole,rolcreatedb,rolinherit,rolcanlogin FROM pg_roles WHERE rolname='cs_agent_query_b2'")).mappings().one())
            memberships = [dict(row) for row in connection.execute(text("SELECT member.rolname AS member,parent.rolname AS parent FROM pg_auth_members m JOIN pg_roles member ON member.oid=m.member JOIN pg_roles parent ON parent.oid=m.roleid WHERE member.rolname='cs_agent_query_b2'")).mappings()]
            REPORT["databases"][database] = {"identity": identity, "guard_verified": True,
                "alembic_head": connection.scalar(text("SELECT version_num FROM alembic_version")), "policies": rows,
                "table_flags": flags, "query_role": role, "query_role_memberships": memberships,
                "self_equality_policies": [row["tablename"] for row in rows if row["policyname"] == "b2_query" and "s.store_id = s.store_id" in row["qual"]]}
    except Exception as exc:
        REPORT["databases"][database] = {"status": "unverified", "error_type": type(exc).__name__, "reason": "read-only metadata connection/guard/catalog unavailable; raw DSN and exception suppressed"}
    finally:
        engine.dispose()
path = Path(__file__).resolve().parent / "deployed-policy-catalog.json"
path.write_text(json.dumps(REPORT, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
print(json.dumps({name: {"verified": "policies" in value, "head": value.get("alembic_head"), "bad_policy_tables": value.get("self_equality_policies"), "error_type": value.get("error_type")} for name, value in REPORT["databases"].items()}, ensure_ascii=False))
