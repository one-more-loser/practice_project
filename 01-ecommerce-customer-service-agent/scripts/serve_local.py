"""仅本项目回环地址的浏览器验证入口，不是部署脚本。"""

import os
from pathlib import Path

import uvicorn
from cs_agent.runtime_profiles import apply_runtime_profile

root = Path(__file__).resolve().parents[1]
apply_runtime_profile()
if os.environ.get("CS_VERIFICATION_TARGET") == "test":
    # 浏览器回归只切换到固定项目测试库，仍由Database隔离守卫二次复核。
    os.environ["CS_DATABASE_URL"] = os.environ["CS_TEST_DATABASE_URL"]
    os.environ["CS_QUERY_DATABASE_URL"] = os.environ["CS_TEST_QUERY_DATABASE_URL"]
    os.environ["CS_EXPECTED_DATABASE"] = "cs_agent_b0_test"
elif os.environ.get("CS_VERIFICATION_TARGET"):
    raise RuntimeError("unknown verification target")
os.environ["CS_COOKIE_SECURE"] = "false"
os.environ["CS_BROWSER_ORIGIN"] = "http://127.0.0.1:5173"

if __name__ == "__main__":
    uvicorn.run(
        "cs_agent.main:app",
        host="127.0.0.1",
        port=8000,
        access_log=False,
        log_level="warning",
    )
