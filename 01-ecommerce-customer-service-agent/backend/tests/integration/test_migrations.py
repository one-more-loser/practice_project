import os
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text


def config_for(url):
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    config.attributes["database_url"] = url
    return config


def test_empty_database_upgrade_preserves_first_revision_data():
    url = os.environ["CS_UPGRADE_MIGRATION_URL"]
    engine = create_engine(url, hide_parameters=True)
    with engine.begin() as connection:
        # 仅测试库；守卫由verify_target复核后方可执行。
        from cs_agent.config import Settings
        from cs_agent.db.session import verify_target

        verify_target(engine, Settings(expected_database="cs_agent_b0_upgrade"), migration=True)
        from cs_agent.db.models import Base

        for table in [t.name for t in reversed(Base.metadata.sorted_tables)] + ["alembic_version"]:
            connection.execute(text(f'DROP TABLE IF EXISTS "{table}" CASCADE'))
    command.upgrade(config_for(url), "0001_source")
    assert "source_batch" in inspect(engine).get_table_names()
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO source_batch (batch_id,generation,source_kind,seed,fingerprint,manifest_revision,status) VALUES ('history',1,'synthetic',22,:fingerprint,'SIM-CS-B0-v1','active')"
            ),
            {"fingerprint": "a" * 64},
        )
    command.upgrade(config_for(url), "head")
    assert {"subject", "staff_role_grant", "login_session", "shipping_input", "audit_event"} <= set(
        inspect(engine).get_table_names()
    )
    with engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT seed FROM source_batch WHERE batch_id='history'")
            ).scalar_one()
            == 22
        )
    engine.dispose()
