"""维护模式手动运行；部署阶段再配置调度，不在请求链使用迁移凭据。"""

import argparse
import json
import os
from datetime import UTC, datetime

from cs_agent.retention.service import expire
from cs_agent.runtime_profiles import apply_runtime_profile
from sqlalchemy import create_engine

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", choices=["local", "debian"], required=True)
    parser.add_argument("--target", choices=["test", "app"], required=True)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if not args.execute:
        print(
            "Dry run: conversation content 30 days, audit 90 days, summaries 180 days; no data changed"
        )
    else:
        apply_runtime_profile(args.profile)
        engine = create_engine(
            os.environ[
                "CS_TEST_MIGRATION_URL" if args.target == "test" else "CS_MIGRATION_URL"
            ],
            hide_parameters=True,
        )
        try:
            print(json.dumps(expire(engine, datetime.now(UTC))))
        finally:
            engine.dispose()
