"""汇总真实模型证据；公开版本、状态、摘要，排除密钥/会话/个人资料。"""

import hashlib
import json
import subprocess
import xml.etree.ElementTree as ET
from datetime import UTC, datetime

import httpx
from cs_agent.config import PROJECT_ROOT, Settings
from cs_agent.db.session import verify_target
from cs_agent.runtime_profiles import apply_runtime_profile
from sqlalchemy import create_engine, text


def main():
    root = PROJECT_ROOT
    target = root / "docs/06-acceptance/models"
    target.mkdir(exist_ok=True)

    def read(name):
        return json.loads((root / ".runtime" / name).read_text(encoding="utf8"))

    def xml(name):
        tree = ET.parse(root / ".runtime" / name).getroot()
        suite = tree.find("testsuite") if tree.tag == "testsuites" else tree
        return {
            k: suite.get(k) for k in ["tests", "failures", "errors", "skipped", "time"]
        }

    config = read("model-services.json")
    assert {config[k]["model"] for k in ["chat", "sql"]} == {"deepseek-flash"}
    models = read("qwen-models.json")
    health = httpx.get(
        "http://127.0.0.1:8010/health", timeout=3, trust_env=False
    ).json()
    assert health["real_model"] is True
    browser = read("real-model-browser.json")
    model_python = str(root / ".runtime/qwen-venv/Scripts/python.exe")
    packages = json.loads(
        subprocess.check_output(
            [model_python, "-m", "pip", "list", "--format=json"], text=True
        )
    )
    report = {
        "captured_at": datetime.now(UTC).isoformat(),
        "status": "real_model_integration_partial_acceptance",
        "configuration": {
            cap: {key: row[key] for key in ["model", "protocol", "service_revision"]}
            for cap, row in config.items()
        },
        "shared_model_snapshots": models,
        "model_runtime_packages": {row["name"]: row["version"] for row in packages},
        "health": health,
        "backend_full": xml("v1-backend.xml"),
        "full_suite_scope": "189 before final guest/public routing and cancellation prompt fixes; affected paths rechecked below, no totals added",
        "runtime_followup": xml("real-model-followup.xml"),
        "cancellation_followup": xml("real-cancel-followup.xml"),
        "browser_stats": browser["stats"],
        "real_generation_usage_note": "smoke reports contain per-request usage when supplied; not an aggregate billing statement for all Agent calls",
    }
    mapping = {
        "deepseek-flash-smoke.json": "deepseek-compatibility.json",
        "qwen-local-evaluation.json": "qwen-local.json",
        "real-pipeline-test.json": "pipeline.json",
        "real-cancellation.json": "cancellation.json",
    }
    for source, destination in mapping.items():
        value = read(source)
        (target / destination).write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf8"
        )
    (target / "browser.json").write_text(
        json.dumps(
            {
                "channel": "Chrome",
                "stats": browser["stats"],
                "purpose": "real public policy routing, source citation, login guidance; opt-in fixed test DB",
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf8",
    )
    _, values = apply_runtime_profile("debian")
    engine = create_engine(
        values["CS_MIGRATION_URL"],
        hide_parameters=True,
        connect_args={"connect_timeout": 4, "options": "-c statement_timeout=5000"},
    )
    verify_target(engine, Settings(expected_database="cs_agent_b0"), migration=True)
    with engine.connect() as c:
        c.execute(text("SET TRANSACTION READ ONLY"))
        report["application_database"] = {
            "schema": c.scalar(text("SELECT version_num FROM alembic_version")),
            "knowledge_chunks": c.scalar(text("SELECT count(*) FROM knowledge_chunk")),
            "nonnull_vectors": c.scalar(
                text("SELECT count(embedding) FROM knowledge_chunk")
            ),
            "wallet_reconciliation_mismatches": c.scalar(
                text(
                    "SELECT count(*) FROM wallet_account a LEFT JOIN (SELECT account_id,sum(CASE WHEN kind='payment' THEN -amount_minor ELSE amount_minor END) total,max(sequence) last_sequence,count(*) entries FROM wallet_entry GROUP BY account_id) e USING(account_id) WHERE a.balance_minor<>coalesce(e.total,0) OR a.ledger_sequence<>e.last_sequence OR e.entries<>e.last_sequence+1"
                )
            ),
            "application_plan_cost_limit_configured": bool(
                values.get("CS_QUERY_PLAN_COST_LIMIT")
            ),
        }
    engine.dispose()
    files = []
    for directory, patterns in [
        ("backend/src", ["*.py"]),
        ("backend/tests", ["*.py"]),
        ("model_service", ["*.py", "*.txt", "*.lock"]),
        ("scripts", ["*.py"]),
        ("frontend/real-model-e2e", ["*.ts"]),
        ("contracts", ["*.json"]),
    ]:
        for pattern in patterns:
            files.extend((root / directory).rglob(pattern))
    files.extend(
        [
            root / "frontend/playwright.real-model.config.ts",
            root / "tests/evaluation/rag/dataset.json",
        ]
    )
    report["source_sha256"] = {
        str(p.relative_to(root)).replace("\\", "/"): hashlib.sha256(
            p.read_bytes()
        ).hexdigest()
        for p in sorted(set(files))
    }
    (target / "compatibility.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf8"
    )
    key = Settings().chat_api_key
    if key:
        secret = key.get_secret_value()
        if len(secret) > 8 and any(
            secret in p.read_text(encoding="utf8") for p in target.glob("*.json")
        ):
            raise RuntimeError("sensitive value in public evidence; inspect privately")
    print(
        json.dumps(
            {
                "models": {k: v["model"] for k, v in config.items()},
                "browser": report["browser_stats"],
                "application_database": report["application_database"],
                "health": health,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
