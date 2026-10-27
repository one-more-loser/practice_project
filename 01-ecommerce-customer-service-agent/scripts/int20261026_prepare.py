"""Run-only guarded migration copies and synthetic fixture; never reset or seed legacy DBs."""
import argparse
import hashlib
import json
import re
import shutil
import sys
import time

from int20261026_common import (
    DATABASES,
    OUT,
    PRIVATE,
    ROLE_MAP,
    ROLES,
    ROOT,
    apply_cs,
    configuration,
    database_url,
    write,
)

sys.path.insert(0, str(ROOT / "backend/src"))


def engine_for(key):
    from sqlalchemy import create_engine
    return create_engine(database_url(key), hide_parameters=True, pool_size=1, max_overflow=0,
                         connect_args={"connect_timeout": 3, "options": "-c statement_timeout=15000 -c timezone=UTC"})


def verify():
    from cs_agent.config import Settings
    from cs_agent.db.session import verify_target
    from sqlalchemy import text
    from sqlalchemy.exc import OperationalError
    apply_cs()
    cfg = configuration()
    results = []
    for key in ROLES:
        engine = engine_for(key)
        try:
            if key.startswith("cs_"):
                verify_target(engine, Settings(), migration=key == "cs_owner", query=key == "cs_query")
            with engine.connect() as conn:
                row = dict(conn.execute(text("SELECT current_database() AS database,current_user AS role,inet_server_port() AS server_port,"
                    "rolsuper,rolbypassrls,rolcreatedb,rolcreaterole,rolreplication,rolinherit,rolconnlimit FROM pg_roles WHERE rolname=current_user")).mappings().one())
                guard = "cs_project_guard" if key.startswith("cs_") else "ops_project_guard"
                expected = cfg["instance_ids"]["cs" if key.startswith("cs_") else "ops"]
                assert conn.execute(text("SELECT instance_id FROM " + guard)).scalar_one() == expected
                assert not any(row[k] for k in ("rolsuper", "rolbypassrls", "rolcreatedb", "rolcreaterole", "rolreplication"))
                row["memberships"] = list(conn.execute(text("SELECT parent.rolname FROM pg_auth_members m JOIN pg_roles parent ON parent.oid=m.roleid JOIN pg_roles child ON child.oid=m.member WHERE child.rolname=current_user")).scalars())
                assert set(row["memberships"]) <= ({"pg_read_all_settings"} if key == "cs_owner" else set())
                row["guard_verified"] = True
                # 02 initialization belongs to 02; public schema currently contains its guard only.
                if key == "ops_owner":
                    tables = list(conn.execute(text("SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename")).scalars())
                    row["public_tables"] = tables
                    assert tables == ["ops_project_guard"]
            other = DATABASES["ops" if key.startswith("cs_") else "cs"]
            from sqlalchemy import create_engine
            forbidden = create_engine(database_url(key).set(database=other), hide_parameters=True, connect_args={"connect_timeout": 3})
            try:
                with forbidden.connect():
                    raise AssertionError("cross-project CONNECT unexpectedly allowed")
            except OperationalError as exc:
                code = getattr(exc.orig, "sqlstate", None)
                # libpq connection errors may omit SQLSTATE; verify ACL in the allowed DB too.
                with engine.connect() as conn:
                    assert not conn.execute(text("SELECT has_database_privilege(current_user,:db,'CONNECT')"), {"db": other}).scalar_one()
                row["cross_project_connect"] = {"target": other, "result": "denied", "sqlstate": code}
            finally:
                forbidden.dispose()
            results.append(row)
        finally:
            engine.dispose()
    write(OUT / "evidence/role-guards.json", results)
    print(json.dumps({"allowed_connections": len(results), "cross_project_denied": len(results), "guards": "passed"}))


def migrate():
    from alembic import command
    from alembic.config import Config
    from cs_agent.config import Settings
    from cs_agent.db.session import verify_target
    from sqlalchemy import text
    apply_cs("cs_owner")
    engine = engine_for("cs_owner")
    try:
        verify_target(engine, Settings(), migration=True)
        with engine.connect() as conn:
            tables = set(conn.execute(text("SELECT tablename FROM pg_tables WHERE schemaname='public'")).scalars())
            if tables != {"cs_project_guard"}:
                raise RuntimeError("fresh-run migration precondition not met; inspect partial state, no automatic retry/reset")
    finally:
        engine.dispose()
    target = PRIVATE / "alembic"
    if target.exists():
        raise RuntimeError("migration copy exists; inspect previous execution, no overwrite")
    shutil.copytree(ROOT / "backend/migrations", target, ignore=shutil.ignore_patterns("__pycache__"))
    rewritten = []
    original_hashes = {}
    for path in sorted((target / "versions").glob("*.py")):
        original = ROOT / "backend/migrations/versions" / path.name
        content = original.read_text(encoding="utf-8")
        original_hashes[path.name] = hashlib.sha256(original.read_bytes()).hexdigest()
        updated = re.sub(r"\b(?:cs_agent_owner_b0|cs_agent_app_b0|cs_agent_query_b2)\b", lambda m: ROLE_MAP[m[0]], content)
        path.write_text(updated, encoding="utf-8")
        if updated != content:
            rewritten.append(path.name)
    config = Config(str(ROOT / "backend/alembic.ini"))
    config.set_main_option("script_location", str(target))
    config.attributes["database_url"] = database_url("cs_owner").render_as_string(hide_password=False)
    started = time.perf_counter()
    command.upgrade(config, "head")
    engine = engine_for("cs_owner")
    try:
        with engine.connect() as conn:
            version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
            policies = [dict(row) for row in conn.execute(text("SELECT tablename,policyname,roles,qual,with_check FROM pg_policies WHERE schemaname='public' ORDER BY tablename,policyname")).mappings()]
            for row in policies:
                assert not set(row["roles"]) & ROLE_MAP.keys()
            assert version == "0023_operations_snapshot"
    finally:
        engine.dispose()
    assert all(hashlib.sha256((ROOT / "backend/migrations/versions" / p).read_bytes()).hexdigest() == value for p, value in original_hashes.items())
    write(OUT / "evidence/migration.json", {"database": DATABASES["cs"], "head": version,
          "elapsed_ms": round((time.perf_counter() - started) * 1000, 2), "role_map": ROLE_MAP,
          "copy_directory": str(target), "rewritten_role_references_only": rewritten,
          "original_hashes": original_hashes, "policies": policies, "historical_files_unchanged": True})
    print(json.dumps({"database": DATABASES["cs"], "head": version, "migration": "passed"}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=["verify", "migrate", "seed"])
    phase = parser.parse_args().phase
    try:
        if phase == "verify":
            verify()
        elif phase == "migrate":
            migrate()
        else:
            from int20261026_seed import seed
            seed()
    except Exception as exc:  # noqa: BLE001 - redact credential-bearing SQL exceptions at the CLI boundary
        # SQLAlchemy exceptions can embed passwords/parameters; diagnostics remain in run-private ACL.
        import traceback
        (PRIVATE / ("prepare-" + phase + "-private-error.log")).write_text(traceback.format_exc(), encoding="utf-8")
        print("Failed: " + type(exc).__name__ + "; private diagnostic retained", file=sys.stderr)
        raise SystemExit(1) from None
