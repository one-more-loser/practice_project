"""演示批次重置，默认只读；执行前停本项目API/Worker并制作新备份。"""

import argparse
import json
import os
from datetime import UTC, datetime

from cs_agent.retention.reset import reset_synthetic_batch
from cs_agent.runtime_profiles import apply_runtime_profile
from sqlalchemy import create_engine

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", choices=["local", "debian"], required=True)
    parser.add_argument("--target", choices=["test", "app"], required=True)
    parser.add_argument("--batch", required=True)
    parser.add_argument("--generation", type=int, required=True)
    parser.add_argument("--acknowledge-whole-batch-reset")
    args = parser.parse_args()
    required = f"RESET:{args.batch}:{args.generation}"
    if args.acknowledge_whole_batch_reset is None:
        print(
            "Dry run: no data changed. Requires stopped services, verified backup and exact batch/generation acknowledgement."
        )
    elif args.acknowledge_whole_batch_reset != required:
        raise RuntimeError("whole-batch acknowledgement does not match target")
    else:
        apply_runtime_profile(args.profile)
        engine = create_engine(
            os.environ[
                "CS_TEST_MIGRATION_URL" if args.target == "test" else "CS_MIGRATION_URL"
            ],
            hide_parameters=True,
        )
        try:
            print(
                json.dumps(
                    reset_synthetic_batch(
                        engine, args.batch, args.generation, datetime.now(UTC)
                    )
                )
            )
        finally:
            engine.dispose()
