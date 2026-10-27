"""仅管理本项目.runtime内B0本地验证实例；不注册系统服务、不操作旧数据库。"""

import argparse
import ctypes
import hashlib
import json
import os
import secrets
import subprocess
import zipfile
from pathlib import Path

import psycopg
from psycopg import sql

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / ".runtime"
DATA = RUNTIME / "pgdata"
BIN = RUNTIME / "postgres/pgsql/bin"
PORT = 55432
DATABASES = ["cs_agent_b0", "cs_agent_b0_test", "cs_agent_b0_upgrade"]
OWNER = "cs_agent_owner_b0"
APP = "cs_agent_app_b0"


def run(*args):
    return subprocess.run(
        [str(a) for a in args],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def load_secrets():
    return json.loads((RUNTIME / "db-secrets.json").read_text(encoding="utf-8"))


def conninfo(database, role):
    return psycopg.conninfo.make_conninfo(
        host="127.0.0.1", port=PORT, dbname=database, user=role, password=load_secrets()[role]
    )


def ascii_runtime():
    """EDB17的initdb把中文路径写入SQL时使用系统代码页。

    临时DOS路径别名只指向本项目.runtime；不创建目录副本、不改注册表或系统区域。
    stop时撤销。实际文件始终写在本项目，迁移仍验证解析后的真实路径。
    """
    marker = RUNTIME / "dos-alias.json"
    target = "\\??\\" + str(RUNTIME)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    if marker.exists():
        letter = json.loads(marker.read_text())["letter"]
        buf = ctypes.create_unicode_buffer(32768)
        if kernel.QueryDosDeviceW(letter, buf, len(buf)):
            if buf.value != target:
                raise RuntimeError("DOS alias occupied by another target")
            return Path(letter + "/")
    else:
        drives = kernel.GetLogicalDrives()
        letter = next(chr(i + 65) + ":" for i in range(25, 17, -1) if not drives & (1 << i))
    if not kernel.DefineDosDeviceW(9, letter, target):
        raise OSError(ctypes.get_last_error(), "cannot create temporary project path alias")
    marker.write_text(json.dumps({"letter": letter}), encoding="utf-8")
    return Path(letter + "/")


def bootstrap():
    RUNTIME.mkdir(exist_ok=True)
    os.environ["TEMP"] = os.environ["TMP"] = str(RUNTIME / "tmp")
    if not BIN.exists():
        archive = RUNTIME / "downloads/postgresql-17.11-3-windows-x64-binaries.zip"
        with zipfile.ZipFile(archive) as bundle:
            for entry in bundle.infolist():
                if entry.filename.startswith(("pgsql/bin/", "pgsql/lib/", "pgsql/share/")):
                    target = (RUNTIME / "postgres" / entry.filename).resolve()
                    if not target.is_relative_to(RUNTIME / "postgres"):
                        raise RuntimeError("invalid archive path")
                    bundle.extract(entry, RUNTIME / "postgres")
    alias = ascii_runtime()
    native_bin = alias / "postgres/pgsql/bin"
    native_data = alias / "pgdata"
    secret_file = RUNTIME / "db-secrets.json"
    if not secret_file.exists():
        secret_file.write_text(
            json.dumps({r: secrets.token_urlsafe(32) for r in ("cs_bootstrap_b0", OWNER, APP)}),
            encoding="utf-8",
        )
    # 凭据目录只允许当前用户；不会打印内容或写入可追踪文档。
    account = subprocess.check_output(["whoami"], text=True).strip()
    run("icacls", RUNTIME, "/inheritance:r", "/grant:r", f"{account}:(OI)(CI)F")
    if not (DATA / "PG_VERSION").exists():
        pwfile = RUNTIME / "init-password"
        pwfile.write_text(load_secrets()["cs_bootstrap_b0"], encoding="utf-8")
        try:
            run(
                native_bin / "initdb.exe",
                "-D",
                native_data,
                "-U",
                "cs_bootstrap_b0",
                "--pwfile",
                alias / "init-password",
                "--auth=scram-sha-256",
                "--encoding=UTF8",
                "--locale=C",
            )
        finally:
            pwfile.unlink(missing_ok=True)
        with (DATA / "postgresql.conf").open("a", encoding="utf-8") as file:
            file.write(
                f"\nlisten_addresses='127.0.0.1'\nport={PORT}\ntimezone='UTC'\nlog_statement='none'\nlog_min_error_statement='panic'\n"
            )
    status = subprocess.run(
        [str(native_bin / "pg_ctl.exe"), "-D", str(native_data), "status"],
        check=False,
        capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if status.returncode:
        # 端口被其他实例占用会失败；不杀进程、不改旧实例端口。
        run(
            native_bin / "pg_ctl.exe",
            "-D",
            native_data,
            "-l",
            alias / "postgres.log",
            "-w",
            "start",
        )
    with psycopg.connect(conninfo("postgres", "cs_bootstrap_b0"), autocommit=True) as conn:
        actual = conn.execute("SHOW data_directory").fetchone()[0]
        if Path(actual).resolve() != DATA.resolve():
            raise RuntimeError("cluster directory mismatch")
        for role in (OWNER, APP):
            if not conn.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (role,)).fetchone():
                conn.execute(
                    sql.SQL(
                        "CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS PASSWORD {}"
                    ).format(sql.Identifier(role), sql.Literal(load_secrets()[role]))
                )
        conn.execute(sql.SQL("GRANT pg_read_all_settings TO {}").format(sql.Identifier(OWNER)))
        for database in DATABASES:
            if not conn.execute(
                "SELECT 1 FROM pg_database WHERE datname=%s", (database,)
            ).fetchone():
                conn.execute(
                    sql.SQL("CREATE DATABASE {} OWNER {} ENCODING 'UTF8'").format(
                        sql.Identifier(database), sql.Identifier(OWNER)
                    )
                )
    manifest_file = RUNTIME / "isolation.json"
    if not manifest_file.exists():
        manifest_file.write_text(
            json.dumps(
                {
                    "project_root": str(ROOT),
                    "port": PORT,
                    "databases": DATABASES,
                    "owner_role": OWNER,
                    "app_role": APP,
                    "instance_id": secrets.token_hex(16),
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    marker = json.loads(manifest_file.read_text(encoding="utf-8"))
    for database in DATABASES:
        with psycopg.connect(conninfo(database, "cs_bootstrap_b0"), autocommit=True) as conn:
            conn.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
            conn.execute("REVOKE ALL ON DATABASE " + database + " FROM PUBLIC")
            conn.execute(
                sql.SQL("GRANT CONNECT ON DATABASE {} TO {}, {}").format(
                    sql.Identifier(database), sql.Identifier(OWNER), sql.Identifier(APP)
                )
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS cs_project_guard (instance_id text PRIMARY KEY)"
            )
            existing = conn.execute("SELECT instance_id FROM cs_project_guard").fetchall()
            if not existing:
                conn.execute("INSERT INTO cs_project_guard VALUES (%s)", (marker["instance_id"],))
            elif existing != [(marker["instance_id"],)]:
                raise RuntimeError("project guard mismatch")
            conn.execute(
                sql.SQL("GRANT USAGE ON SCHEMA public TO {}, {}").format(
                    sql.Identifier(OWNER), sql.Identifier(APP)
                )
            )
            conn.execute(
                sql.SQL("GRANT SELECT ON cs_project_guard TO {}, {}").format(
                    sql.Identifier(OWNER), sql.Identifier(APP)
                )
            )
    from sqlalchemy import URL

    def url(database, role):
        return URL.create(
            "postgresql+psycopg",
            username=role,
            password=load_secrets()[role],
            host="127.0.0.1",
            port=PORT,
            database=database,
        ).render_as_string(hide_password=False)

    lines = {
        "CS_DATABASE_URL": url(DATABASES[0], APP),
        "CS_TEST_DATABASE_URL": url(DATABASES[1], APP),
        "CS_MIGRATION_URL": url(DATABASES[0], OWNER),
        "CS_TEST_MIGRATION_URL": url(DATABASES[1], OWNER),
        "CS_UPGRADE_MIGRATION_URL": url(DATABASES[2], OWNER),
        "CS_COOKIE_SECURE": "false",
    }
    (RUNTIME / "local.env").write_text(
        "\n".join(f'{k}="{v}"' for k, v in lines.items()) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "status": "isolated",
                "host": "127.0.0.1",
                "port": PORT,
                "databases": DATABASES,
                "roles_superuser": False,
                "data_directory": str(DATA),
                "archive_sha256": hashlib.file_digest(
                    (RUNTIME / "downloads/postgresql-17.11-3-windows-x64-binaries.zip").open("rb"),
                    "sha256",
                ).hexdigest(),
            }
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["start", "stop"])
    args = parser.parse_args()
    if args.action == "start":
        bootstrap()
    else:
        # 只关闭数据目录路径一致的项目实例。
        if DATA.resolve().parent != RUNTIME.resolve():
            raise RuntimeError("invalid data directory")
        alias = ascii_runtime()
        run(
            alias / "postgres/pgsql/bin/pg_ctl.exe",
            "-D",
            alias / "pgdata",
            "-m",
            "fast",
            "-w",
            "stop",
        )
        marker = RUNTIME / "dos-alias.json"
        letter = json.loads(marker.read_text())["letter"]
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        if not kernel.DefineDosDeviceW(15, letter, "\\??\\" + str(RUNTIME)):
            raise OSError(ctypes.get_last_error(), "cannot remove project DOS alias")
        marker.unlink()
        print("project database stopped")
