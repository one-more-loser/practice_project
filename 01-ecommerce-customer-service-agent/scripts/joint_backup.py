"""本run独立备份/恢复演练；只允许固定upgrade做恢复，绝不DROP public。"""

import argparse
import hashlib
import json
import os
import subprocess

from alembic import command
from alembic.config import Config
from cs_agent.config import PROJECT_ROOT, Settings
from cs_agent.db.models import Base
from cs_agent.db.session import verify_target
from cs_agent.runtime_profiles import apply_runtime_profile
from sqlalchemy import create_engine, inspect, text

RUN = "XOPS-20261023-01"
PRIVATE = PROJECT_ROOT / ".runtime" / RUN
BIN = PROJECT_ROOT / ".runtime/postgres/pgsql/bin"


def engine_for(kind, values):
    key = {"app": "CS_MIGRATION_URL", "test": "CS_TEST_MIGRATION_URL", "upgrade": "CS_UPGRADE_MIGRATION_URL"}[kind]
    engine = create_engine(values[key], hide_parameters=True, pool_size=1, max_overflow=0,
                           connect_args={"connect_timeout": 3, "options": "-c statement_timeout=10000"})
    verify_target(engine, Settings(expected_database=engine.url.database), migration=True)
    return engine


def fingerprint(engine):
    result = {"tables": {}}
    with engine.connect() as connection:
        result["schema_revision"] = connection.scalar(text("SELECT version_num FROM alembic_version"))
        result["guard"] = connection.scalar(text("SELECT instance_id FROM cs_project_guard"))
        result["extensions"] = dict(connection.execute(text("SELECT extname,extversion FROM pg_extension")).all())
        for table in sorted(inspect(connection).get_table_names()):
            if table == "cs_project_guard":
                continue
            # 只保存行数/摘要；完整备份只在私密runtime。
            rows = connection.execute(text('SELECT to_jsonb(t)::text FROM public."' + table + '" t ORDER BY to_jsonb(t)::text')).scalars()
            digest, count = hashlib.sha256(), 0
            for row in rows:
                digest.update(row.encode("utf-8")); digest.update(b"\n"); count += 1
            result["tables"][table] = {"rows": count, "sha256": digest.hexdigest()}
        result["acl"] = [dict(row) for row in connection.execute(text(
            "SELECT table_schema,table_name,grantee,privilege_type FROM information_schema.table_privileges "
            "WHERE grantee IN ('cs_agent_app_b0','cs_agent_query_b2') ORDER BY 1,2,3,4")).mappings()]
        result["roles"] = [dict(row) for row in connection.execute(text(
            "SELECT rolname,rolsuper,rolbypassrls,rolcreatedb,rolcreaterole FROM pg_roles "
            "WHERE rolname IN ('cs_agent_owner_b0','cs_agent_app_b0','cs_agent_query_b2') ORDER BY 1")).mappings()]
    return result


def pg_env(url):
    return {**os.environ, "PGHOST": url.host, "PGPORT": str(url.port), "PGUSER": url.username,
            "PGPASSWORD": url.password, "PGDATABASE": url.database}


def backup(kind):
    _, values = apply_runtime_profile("debian")
    engine = engine_for(kind, values)
    destination = PRIVATE / (kind + "-before.dump")
    if destination.exists():
        raise RuntimeError("backup exists; history is never overwritten")
    before = fingerprint(engine)
    result = subprocess.run([str(BIN / "pg_dump.exe"), "--format=custom", "--enable-row-security", "--inserts",
                             "--no-owner", "--exclude-table=cs_project_guard", "--file", str(destination)],
                            env=pg_env(engine.url), capture_output=True, timeout=120, check=False)
    (PRIVATE / (kind + "-backup.stderr.log")).write_bytes(result.stderr)
    if result.returncode:
        raise RuntimeError("backup failed; private diagnostic preserved")
    catalog = subprocess.run([str(BIN / "pg_restore.exe"), "--list", str(destination)], capture_output=True, check=True).stdout
    (PRIVATE / (kind + "-before.catalog")).write_bytes(catalog)
    (PRIVATE / (kind + "-before-state.json")).write_text(json.dumps(before, indent=2), encoding="utf-8")
    metadata = {"database": engine.url.database, "sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
                "bytes": destination.stat().st_size, "schema_revision": before["schema_revision"],
                "acl_preserved_in_dump": True, "roles_and_schema_sidecar": kind + "-before-state.json",
                "sidecar_sha256": hashlib.sha256((PRIVATE / (kind + "-before-state.json")).read_bytes()).hexdigest()}
    (PRIVATE / (kind + "-backup.json")).write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    print(json.dumps(metadata)); engine.dispose()


