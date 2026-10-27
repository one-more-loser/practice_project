"""记录实际版本和文件指纹；不把安装记录解释为业务验收。"""

import argparse
import hashlib
import importlib.metadata as metadata
import json
import platform
import subprocess
import sys
from pathlib import Path

from dotenv import dotenv_values
from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parents[1]


def main(node, stage="B0"):
    packages = [
        "fastapi",
        "starlette",
        "pydantic",
        "pydantic-settings",
        "uvicorn",
        "sqlalchemy",
        "alembic",
        "psycopg",
        "pytest",
        "httpx",
        "ruff",
    ]
    frontend = [
        "vue",
        "vue-router",
        "pinia",
        "element-plus",
        "vite",
        "@vitejs/plugin-vue",
        "typescript",
        "vue-tsc",
        "vitest",
        "@vue/test-utils",
        "jsdom",
    ]
    if stage == "B1":
        frontend.extend(["@playwright/test", "prettier"])
    files = [
        ROOT / "backend/pyproject.toml",
        ROOT / "backend/uv.lock",
        ROOT / "frontend/package.json",
        ROOT / "frontend/package-lock.json",
        ROOT / "data/synthetic/manifest.json",
        ROOT / "tests/fixtures/source_map.json",
    ]
    files.extend((ROOT / "backend/src").rglob("*.py"))
    files.extend((ROOT / "backend/migrations").rglob("*.py"))
    files.extend((ROOT / "backend/tests").rglob("*.py"))
    files.extend((ROOT / "frontend/src").rglob("*.ts"))
    files.extend((ROOT / "frontend/src").rglob("*.vue"))
    files.extend((ROOT / "frontend/tests").rglob("*.ts"))
    files.extend((ROOT / "contracts").glob("*.ts"))
    if stage == "B1":
        files.extend((ROOT / "frontend/e2e").glob("*.ts"))
        files.extend((ROOT / "frontend/src").rglob("*.css"))
        files.extend((ROOT / "scripts").glob("*.py"))
        files.extend(
            [
                ROOT / "data/synthetic/catalog-v1.json",
                ROOT / "tests/fixtures/business_source_map.json",
                ROOT / "frontend/playwright.config.ts",
                ROOT / ".better-web-ui.md",
                ROOT / ".agents/skills/apple-hig/SKILL.md",
            ]
        )
    report = {
        "stage": stage,
        "python": {
            "version": platform.python_version(),
            "venv_executable": sys.executable,
            "base_prefix": sys.base_prefix,
        },
        "node": {
            "version": subprocess.check_output([node, "--version"], text=True).strip(),
            "executable": node,
        },
        "backend": {name: metadata.version(name) for name in packages},
        "frontend": {
            name: json.loads((ROOT / "frontend/node_modules" / name / "package.json").read_text())[
                "version"
            ]
            for name in frontend
        },
        "sha256": {
            str(file.relative_to(ROOT)).replace("\\", "/"): hashlib.sha256(
                file.read_bytes()
            ).hexdigest()
            for file in sorted(set(files))
        },
        "unverified": [
            "LangGraph/LangChain/检查点",
            "pgvector",
            "SQLGlot/jieba",
            "四类模型能力",
            "Chrome/Edge各当前与前一主版本完整兼容验收",
            "多进程与负载",
            "02/03真实联调",
        ],
    }
    values = dotenv_values(ROOT / ".runtime/local.env")
    engine = create_engine(values["CS_DATABASE_URL"], hide_parameters=True)
    with engine.connect() as connection:
        report["database"] = {
            "version": connection.execute(text("SHOW server_version")).scalar_one(),
            "encoding": connection.execute(text("SHOW server_encoding")).scalar_one(),
            "schema_revision": connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one(),
        }
    engine.dispose()
    output = ROOT / (
        "docs/04-implementation/compatibility-b1.json"
        if stage == "B1"
        else "docs/04-implementation/compatibility.json"
    )
    output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Captured actual {stage} versions and source SHA256; no credential values exported")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--node", required=True)
    parser.add_argument("--stage", choices=["B0", "B1"], default="B0")
    args = parser.parse_args()
    main(args.node, args.stage)
