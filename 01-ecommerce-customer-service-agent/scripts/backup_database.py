"""迁移前应用库逻辑备份；不输出凭据，不停止或删除服务器资源。"""

import argparse
import hashlib
import json
import os
import subprocess

from cs_agent.config import PROJECT_ROOT, Settings
from cs_agent.db.session import verify_target
from cs_agent.runtime_profiles import apply_runtime_profile
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url

from scripts.snapshot_database import snapshot


def backup(profile, label):
    apply_runtime_profile(profile)
    directory = PROJECT_ROOT / ".runtime/backups"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{label}.dump"
    if path.exists():
        raise RuntimeError("backup already exists; do not overwrite history")
    url = make_url(os.environ["CS_MIGRATION_URL"])
    engine = create_engine(url, hide_parameters=True)
    verify_target(engine, Settings(expected_database=url.database), migration=True)
    engine.dispose()
    before = snapshot(profile)
    env = {
        **os.environ,
        "PGHOST": url.host,
        "PGPORT": str(url.port),
        "PGUSER": url.username,
        "PGPASSWORD": url.password,
        "PGDATABASE": url.database,
    }
    executable = PROJECT_ROOT / ".runtime/postgres/pgsql/bin/pg_dump.exe"
    # FORCE RLS保持开启；迁移角色已有USING(true)，启用RLS导出全部授权行。
    # INSERT格式可用于RLS启用的恢复目标；不能把失败的部分文件视作备份。
    result = subprocess.run(
        [
            str(executable),
            "--format=custom",
            "--enable-row-security",
            "--inserts",
            "--no-owner",
            "--no-acl",
            "--exclude-table=cs_project_guard",
            "--file",
            str(path),
        ],
        env=env,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        (PROJECT_ROOT / ".runtime/backup-private-diagnostic.txt").write_bytes(
            result.stderr
        )
        raise RuntimeError("database backup failed; raw diagnostics kept private")
    check = subprocess.run(
        [str(executable.with_name("pg_restore.exe")), "--list", str(path)],
        capture_output=True,
        check=False,
    )
    if check.returncode:
        raise RuntimeError("backup catalog unreadable")
    metadata = {
        "database": url.database,
        "schema_revision": before["schema_revision"],
        "bytes": path.stat().st_size,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "catalog_readable": True,
        "restore_rehearsal": False,
        "tables": len(before["tables"]),
    }
    (directory / f"{label}-before.json").write_text(
        json.dumps(before, indent=2) + "\n", encoding="utf8"
    )
    (directory / f"{label}-metadata.json").write_text(
        json.dumps(metadata, indent=2) + "\n", encoding="utf8"
    )
    print(json.dumps(metadata))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", choices=["debian", "local"], required=True)
    parser.add_argument("--label", choices=["v1-before-0020"], required=True)
    args = parser.parse_args()
    backup(args.profile, args.label)
