import os
from datetime import UTC, datetime

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from cs_agent.config import PROJECT_ROOT, Settings
from cs_agent.core.context import Role, TrustedContext
from cs_agent.db.seed import seed
from cs_agent.db.seed_business import seed_accounts, seed_catalog
from cs_agent.db.session import Database
from tests.helpers import reset_test_data


@pytest.fixture
def business():
    url = os.environ["CS_TEST_MIGRATION_URL"]
    owner = create_engine(url, hide_parameters=True)
    config = Config(str(PROJECT_ROOT / "backend/alembic.ini"))
    config.attributes["database_url"] = url
    command.upgrade(config, "head")
    reset_test_data(owner)
    seed(url)
    with Session(owner) as session, session.begin():
        seed_accounts(session)
        seed_catalog(session)
    runtime = Database(
        Settings(
            database_url=os.environ["CS_TEST_DATABASE_URL"], expected_database="cs_agent_b0_test"
        )
    )
    ctx = TrustedContext(
        "customer_a",
        "store_sim",
        Role.CUSTOMER,
        "test_session",
        1,
        "sim_b0_20261022",
        1,
        "req_test",
    )
    yield runtime, owner, ctx, datetime(2026, 10, 22, 3, tzinfo=UTC)
    runtime.close()
    owner.dispose()
