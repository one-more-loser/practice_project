"""Capture scoped handoff evidence; keep the original 179/16 baseline immutable."""
import hashlib
import json
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

root = Path(__file__).resolve().parents[1]
assert root.name == "02-ecommerce-operations-agent"


def read(path):
    return (root / path).read_text(encoding="utf-8-sig")


def sha(path):
    return hashlib.sha256((root / path).read_bytes()).hexdigest()


baseline_path = "docs/06-acceptance/验证证据.json"
baseline = json.loads(read(baseline_path))
current = {path: sha(path) for path in baseline["code_sha256"]}
current["frontend/src/components/monitoring-empty.test.ts"] = sha(
    "frontend/src/components/monitoring-empty.test.ts")
changed = sorted(path for path, original in baseline["code_sha256"].items()
                 if current[path] != original)
assert changed == sorted([
    "backend/src/ops_agent/bridge.py", "backend/tests/test_bridge.py",
    "frontend/src/components/ActivityPanel.vue", "frontend/src/components/cancel-approval.test.ts"])
source_digest = hashlib.sha256(json.dumps(current, sort_keys=True,
    separators=(",", ":")).encode()).hexdigest()


def tests(path):
    tree = ET.parse(root / path).getroot()
    suites = [tree] if tree.tag == "testsuite" else list(tree.iter("testsuite"))
    return {key: sum(int(s.get(key, 0)) for s in suites)
            for key in ("tests", "failures", "errors", "skipped")}


red_path = ".runtime/handoff-malformed-red.xml"
green_path = ".runtime/handoff-bridge-api.xml"
red, green = tests(red_path), tests(green_path)
assert red["tests"] == red["failures"] == 5
assert green == {"tests": 65, "failures": 0, "errors": 0, "skipped": 0}
front = read(".runtime/handoff-frontend-test.log")
build = read(".runtime/handoff-frontend-build.log")
assert re.search(r"Tests\s+17 passed", front) and "built in" in build
documents = json.loads(read(".runtime/requirements-document-check.json"))
assert documents["issues"] == [] and len(documents["html_pages"]) == 3

report = {
    "delivery_version": "OPS02-HANDOFF-20261023",
    "contract_version": "XOPS-ACTIVITY-v0.1; not yet signed by 01",
    "captured_at": datetime.now(timezone.utc).isoformat(),
    "scope": "02 local handoff closeout; full V1, real 01 and PostgreSQL not accepted",
    "collector_python": sys.version.split()[0],
    "baseline": {"path": baseline_path, "sha256": sha(baseline_path),
        "captured_at": baseline["captured_at"], "fingerprints_checked_at_takeover": 39,
        "mismatches_at_takeover": [], "backend_tests": 179, "frontend_tests": 16,
        "policy": "reused unchanged relevant evidence; original report not regenerated"},
    "changed_baseline_sources": changed,
    "added_sources": ["frontend/src/components/monitoring-empty.test.ts"],
    "source_manifest_sha256": source_digest,
    "code_sha256": dict(sorted(current.items())),
    "checks": {
        "red": {"path": red_path, "result": red,
            "reason": "non-object reconciliation data raises AttributeError before guard"},
        "backend_focused": {"path": green_path, "exit_code": 0, "result": green,
            "command": "backend: ../.venv/Scripts/python.exe -m pytest -q tests/test_bridge.py tests/test_api.py --tb=short --basetemp ../.runtime/handoff-bridge-api-permitted --junitxml ../.runtime/handoff-bridge-api.xml",
            "backend_full_suite_rerun": False},
        "ruff": {"exit_code": 0, "output": read(".runtime/handoff-ruff.log"),
            "command": ".venv/Scripts/python.exe -m ruff check backend/src backend/tests"},
        "frontend": {"tests_passed": 17, "test_exit_code": 0, "build_exit_code": 0,
            "commands": ["npm test", "npm run build"],
            "logs": [".runtime/handoff-frontend-test.log", ".runtime/handoff-frontend-build.log"],
            "first_build_failure": "new test validity fixture omitted latest_snapshot_revision (TS2741); field added, tests and build rerun successfully"},
        "documents": documents,
        "browser": {"surface": "Codex in-app browser, localhost 5174, existing demo DB",
            "observed": ["existing confirmed demo plan and evaluation retained",
                "cancel action disabled without reason, enabled with reason; no cancellation submitted",
                "activity control actions disabled in demo, no fake activity receipt",
                "no actual data, freshness shown as not provided after UI fix"],
            "component_only": ["cancel_pending -> reconcile active keeps phase and editing frozen"],
            "screenshot": "活动与复盘-收尾.jpg",
            "mobile_full_accessibility": "not verified"},
        "processes": "Used existing Start-Local/Stop-Local scripts and dynamic record; original stopped PIDs not reused; demo DB retained",
    },
    "environment_failures": [
        "restricted localhost socket/CIM denied during initial Start-Local; approved rerun of existing scripts ready",
        "restricted combined pytest reached 46 bridge cases then TestClient made no progress; session interrupted, approved isolated rerun 65 passed",
        "02 business venv has no markdown renderer; existing system Python markdown used only for document generation, business tests stay in 02 Python3.12",
        "docker info: dockerDesktopLinuxEngine named pipe missing; no dedicated ops02_ PostgreSQL configured/tested"],
    "remaining_required": [
        "01 signed contract/diff and exact source/runtime/database versions with samples",
        "real identity, preflight, activity controls/navigation, trade/ledger/slot barrier/outbox and actual feed",
        "02 real HTTP consumption and page adaptation after 01 response; retained P0 gaps must be assessed against approved launch scope, not auto-deferred",
        "02 dedicated PostgreSQL concurrent grant/cancel/idempotence/recovery",
        "authorized business input/cost/capacity owners, data freshness and profit display acceptance",
        "same-version joint acceptance, 01 transaction concurrency/performance and 02 polling/latency evidence"],
    "not_performed": ["real 01 HTTP", "PostgreSQL concurrency", "real purchases/business effects",
        "paid model", "production deployment", "sibling/root writes", "messages to other chats"],
}
paths = [red_path, green_path, ".runtime/handoff-frontend-test.log",
         ".runtime/handoff-frontend-build.log", ".runtime/handoff-ruff.log",
         ".runtime/requirements-document-check.json"]
report["evidence_sha256"] = {path: sha(path) for path in paths}
handoff_files = ["HANDOFF.MD", "README.md", "backend/pyproject.toml", "frontend/package.json",
    "frontend/tsconfig.json", "frontend/index.html", "scripts/Start-Local.ps1",
    "scripts/Stop-Local.ps1", "scripts/Collect-Handoff-Evidence.py",
    ".runtime/update_review_documents.py", "docs/03-plan/02实施计划.md", "docs/03-plan/API约定.md",
    "docs/04-integration/02实际执行契约.md", "docs/04-integration/发给01会话的联调提示词.md",
    "docs/04-integration/01回传模板.md", "docs/06-acceptance/02模块验收记录.md",
    "docs/06-acceptance/02收尾与联调交付.md", "docs/06-acceptance/02收尾与联调交付.html",
    "docs/01-requirements/需求评审.html", "docs/02-design/设计评审.html",
    "docs/06-acceptance/活动与复盘-收尾.jpg", ".runtime/handoff-first-build-failure.txt"]
report["handoff_files_sha256"] = {path: sha(path) for path in handoff_files}
target = root / "docs/06-acceptance/收尾复核证据.json"
target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps({"backend_focused": green, "frontend": 17,
    "source_files": len(current), "source_manifest_sha256": source_digest}, ensure_ascii=False))
