"""项目内开发迁移：按local/debian配置核对独立目标，不输出URL。"""

import argparse
import os
from pathlib import Path

from alembic import command
from alembic.config import Config

from cs_agent.runtime_profiles import apply_runtime_profile

ROOT = Path(__file__).resolve().parents[1]

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("target", choices=["app", "test"])
    parser.add_argument("--profile", choices=["local", "debian"])
    args = parser.parse_args()
    apply_runtime_profile(args.profile)
    key = "CS_MIGRATION_URL" if args.target == "app" else "CS_TEST_MIGRATION_URL"
    url = os.environ[key]
    config = Config(str(ROOT / "backend/alembic.ini"))
    config.attributes["database_url"] = url
    command.upgrade(config, "head")
    from cs_agent.db.seed import seed

    fingerprint = seed(url)
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from cs_agent.db.schema import SCHEMA_REVISION
    from cs_agent.db.seed_business import seed_accounts, seed_catalog

    engine = create_engine(url, hide_parameters=True)
    with Session(engine) as session, session.begin():
        seed_accounts(session)
        seed_catalog(session)
    engine.dispose()
    print(
        f"{args.target}: revision={SCHEMA_REVISION}; seed_fingerprint={fingerprint}; target verified"
    )
