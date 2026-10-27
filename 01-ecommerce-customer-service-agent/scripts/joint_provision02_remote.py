"""同一PG实例供给02独立联调库；不修改01业务表或重建容器。"""

import getpass
import json
import secrets
import sys
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import URL, create_engine, text

ROOT = Path(__file__).resolve().parents[1]
OPS = ROOT.parent / "02-ecommerce-operations-agent"
RUN = "XOPS-20261023-01"
PRIVATE = OPS / ".runtime" / RUN
sys.path.insert(0, str(ROOT / ".runtime/ssh-tools"))
import paramiko


def main():
    configuration = PRIVATE / "database-private.json"
    if configuration.exists():
        raise RuntimeError("run database already configured; refusing replacement")
    PRIVATE.mkdir(parents=True, exist_ok=True)
    role = "ops02_app_xops_20261023_01"
    database = "ops02_joint_xops_20261023_01"
    password = secrets.token_urlsafe(36)
    instance = secrets.token_hex(24)
    client = paramiko.SSHClient()
    client.load_host_keys(str(ROOT / ".runtime/ssh/known_hosts"))
    supplied = getpass.getpass("SSH password (not logged): ")
    try:
        client.connect("192.168.10.100", username="zzh", password=supplied,
                       look_for_keys=False, allow_agent=False, timeout=5, auth_timeout=5)
        supplied = None

        def run_sql(target, statement):
            # 目标和角色为本脚本固定值，秘密只经stdin传输。
            channel_in, output, error = client.exec_command(
                "docker exec -i cs01-db-postgres-1 psql -X -v ON_ERROR_STOP=1 -U cs01_bootstrap -d " + target,
                timeout=15,
            )
            channel_in.write(statement)
            channel_in.channel.shutdown_write()
            data, diagnostic = output.read(), error.read()
            if output.channel.recv_exit_status():
                (PRIVATE / "provision-private-diagnostic.log").write_bytes(diagnostic)
                raise RuntimeError("02 provisioning failed; private diagnostic preserved")
            return data

        # 禁止覆盖现有角色/库；如同名已存在，保留现场并失败。
        run_sql("postgres", f"CREATE ROLE {role} LOGIN PASSWORD '{password}' NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS CONNECTION LIMIT 8;\n"
                f"CREATE DATABASE {database} OWNER {role} TEMPLATE template0 ENCODING 'UTF8';\n"
                f"REVOKE CONNECT ON DATABASE {database} FROM PUBLIC;\n"
                f"GRANT CONNECT ON DATABASE {database} TO {role};\n")
        run_sql(database, "CREATE TABLE ops_project_guard(project_id TEXT PRIMARY KEY,instance_id TEXT NOT NULL);\n"
                f"INSERT INTO ops_project_guard VALUES ('ops02','{instance}');\n"
                f"REVOKE ALL ON ops_project_guard FROM PUBLIC; GRANT SELECT ON ops_project_guard TO {role};\n")
    finally:
        client.close()
    url = URL.create("postgresql+psycopg", username=role, password=password,
                     host="192.168.10.100", port=55432, database=database)
    configuration.write_text(json.dumps({"url": url.render_as_string(hide_password=False),
                                         "integration_run_id": RUN}), encoding="utf-8")
    identity = {"host": "192.168.10.100", "port": 55432, "database": database,
                "current_user": role, "instance_id": instance}
    (PRIVATE / "database-identity.json").write_text(json.dumps(identity, indent=2), encoding="utf-8")
    engine = create_engine(url, hide_parameters=True, pool_size=1, max_overflow=0,
                           connect_args={"connect_timeout": 3, "options": "-c statement_timeout=2000"})
    try:
        with engine.connect() as connection:
            actual = connection.execute(text("SELECT current_database(),current_user,inet_server_port(),"
                                              "(SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname=current_user),"
                                              "(SELECT instance_id FROM ops_project_guard WHERE project_id='ops02')")).one()
            if tuple(actual) != (database, role, 5432, False, instance):
                raise RuntimeError("02 isolated database identity mismatch")
            version = connection.execute(text("SHOW server_version")).scalar_one()
        evidence = {**identity, "integration_run_id": RUN, "captured_at": datetime.now(UTC).isoformat(),
                    "server_port": 5432, "postgresql": version, "super_or_bypassrls": False,
                    "same_instance_as_01": True, "same_logical_database_as_01": False,
                    "app_pool": 5, "app_max_overflow": 0, "role_connection_limit": 8,
                    "instance_max_connections": 60, "container_recreated": False}
        destination = OPS / "docs/06-acceptance" / RUN / "02-database.json"
        destination.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(evidence, ensure_ascii=False))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
