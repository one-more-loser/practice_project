"""隔离守卫通过后准备LangGraph原生PG表；普通请求不执行DDL。"""

import argparse
from pathlib import Path

import psycopg
from cs_agent.config import Settings
from cs_agent.db.session import verify_target
from cs_agent.runtime_profiles import apply_runtime_profile
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg.rows import dict_row
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url


def setup(url, isolation_file):
    parsed = make_url(url)
    engine = create_engine(parsed, hide_parameters=True)
    try:
        verify_target(
            engine,
            Settings(
                expected_database=parsed.database, isolation_file=Path(isolation_file)
            ),
            migration=True,
        )
        with engine.connect() as connection:
            if (
                connection.execute(
                    text("SELECT to_regnamespace('lg') IS NOT NULL")
                ).scalar_one()
                is not True
            ):
                raise RuntimeError("runtime migration must precede checkpoint setup")
    finally:
        engine.dispose()
    with psycopg.connect(
        parsed.set(drivername="postgresql").render_as_string(hide_password=False),
        autocommit=True,
        row_factory=dict_row,
        connect_timeout=3,
        options="-c search_path=lg,public -c timezone=UTC",
    ) as connection:
        PostgresSaver(connection).setup()
        connection.execute(
            "GRANT SELECT ON lg.checkpoint_migrations TO cs_agent_app_b0"
        )
        for table in ("checkpoints", "checkpoint_blobs", "checkpoint_writes"):
            # 表名为固定库契约，不接受用户/模型标识符。
            connection.execute(
                f"GRANT SELECT,INSERT,UPDATE,DELETE ON lg.{table} TO cs_agent_app_b0"
            )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", choices=["local", "debian"], required=True)
    parser.add_argument("--target", choices=["test", "app"], default="test")
    args = parser.parse_args()
    _, values = apply_runtime_profile(args.profile)
    setup(
        values[
            "CS_TEST_MIGRATION_URL" if args.target == "test" else "CS_MIGRATION_URL"
        ],
        values["CS_ISOLATION_FILE"],
    )
    print("Project PostgreSQL checkpoints prepared; credentials omitted")
