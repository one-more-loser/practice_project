"""不依赖Git的联合源码清单。报告/私密runtime不进入源码摘要。"""
import hashlib
import json
import os
from pathlib import Path


def source_manifest(root):
    root = Path(root)
    files = {}
    excluded = {".venv", "node_modules", "__pycache__", ".pytest_cache", ".ruff_cache", "dist", ".runtime", ".git", ".agents", "playwright-report", "test-results"}
    for directory in ("backend", "frontend", "scripts", "contracts"):
        for current, children, names in os.walk(root / directory):
            children[:] = [name for name in children if name not in excluded]
            for name in names:
                path = Path(current) / name
                if path.suffix in {".pyc", ".log", ".db", ".png", ".jpg"} or name.startswith(".env"):
                    continue
                files[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    canonical = json.dumps(files, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {"files": files, "digest": hashlib.sha256(canonical.encode("utf-8")).hexdigest()}
