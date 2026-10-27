"""只在固定upgrade库恢复备份；从不删除或恢复应用库。"""

import hashlib
import json
import os
import subprocess

from alembic import command
from alembic.config import Config
from cs_agent.config import PROJECT_ROOT, Settings
from cs_agent.db.models import Base
from cs_agent.db.session import verify_target
from cs_agent.main import create_app
from cs_agent.runtime_profiles import apply_runtime_profile
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.engine import make_url

ROOT = PROJECT_ROOT


def digest_tables(engine, names):
    result = {}
    with engine.connect() as conn:
        for table in Base.metadata.sorted_tables:
            if table.name not in names:
                continue
            h = hashlib.sha256()
            count = 0
            for row in conn.execute(
                select(table).order_by(*table.primary_key.columns)
            ).mappings():
                h.update(
                    json.dumps(
                        dict(row),
                        sort_keys=True,
                        ensure_ascii=False,
                        default=str,
                        separators=(",", ":"),
                    ).encode()
                )
                h.update(b"\n")
                count += 1
            result[table.name] = {"rows": count, "sha256": h.hexdigest()}
    return result


def main():
    _, values = apply_runtime_profile("debian")
    dump = ROOT / ".runtime/backups/v1-before-0020.dump"
    metadata = json.loads(dump.with_name("v1-before-0020-metadata.json").read_text())
    expected = json.loads(dump.with_name("v1-before-0020-before.json").read_text())
    assert hashlib.sha256(dump.read_bytes()).hexdigest() == metadata["sha256"]
    url = make_url(values["CS_UPGRADE_MIGRATION_URL"])
    if url.database != "cs_agent_b0_upgrade":
        raise RuntimeError("fixed upgrade database required")
    target = create_engine(url, hide_parameters=True)
    verify_target(target, Settings(expected_database=url.database), migration=True)
    # 原备份不含ACL；安全侧车必须随备份保留，不能在事故恢复时依赖源库在线。
    sidecar = dump.with_name("v1-before-0020-acl.json")
    if not sidecar.exists():
        source = create_engine(values["CS_MIGRATION_URL"], hide_parameters=True)
        verify_target(source, Settings(expected_database="cs_agent_b0"), migration=True)
        resources = (
            [("public", name) for name in [*expected["tables"], "alembic_version"]]
            + [("csq", name) for name in inspect(source).get_view_names(schema="csq")]
            + [("lg", name) for name in inspect(source).get_table_names(schema="lg")]
        )
        acl = {"tables": [], "columns": [], "schemas": []}
        roles = ["cs_agent_app_b0", "cs_agent_query_b2"]
        with source.connect() as conn:
            for schema, name in resources:
                for role in roles:
                    acl["tables"] += [
                        dict(row)
                        for row in conn.execute(
                            text(
                                "SELECT table_schema,table_name,grantee,privilege_type FROM information_schema.table_privileges WHERE table_schema=:s AND table_name=:t AND grantee=:r"
                            ),
                            {"s": schema, "t": name, "r": role},
                        ).mappings()
                    ]
                acl["columns"] += [
                    dict(row)
                    for row in conn.execute(
                        text(
                            "SELECT table_schema,table_name,column_name,grantee,privilege_type FROM information_schema.column_privileges WHERE table_schema=:s AND table_name=:t AND grantee='cs_agent_query_b2'"
                        ),
                        {"s": schema, "t": name},
                    ).mappings()
                ]
            for schema in ["public", "lg", "csq"]:
                for role in roles:
                    if conn.scalar(
                        text("SELECT has_schema_privilege(:r,:s,'USAGE')"),
                        {"r": role, "s": schema},
                    ):
                        acl["schemas"].append({"schema": schema, "role": role})
        sidecar.write_text(json.dumps(acl, indent=2) + "\n", encoding="utf8")
        source.dispose()
    acl = json.loads(sidecar.read_text())
    if (
        metadata.get("acl_sidecar_sha256")
        and hashlib.sha256(sidecar.read_bytes()).hexdigest()
        != metadata["acl_sidecar_sha256"]
    ):
        raise RuntimeError(
            "ACL sidecar checksum mismatch; target must remain untouched"
        )
    executable = str(ROOT / ".runtime/postgres/pgsql/bin/pg_restore.exe")
    catalog = subprocess.run(
        [executable, "--list", str(dump)], capture_output=True, check=True
    ).stdout.decode("utf8")
    # 目标已由bootstrap安装vector；不借恢复把extension所有权授给业务迁移角色。
    restore_list = dump.with_name("v1-before-0020-restore.list")
    restore_list.write_text(
        "\n".join(
            line
            for line in catalog.splitlines()
            if not ("EXTENSION" in line and "vector" in line)
        )
        + "\n",
        encoding="utf8",
    )
    with target.begin() as conn:
        conn.execute(text("SET LOCAL lock_timeout='3s'"))
        guard_before = conn.scalar(
            text("SELECT instance_id FROM public.cs_project_guard")
        )
        conn.execute(text("DROP SCHEMA IF EXISTS lg CASCADE"))
        conn.execute(text("DROP SCHEMA IF EXISTS csq CASCADE"))
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(text(f'DROP TABLE IF EXISTS public."{table.name}" CASCADE'))
        conn.execute(text("DROP TABLE IF EXISTS public.alembic_version"))
        if (
            conn.scalar(text("SELECT instance_id FROM public.cs_project_guard"))
            != guard_before
            or conn.scalar(
                text("SELECT count(*) FROM pg_extension WHERE extname='vector'")
            )
            != 1
        ):
            raise RuntimeError("cleanup crossed protected guard/extension boundary")
    env = {
        **os.environ,
        "PGHOST": url.host,
        "PGPORT": str(url.port),
        "PGUSER": url.username,
        "PGPASSWORD": url.password,
        "PGDATABASE": url.database,
    }
    restored = subprocess.run(
        [
            executable,
            "--no-owner",
            "--no-acl",
            "--enable-row-security",
            "--exit-on-error",
            "--use-list",
            str(restore_list),
            "--dbname",
            url.database,
            str(dump),
        ],
        env=env,
        capture_output=True,
        check=False,
    )
    if restored.returncode:
        (ROOT / ".runtime/restore-private-diagnostic.txt").write_bytes(restored.stderr)
        raise RuntimeError("fixed restore failed; private diagnostic retained")
    permitted = set(expected["tables"]) | {
        "alembic_version",
        "cs_order_self",
        "cs_order_item_self",
        "cs_shipment_self",
        "cs_wallet_summary_self",
        "cs_wallet_entry_self",
        "cs_product_public",
        "checkpoint_migrations",
        "checkpoints",
        "checkpoint_blobs",
        "checkpoint_writes",
    }
    with target.begin() as conn:
        for item in acl["schemas"]:
            assert item["schema"] in {"public", "lg", "csq"} and item["role"] in {
                "cs_agent_app_b0",
                "cs_agent_query_b2",
            }
            conn.execute(
                text(f"GRANT USAGE ON SCHEMA {item['schema']} TO {item['role']}")
            )
        for item in acl["tables"] + acl["columns"]:
            assert (
                item["table_schema"] in {"public", "lg", "csq"}
                and item["table_name"] in permitted
            )
            assert item["grantee"] in {"cs_agent_app_b0", "cs_agent_query_b2"} and item[
                "privilege_type"
            ] in {
                "SELECT",
                "INSERT",
                "UPDATE",
                "DELETE",
                "TRUNCATE",
                "REFERENCES",
                "TRIGGER",
            }
            column = item.get("column_name")
            if column is not None and not column.replace("_", "").isalnum():
                raise RuntimeError("invalid frozen column ACL")
            privilege = item["privilege_type"] + (f' ("{column}")' if column else "")
            conn.execute(
                text(
                    f'GRANT {privilege} ON {item["table_schema"]}."{item["table_name"]}" TO {item["grantee"]}'
                )
            )
    assert digest_tables(target, expected["tables"]) == expected["tables"], (
        "restored content mismatch"
    )
    config = Config(str(ROOT / "backend/alembic.ini"))
    config.attributes["database_url"] = values["CS_UPGRADE_MIGRATION_URL"]
    command.upgrade(config, "head")
    assert digest_tables(target, expected["tables"]) == expected["tables"], (
        "forward upgrade changed existing contents"
    )
    app_url = (
        make_url(values["CS_TEST_DATABASE_URL"])
        .set(database=url.database)
        .render_as_string(hide_password=False)
    )
    with TestClient(
        create_app(
            Settings(
                database_url=app_url,
                expected_database=url.database,
                cookie_secure=False,
                agent_worker_enabled=False,
                expiration_worker_enabled=False,
            )
        )
    ) as client:
        assert client.get("/health/ready").status_code == 200
        accounts = json.loads((ROOT / ".runtime/demo-accounts.json").read_text())
        assert (
            client.post(
                "/api/v1/session",
                headers={"Origin": "http://127.0.0.1:5173"},
                json={"login_name": "customer_a", "password": accounts["customer_a"]},
            ).status_code
            == 200
        )
        assert client.get("/api/v1/me/wallet").status_code == 200
    report = {
        "target_database": "cs_agent_b0_upgrade",
        "backup_sha256": metadata["sha256"],
        "restored_source_revision": metadata["schema_revision"],
        "original_tables_compared": len(expected["tables"]),
        "original_content_matches": True,
        "forward_upgrade_to_current_head": True,
        "post_upgrade_content_matches": True,
        "acl_sidecar_sha256": hashlib.sha256(sidecar.read_bytes()).hexdigest(),
        "restored_application_role_ready_login_wallet": True,
        "source_application_modified": False,
        "limits": "one controlled isolated rehearsal; private ACL sidecar must accompany no-ACL backup",
    }
    path = ROOT / "docs/06-acceptance/V1-business/backup-restore.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf8")
    print(json.dumps(report))
    target.dispose()


if __name__ == "__main__":
    main()
