"""Inspect/start only the existing Compose container; provision only two fresh logical DBs."""
import argparse
import hashlib
import json
import secrets
import shlex
import sys
import time
from uuid import uuid4

from int20261026_common import (
    COMPOSE,
    DATABASES,
    HOST,
    OUT,
    PORT,
    PRIVATE,
    ROLE_MAP,
    ROLES,
    ROOT,
    RUN,
    SERVER_PORT,
    configuration,
    database_url,
    read,
    write,
)

sys.path.insert(0, str(ROOT / ".runtime/ssh-tools"))
import paramiko
import yaml


def connect():
    config = read(PRIVATE / "ssh-credential.private.json")
    if config.get("host") != HOST or config.get("username") != "zzh" or not config.get("password"):
        raise RuntimeError("run SSH credential missing/mismatched")
    client = paramiko.SSHClient()
    client.load_host_keys(str(ROOT / ".runtime/ssh/known_hosts"))
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    client.connect(HOST, username="zzh", password=config["password"], look_for_keys=False,
                   allow_agent=False, timeout=5, auth_timeout=5)
    return client


def command(client, value, stdin=None):
    started = time.perf_counter()
    incoming, output, error = client.exec_command(value, timeout=20)
    if stdin is not None:
        incoming.write(stdin)
        incoming.channel.shutdown_write()
    data, diagnostic = output.read(), error.read()
    code = output.channel.recv_exit_status()
    events = read(OUT / "evidence/remote-commands.json") if (OUT / "evidence/remote-commands.json").exists() else []
    events.append({"command": value, "stdin": "private SQL/auth payload withheld" if stdin else None,
                   "exit_code": code, "elapsed_ms": round((time.perf_counter() - started) * 1000, 2)})
    write(OUT / "evidence/remote-commands.json", events)
    if code:
        # May contain passwords from failed CREATE ROLE; diagnostic is private, never printed.
        (PRIVATE / "remote-private-error.log").write_bytes(diagnostic)
        raise RuntimeError("remote command failed; private diagnostic retained")
    return data.decode("utf-8").strip()


def inspect(client):
    daemon = client.exec_command("systemctl is-active docker", timeout=10)
    state = daemon[1].read().decode().strip()
    if state != "active":
        credential = read(PRIVATE / "ssh-credential.private.json")
        command(client, "sudo -S -p '' systemctl start docker", credential["password"] + "\n")
    with client.open_sftp() as sftp, sftp.open(COMPOSE) as file:
        raw = file.read()
    compose = yaml.safe_load(raw)
    # Never write raw Compose/env/config; only hashes and selected non-secret configuration.
    prefix = "docker compose -f " + shlex.quote(COMPOSE)
    rows = [json.loads(s) for s in command(client, prefix + " ps -a --format json").splitlines() if s]
    matches = [r for r in rows if r.get("Project") == "cs01-db" and r.get("Service") == "postgres"]
    if len(matches) != 1:
        raise RuntimeError("expected existing cs01-db postgres container not uniquely present; no up/create fallback")
    name = matches[0]["Name"]
    fields = {"id": ".Id", "image_id": ".Image", "image": ".Config.Image", "mounts": ".Mounts",
              "ports": ".HostConfig.PortBindings", "memory_bytes": ".HostConfig.Memory",
              "nano_cpus": ".HostConfig.NanoCpus", "pids_limit": ".HostConfig.PidsLimit",
              "labels": ".Config.Labels", "state": ".State.Status"}
    info = {k: json.loads(command(client, "docker inspect --format " + shlex.quote("{{json " + f + "}}") + " " + shlex.quote(name)))
            for k, f in fields.items()}
    labels = info.pop("labels")
    if labels.get("com.docker.compose.project") != "cs01-db" or labels.get("com.docker.compose.service") != "postgres":
        raise RuntimeError("container labels mismatch")
    ports = info["ports"].get("5432/tcp", [])
    if not any(p.get("HostPort") == str(PORT) for p in ports):
        raise RuntimeError("existing PG mapping differs; do not edit/recreate")
    mount = [m for m in info["mounts"] if m["Destination"] == "/var/lib/postgresql/data"]
    if len(mount) != 1 or mount[0]["Type"] != "volume":
        raise RuntimeError("expected PG data volume not found")
    info.update(integration_run_id=RUN, name=name, compose_project="cs01-db", service="postgres",
                compose_sha256=hashlib.sha256(raw).hexdigest(), compose_file=COMPOSE,
                compose_service_keys=sorted(compose["services"]["postgres"].keys()),
                host=HOST, port=PORT, server_port=SERVER_PORT, data_directory="/var/lib/postgresql/data",
                image_digest=command(client, "docker image inspect --format '{{json .RepoDigests}}' " + shlex.quote(info["image_id"])))
    write(OUT / "evidence/container-before.json", info)
    if info["state"] != "running":
        command(client, prefix + " start postgres")
    for _ in range(10):
        health = command(client, "docker inspect --format '{{json .State.Health.Status}}' " + shlex.quote(name))
        if json.loads(health) == "healthy":
            break
        time.sleep(1)
    else:
        raise RuntimeError("existing PG container did not become healthy within bounded polling")
    info["health"] = "healthy"
    info["state_after"] = "running"
    info["existing_container_reused"] = True
    write(OUT / "evidence/container-verified.json", info)
    return info


