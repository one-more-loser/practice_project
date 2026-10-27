"""V1当前业务远程回归入口，显式profile；完整测试仅重置已守卫的测试/升级库。"""

import argparse
import os
import sys

import pytest
from cs_agent.config import PROJECT_ROOT
from cs_agent.runtime_profiles import apply_runtime_profile

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", choices=["local", "debian"], required=True)
    args = parser.parse_args()
    apply_runtime_profile(args.profile)
    os.chdir(PROJECT_ROOT)
    sys.path.insert(0, str(PROJECT_ROOT))
    os.environ["TEMP"] = os.environ["TMP"] = str(PROJECT_ROOT / ".runtime/tmp")
    raise SystemExit(
        pytest.main(
            [
                "backend/tests",
                "-q",
                "--tb=short",
                "--basetemp=.runtime/pytest-v1-full",
                "--junitxml=.runtime/v1-backend.xml",
            ]
        )
    )
