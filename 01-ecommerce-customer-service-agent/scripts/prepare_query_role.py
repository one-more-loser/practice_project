"""仅在已核对项目集群创建独立只读查询角色；不改旧角色口令，不输出凭据。"""

import argparse
import json
import secrets

import psycopg
from psycopg import sql
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url

from cs_agent.config import PROJECT_ROOT, Settings
from cs_agent.db.session import verify_target
from cs_agent.runtime_profiles import profile_values


def main(profile):
    _, values = profile_values(profile)
    owner_url = make_url(values["CS_MIGRATION_URL"])
    marker_path = PROJECT_ROOT / ".runtime" / (
        "isolation-debian.json" if profile == "debian" else "isolation.json"
    )
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    engine = create_engine(owner_url, hide_parameters=True)
    verify_target(engine, Settings(expected_database=owner_url.database, isolation_file=marker_path), migration=True)
    engine.dispose()
    role = "cs_agent_query_b2"
    secret_path = PROJECT_ROOT / ".runtime" / f"{profile}-query-secret.json"
    existed = secret_path.exists()
    secret = json.loads(secret_path.read_text())["password"] if existed else secrets.token_hex(36)
    if not existed:
        secret_path.write_text(json.dumps({"password":secret}), encoding="utf-8")
    if profile == "debian":
        bootstrap_role = "cs01_bootstrap"
        bootstrap_password = json.loads((PROJECT_ROOT/".runtime/debian-db-secrets.json").read_text())["bootstrap_password"]
    else:
        bootstrap_role = "cs_bootstrap_b0"
        bootstrap_password = json.loads((PROJECT_ROOT/".runtime/db-secrets.json").read_text())[bootstrap_role]
    bootstrap = owner_url.set(drivername="postgresql", username=bootstrap_role, password=bootstrap_password)
    with psycopg.connect(bootstrap.render_as_string(hide_password=False), autocommit=True) as connection:
        actual = connection.execute("SELECT current_database(), inet_server_port(), (SELECT instance_id FROM cs_project_guard)").fetchone()
        if actual != (owner_url.database, marker.get("server_port", marker["port"]), marker["instance_id"]):
            raise RuntimeError("bootstrap isolation mismatch")
        current = connection.execute("SELECT rolsuper,rolbypassrls,rolcreaterole,rolcreatedb FROM pg_roles WHERE rolname=%s", (role,)).fetchone()
        if current is not None and (not existed or any(current)):
            raise RuntimeError("unexpected existing query role")
        if current is None:
            connection.execute(sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS NOINHERIT PASSWORD {}").format(sql.Identifier(role), sql.Literal(secret)))
        for database in marker["databases"]:
            connection.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(sql.Identifier(database), sql.Identifier(role)))
    for database in marker["databases"]:
        with psycopg.connect(bootstrap.set(database=database).render_as_string(hide_password=False), autocommit=True) as connection:
            if connection.execute("SELECT instance_id FROM cs_project_guard").fetchall() != [(marker["instance_id"],)]:
                raise RuntimeError("database guard mismatch")
            connection.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(sql.Identifier(role)))
            connection.execute(sql.SQL("GRANT SELECT ON cs_project_guard TO {}").format(sql.Identifier(role)))
    marker["query_role"] = role
    marker_path.write_text(json.dumps(marker,ensure_ascii=False,indent=2)+"\n", encoding="utf-8")
    env_path = PROJECT_ROOT / ".runtime" / ("debian.env" if profile=="debian" else "local.env")
    content = env_path.read_text(encoding="utf-8")
    content = "\n".join(line for line in content.splitlines() if not line.startswith(("CS_QUERY_DATABASE_URL=", "CS_TEST_QUERY_DATABASE_URL=")))
    for key, database in (("CS_QUERY_DATABASE_URL", "cs_agent_b0"),("CS_TEST_QUERY_DATABASE_URL", "cs_agent_b0_test")):
        value=owner_url.set(username=role,password=secret,database=database).render_as_string(hide_password=False)
        content+=f'\n{key}="{value}"'
    env_path.write_text(content+"\n",encoding="utf-8")
    print("Project query role and private URLs prepared; existing business data unchanged")


if __name__=="__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("--profile",choices=["local","debian"],required=True)
    main(parser.parse_args().profile)
