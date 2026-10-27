from sqlalchemy import text

from cs_agent.config import Settings
from cs_agent.db.models import Base
from cs_agent.db.session import verify_target


def reset_test_data(engine):
    if engine.url.database != "cs_agent_b0_test":
        raise RuntimeError("only the fixed project test database can be reset")
    verify_target(engine, Settings(expected_database="cs_agent_b0_test"), migration=True)
    with engine.begin() as connection:
        for table in reversed(Base.metadata.sorted_tables):
            connection.execute(text(f'DELETE FROM "{table.name}"'))
