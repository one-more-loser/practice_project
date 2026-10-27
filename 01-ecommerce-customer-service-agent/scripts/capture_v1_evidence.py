"""只读采集当前版本/指纹、Schema和测试摘要，不导出个人资料或密钥。"""

import hashlib
import json
import os
import platform
import subprocess
import xml.etree.ElementTree as ET
from importlib import metadata

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from cs_agent.config import PROJECT_ROOT, Settings
from cs_agent.db.models import Base
from cs_agent.db.session import verify_target
from cs_agent.models.gateway import ModelGateway
from cs_agent.runtime_profiles import apply_runtime_profile
from sqlalchemy import create_engine, inspect, text


def main():
    root = PROJECT_ROOT
    apply_runtime_profile("debian")

    def xml(name):
        tree = ET.parse(root / ".runtime" / name).getroot()
        suite = tree.find("testsuite") if tree.tag == "testsuites" else tree
        return {
            key: suite.attrib.get(key)
            for key in ["tests", "failures", "errors", "skipped", "time"]
        }

    def playwright(name):
        return json.loads((root / ".runtime" / name).read_text(encoding="utf8"))[
            "stats"
        ]

    files = []
    for folder, patterns in [
        ("backend/src", ["*.py"]),
        ("backend/migrations", ["*.py"]),
        ("backend/tests", ["*.py"]),
        ("frontend/src", ["*.ts", "*.vue", "*.css"]),
        ("frontend/e2e", ["*.ts"]),
        ("contracts", ["*.json", "*.ts"]),
        ("scripts", ["*.py", "*.ps1"]),
    ]:
        for pattern in patterns:
            files.extend((root / folder).rglob(pattern))
    files += [
        root / name
        for name in [
            "backend/pyproject.toml",
            "backend/uv.lock",
            "frontend/package.json",
            "frontend/package-lock.json",
            "frontend/vite.config.ts",
            "frontend/playwright.config.ts",
        ]
    ]
    for folder in ["data/synthetic", "tests/evaluation", "tests/fixtures"]:
        files.extend((root / folder).rglob("*.json"))
    versions = {}
    for name in [
        "fastapi",
        "starlette",
        "pydantic",
        "sqlalchemy",
        "alembic",
        "psycopg",
        "pgvector",
        "langgraph",
        "langgraph-checkpoint",
        "langgraph-checkpoint-postgres",
        "langchain",
        "langchain-core",
        "sqlglot",
        "jieba",
        "pytest",
        "httpx",
        "uvicorn",
        "websockets",
        "ruff",
        "tzdata",
    ]:
        try:
            versions[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            versions[name] = None
    frontend = {}
    for name in [
        "vue",
        "vue-router",
        "pinia",
        "element-plus",
        "vite",
        "typescript",
        "vue-tsc",
        "vitest",
        "@playwright/test",
        "prettier",
    ]:
        frontend[name] = json.loads(
            (root / "frontend/node_modules" / name / "package.json").read_text()
        )["version"]
    node = r"C:\Users\27997\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe"
    report = {
        "stage": "V1-business-partial-acceptance",
        "python": platform.python_version(),
        "node": subprocess.check_output([node, "--version"], text=True).strip(),
        "python_packages": versions,
        "frontend_packages": frontend,
        "backend_full": xml("v1-backend.xml"),
        "case_supplement": xml("v1-case-final.xml"),
        "human_supplement": xml("v1-human-final.xml"),
        "counting_note": "supplement repeats five full-suite cases and adds one; do not add 181+6",
        "chrome": playwright("v1-business-playwright.json"),
        "edge": playwright("v1-edge-playwright.json"),
        "browser_versions": {
            "chrome": "155.0.8059.85",
            "edge": "155.0.4283.62",
            "previous_major": "not_verified",
        },
        "real_model_services": sorted(ModelGateway.from_settings(Settings()).configs),
        "real_model_calls": 0,
        "file_sha256": {
            str(path.relative_to(root)).replace("\\", "/"): hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
            for path in sorted(set(files))
        },
    }
    engine = create_engine(os.environ["CS_MIGRATION_URL"], hide_parameters=True)
    verify_target(engine, Settings(expected_database="cs_agent_b0"), migration=True)
    with engine.connect() as conn:
        context = MigrationContext.configure(
            conn,
            opts={
                "compare_type": True,
                "include_object": lambda obj, name, type_, reflected, compare_to: (
                    name != "cs_project_guard"
                ),
            },
        )
        differences = compare_metadata(context, Base.metadata)
        report["database"] = {
            "server_version": conn.scalar(text("SHOW server_version")),
            "schema": conn.scalar(text("SELECT version_num FROM alembic_version")),
            "pgvector": conn.scalar(
                text("SELECT extversion FROM pg_extension WHERE extname='vector'")
            ),
            "mapped_public_tables": len(Base.metadata.tables),
            "query_views": len(inspect(conn).get_view_names(schema="csq")),
            "native_checkpoint_tables": len(inspect(conn).get_table_names(schema="lg")),
            "metadata_difference_count": len(differences),
            "wallet_reconciliation_mismatches": conn.scalar(
                text(
                    "SELECT count(*) FROM wallet_account a LEFT JOIN (SELECT account_id,sum(CASE WHEN kind='payment' THEN -amount_minor ELSE amount_minor END) total,max(sequence) last_sequence,count(*) entries FROM wallet_entry GROUP BY account_id) e USING(account_id) WHERE a.balance_minor<>coalesce(e.total,0) OR a.ledger_sequence<>e.last_sequence OR e.entries<>e.last_sequence+1"
                )
            ),
        }
        if differences:
            raise RuntimeError(
                "metadata mismatch; inspect privately before claiming readiness"
            )
    engine.dispose()
    target = root / "docs/04-implementation/compatibility-v1-business.json"
    target.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf8"
    )
    print(
        json.dumps(
            {
                "source_files": len(report["file_sha256"]),
                "backend_full": report["backend_full"],
                "case_supplement": report["case_supplement"],
                "database": report["database"],
                "configured_real_model_services": report["real_model_services"],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