def sql(client, info, database, statement):
    if database not in {"postgres", *DATABASES.values()}:
        raise RuntimeError("non-run DB access forbidden")
    return command(client, "docker exec -i " + shlex.quote(info["name"])
                   + " psql -X -qAt -v ON_ERROR_STOP=1 -U cs01_bootstrap -d " + database, statement)


def provision(client, info):
    if (PRIVATE / "resources-private.json").exists():
        raise RuntimeError("private allocation exists; inspect run guards before any retry; no automatic takeover")
    names = list(DATABASES.values()) + list(ROLES.values())
    assert all(len(n) <= 63 and n.replace("_", "").isalnum() for n in names)
    quoted = ",".join("'" + n + "'" for n in names)
    conflicts = sql(client, info, "postgres", "SELECT datname FROM pg_database WHERE datname IN (" + quoted + ");"
                    "SELECT rolname FROM pg_roles WHERE rolname IN (" + quoted + ");")
    if conflicts:
        raise RuntimeError("same-name logical resource exists without this run allocation; no takeover")
    cfg = {"integration_run_id": RUN, "databases": DATABASES, "roles": ROLES,
           "passwords": {k: secrets.token_urlsafe(36) for k in ROLES},
           "instance_ids": {p: str(uuid4()) for p in DATABASES}, "service_token": secrets.token_urlsafe(48),
           "container_id": info["id"]}
    write(PRIVATE / "resources-private.json", cfg)  # partial creation can be diagnosed; no destructive retry
    statements = []
    for key, name in ROLES.items():
        statements.append(f"CREATE ROLE {name} LOGIN PASSWORD '{cfg['passwords'][key]}' NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION NOINHERIT CONNECTION LIMIT 12;")
        statements.append(f"COMMENT ON ROLE {name} IS '{RUN}:{key}';")
    # verify_target migration requires data_directory visibility; no application/query membership.
    statements.append(f"GRANT pg_read_all_settings TO {ROLES['cs_owner']} WITH INHERIT TRUE, SET FALSE;")
    for project, database in DATABASES.items():
        owner = ROLES[project + "_owner"]
        local_roles = [role for key, role in ROLES.items() if key.startswith(project + "_")]
        statements.extend([f"CREATE DATABASE {database} OWNER {owner} TEMPLATE template0 ENCODING 'UTF8';",
                           f"COMMENT ON DATABASE {database} IS '{RUN}:{cfg['instance_ids'][project]}';",
                           f"REVOKE ALL ON DATABASE {database} FROM PUBLIC;",
                           f"GRANT CONNECT ON DATABASE {database} TO {','.join(local_roles)};"])
    sql(client, info, "postgres", "\n".join(statements))
    for project, database in DATABASES.items():
        owner, app = ROLES[project + "_owner"], ROLES[project + "_app"]
        guard = project + "_project_guard" if project == "cs" else "ops_project_guard"
        instance = cfg["instance_ids"][project]
        extra = "," + ROLES["cs_query"] if project == "cs" else ""
        sql(client, info, database,
            f"REVOKE ALL ON SCHEMA public FROM PUBLIC; GRANT USAGE,CREATE ON SCHEMA public TO {owner};"
            f"GRANT USAGE ON SCHEMA public TO {app}{extra};"
            f"CREATE TABLE {guard}(project_id TEXT PRIMARY KEY,instance_id TEXT NOT NULL);"
            f"INSERT INTO {guard} VALUES ('{'cs01' if project == 'cs' else 'ops02'}','{instance}');"
            f"REVOKE ALL ON {guard} FROM PUBLIC; GRANT SELECT ON {guard} TO {owner},{app}{extra};")
        if project == "ops":
            sql(client, info, database,
                f"ALTER DEFAULT PRIVILEGES FOR ROLE {owner} IN SCHEMA public GRANT SELECT,INSERT,UPDATE,DELETE ON TABLES TO {app};"
                f"ALTER DEFAULT PRIVILEGES FOR ROLE {owner} IN SCHEMA public GRANT USAGE,SELECT ON SEQUENCES TO {app};")
    manifest = {"kind": "docker_debian", "host": HOST, "port": PORT, "server_port": SERVER_PORT,
                "data_directory": info["data_directory"], "project_root": str(ROOT), "databases": [DATABASES["cs"]],
                "owner_role": ROLES["cs_owner"], "app_role": ROLES["cs_app"], "query_role": ROLES["cs_query"],
                "instance_id": cfg["instance_ids"]["cs"], "integration_run_id": RUN, "container_id": info["id"]}
    write(PRIVATE / "isolation.json", manifest)
    from int20261026_common import INITIAL_TIME
    write(PRIVATE / "clock.json", {"integration_run_id": RUN, "now": INITIAL_TIME})
    write(PRIVATE / "models-disabled.json", {})
    for key in ("ops_owner", "ops_app"):
        name = "migration" if key.endswith("owner") else "runtime"
        path = PRIVATE / "ops02-handoff"
        write(path / (name + "-private.json"), {"integration_run_id": RUN, "url": database_url(key, cfg).render_as_string(hide_password=False)})
        write(path / (name + "-identity.json"), {"host": HOST, "port": PORT, "database": DATABASES["ops"],
            "current_user": ROLES[key], "instance_id": cfg["instance_ids"]["ops"]})
    write(PRIVATE / "ops02-handoff/service-private.json", {"integration_run_id": RUN,
          "service_token": cfg["service_token"], "stores": ["int_store_a"], "clock_file": str(PRIVATE / "clock.json")})
    public = {"integration_run_id": RUN, "databases": DATABASES, "roles": ROLES, "instance_ids": cfg["instance_ids"],
              "role_aliases": ROLE_MAP, "state": "created_guards_only", "no_old_database_modified": True,
              "ops02_business_tables_initialized": False}
    write(OUT / "evidence/logical-resources.json", public)
    return public


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=["inspect", "provision", "extension"])
    args = parser.parse_args()
    client = connect()
    try:
        if args.phase == "inspect":
            print(json.dumps(inspect(client), ensure_ascii=False))
        else:
            info = read(OUT / "evidence/container-verified.json")
            current = command(client, "docker inspect --format '{{.Id}}' " + shlex.quote(info["name"]))
            if current != info["id"]:
                raise RuntimeError("container changed since inspection")
            if args.phase == "extension":
                cfg = configuration()
                actual = sql(client, info, DATABASES["cs"], "SELECT instance_id FROM cs_project_guard WHERE project_id='cs01';")
                if actual != cfg["instance_ids"]["cs"]:
                    raise RuntimeError("run guard mismatch before extension install")
                sql(client, info, DATABASES["cs"], "CREATE EXTENSION IF NOT EXISTS vector;")
                print(json.dumps({"database": DATABASES["cs"], "extension": "vector", "bootstrap_used_for": "new DB extension only"}))
            else:
                print(json.dumps(provision(client, info), ensure_ascii=False))
    except Exception as exc:  # noqa: BLE001 - prevent private credentials from entering public output
        print("Failed: " + type(exc).__name__ + "; details retained privately where available", file=sys.stderr)
        raise SystemExit(1) from None
    finally:
        client.close()