def restore_rehearsal():
    _, values = apply_runtime_profile("debian")
    target = engine_for("upgrade", values)
    if target.url.database != "cs_agent_b0_upgrade":
        raise RuntimeError("only guarded upgrade database may be restored")
    dump = PRIVATE / "test-before.dump"
    meta = json.loads((PRIVATE / "test-backup.json").read_text())
    expected = json.loads((PRIVATE / "test-before-state.json").read_text())
    upgrade_backup = PRIVATE / "upgrade-before.dump"
    upgrade_meta = json.loads((PRIVATE / "upgrade-backup.json").read_text())
    if (hashlib.sha256(dump.read_bytes()).hexdigest() != meta["sha256"] or
            hashlib.sha256(upgrade_backup.read_bytes()).hexdigest() != upgrade_meta["sha256"]):
        raise RuntimeError("backup integrity mismatch; target preserved")
    catalog = (PRIVATE / "test-before.catalog").read_text(encoding="utf-8")
    restore_list = PRIVATE / "test-restore.list"
    restore_list.write_text("\n".join(line for line in catalog.splitlines()
                                      if not ("EXTENSION" in line and "vector" in line)) + "\n", encoding="utf-8")
    with target.begin() as connection:
        connection.execute(text("SET LOCAL lock_timeout='3s'"))
        guard = connection.scalar(text("SELECT instance_id FROM cs_project_guard"))
        connection.execute(text("DROP SCHEMA IF EXISTS lg CASCADE"))
        connection.execute(text("DROP SCHEMA IF EXISTS csq CASCADE"))
        for table in reversed(Base.metadata.sorted_tables):
            connection.execute(text('DROP TABLE IF EXISTS public."' + table.name + '" CASCADE'))
        connection.execute(text("DROP TABLE IF EXISTS public.alembic_version"))
        assert connection.scalar(text("SELECT instance_id FROM cs_project_guard")) == guard
        assert connection.scalar(text("SELECT count(*) FROM pg_extension WHERE extname='vector'")) == 1
    result = subprocess.run([str(BIN / "pg_restore.exe"), "--no-owner", "--enable-row-security", "--exit-on-error",
                             "--use-list", str(restore_list), "--dbname", target.url.database, str(dump)],
                            env=pg_env(target.url), capture_output=True, timeout=120, check=False)
    (PRIVATE / "restore.stderr.log").write_bytes(result.stderr)
    if result.returncode:
        raise RuntimeError("guarded restore failed; private diagnostic preserved")
    restored = fingerprint(target)
    assert restored["tables"] == expected["tables"], "restored data hashes differ"
    assert restored["acl"] == expected["acl"], "restored ACL differs"
    cfg = Config(str(PROJECT_ROOT / "backend/alembic.ini"))
    cfg.attributes["database_url"] = values["CS_UPGRADE_MIGRATION_URL"]
    command.upgrade(cfg, "head")
    upgraded = fingerprint(target)
    # 升级只增表，不重写既有资金/订单；alembic_version允许变化。
    assert all(upgraded["tables"][name] == data for name, data in expected["tables"].items() if name != "alembic_version")
    evidence = {"integration_run_id": RUN, "backup_sha256": meta["sha256"], "source": "cs_agent_b0_test",
                "target": "cs_agent_b0_upgrade", "data_hashes_restored": True, "acl_restored": True,
                "guard_preserved": upgraded["guard"] == guard, "vector_preserved": upgraded["extensions"].get("vector"),
                "restored_head": restored["schema_revision"], "upgraded_head": upgraded["schema_revision"],
                "prior_order_wallet_hashes_unchanged_after_upgrade": True, "public_schema_dropped": False}
    (PROJECT_ROOT / "docs/06-acceptance" / RUN / "backup-restore.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")
    print(json.dumps(evidence)); target.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["backup-app", "backup-test", "backup-upgrade", "restore-rehearsal"])
    args = parser.parse_args()
    if args.action.startswith("backup-"):
        backup(args.action.split("-", 1)[1])
    else:
        restore_rehearsal()
