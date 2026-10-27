"""本机与Debian数据库配置显式分离，不读取SSH口令或模型凭据。"""

import json
import os

from dotenv import dotenv_values

from cs_agent.config import PROJECT_ROOT

DATABASE_KEYS = {
    "CS_DATABASE_URL",
    "CS_MIGRATION_URL",
    "CS_TEST_DATABASE_URL",
    "CS_TEST_MIGRATION_URL",
    "CS_UPGRADE_MIGRATION_URL",
    "CS_ISOLATION_FILE",
    "CS_EXPECTED_DATABASE",
    "CS_QUERY_DATABASE_URL",
    "CS_TEST_QUERY_DATABASE_URL",
    "CS_QUERY_PLAN_COST_LIMIT",
}


def profile_values(profile=None):
    chosen = profile or os.environ.get("CS_DATABASE_PROFILE")
    if not chosen:
        active = PROJECT_ROOT / ".runtime/active-database.json"
        chosen = (
            json.loads(active.read_text(encoding="utf-8"))["profile"]
            if active.exists()
            else "local"
        )
    if chosen not in {"local", "debian"}:
        raise RuntimeError("unknown database profile")
    path = PROJECT_ROOT / ".runtime" / ("local.env" if chosen == "local" else "debian.env")
    if not path.is_file():
        raise RuntimeError("database profile not configured")
    values = {
        key: value
        for key, value in dotenv_values(path).items()
        if key in DATABASE_KEYS and value is not None
    }
    values.setdefault(
        "CS_ISOLATION_FILE",
        str(
            PROJECT_ROOT
            / ".runtime"
            / ("isolation.json" if chosen == "local" else "isolation-debian.json")
        ),
    )
    values.setdefault("CS_EXPECTED_DATABASE", "cs_agent_b0")
    return chosen, values


def apply_runtime_profile(profile=None):
    chosen, values = profile_values(profile)
    os.environ.update(values)
    os.environ["CS_DATABASE_PROFILE"] = chosen
    return chosen, values
