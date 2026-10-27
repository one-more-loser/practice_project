"""受控迁移核对：只输出每表行数和有序内容SHA256，不导出字段值。"""

import argparse
import hashlib
import json
from pathlib import Path

from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.engine import make_url

from cs_agent.config import PROJECT_ROOT, Settings
from cs_agent.db.models import Base
from cs_agent.db.session import verify_target
from cs_agent.runtime_profiles import profile_values


def snapshot(profile):
    _, values = profile_values(profile)
    url = values["CS_MIGRATION_URL"]
    engine = create_engine(url, hide_parameters=True)
    verify_target(
        engine,
        Settings(
            expected_database=make_url(url).database,
            isolation_file=Path(values["CS_ISOLATION_FILE"]),
        ),
        migration=True,
    )
    result = {"profile": profile, "tables": {}}
    with engine.connect().execution_options(isolation_level="REPEATABLE READ") as connection:
        result["schema_revision"] = connection.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one()
        for table in Base.metadata.sorted_tables:
            if not inspect(connection).has_table(table.name):
                continue
            digest = hashlib.sha256()
            count = 0
            for row in connection.execute(
                select(table).order_by(*table.primary_key.columns)
            ).mappings():
                digest.update(
                    json.dumps(
                        dict(row),
                        sort_keys=True,
                        ensure_ascii=False,
                        default=str,
                        separators=(",", ":"),
                    ).encode()
                )
                digest.update(b"\n")
                count += 1
            result["tables"][table.name] = {"rows": count, "sha256": digest.hexdigest()}
    engine.dispose()
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", choices=["local", "debian"], required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    target = args.output.resolve()
    if not target.is_relative_to(PROJECT_ROOT / ".runtime"):
        raise RuntimeError("snapshot must stay in project runtime")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(snapshot(args.profile), indent=2) + "\n", encoding="utf-8")
    print("Database content snapshot captured; only row counts and hashes exported")
