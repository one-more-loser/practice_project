"""重置固定隔离测试库供浏览器/真实HTTP使用；绝不清理应用库。"""

import argparse
import sys
from datetime import UTC, datetime

from alembic import command
from alembic.config import Config
from cs_agent.config import PROJECT_ROOT, Settings
from cs_agent.db.seed import seed
from cs_agent.db.seed_business import seed_accounts, seed_catalog
from cs_agent.db.session import Database
from cs_agent.knowledge.service import seed_faq_drafts
from cs_agent.runtime_profiles import apply_runtime_profile
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session


def main(profile):
    _, values = apply_runtime_profile(profile)
    url = values["CS_TEST_MIGRATION_URL"]
    if make_url(url).database != "cs_agent_b0_test":
        raise RuntimeError("only the fixed project test database can be prepared")
    sys.path.insert(0, str(PROJECT_ROOT / "backend"))
    from tests.helpers import reset_test_data

    from scripts.setup_checkpoints import setup

    config = Config(str(PROJECT_ROOT / "backend/alembic.ini"))
    config.attributes["database_url"] = url
    command.upgrade(config, "head")
    owner = create_engine(url, hide_parameters=True)
    try:
        reset_test_data(owner)
        seed(url)
        with Session(owner) as session, session.begin():
            seed_accounts(session)
            seed_catalog(session)
    finally:
        owner.dispose()
    setup(url, values["CS_ISOLATION_FILE"])
    db = Database(
        Settings(
            database_url=values["CS_TEST_DATABASE_URL"],
            expected_database="cs_agent_b0_test",
        )
    )
    try:
        result = seed_faq_drafts(
            db, PROJECT_ROOT / "data/synthetic/faq-sim-v0.2.json", datetime.now(UTC)
        )
    finally:
        db.close()
    print(
        f"Fixed test database prepared; {result['inserted']} FAQ drafts; no fake vectors or model calls"
    )


if __name__ == "__main__":
    sys.path.insert(0, str(PROJECT_ROOT))
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", choices=["local", "debian"], required=True)
    main(parser.parse_args().profile)
