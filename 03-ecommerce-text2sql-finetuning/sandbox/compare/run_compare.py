"""Run both validators and write the comparison table/JSON.

03: sandbox/.venv (sqlglot 27.20.0) + sandbox/runner/validator.py
01: sandbox/.venv-compare (sqlglot 30.24.0) + 01 backend validator
"""
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
PY03 = ROOT / "sandbox" / ".venv" / "bin" / "python"
PY01 = ROOT / "sandbox" / ".venv-compare" / "bin" / "python"
P01 = Path("/mnt/d/codex_project/项目实战/01-ecommerce-customer-service-agent/backend/src")


def run(py: Path, script: Path, env_extra: dict) -> dict:
    env = os.environ.copy()
    env.update(env_extra)
    out = subprocess.check_output([str(py), str(script)], env=env, text=True)
    return json.loads(out)


def main() -> int:
    c03 = run(PY03, HERE / "collect_03.py", {"PYTHONPATH": str(ROOT / "sandbox" / "runner")})
    c01 = run(PY01, HERE / "collect_01.py", {"PYTHONPATH": str(P01)})
    cases = json.loads((HERE / "cases_compare.json").read_text())
    rows = []
    for case in cases:
        a, b = c03[case["id"]], c01[case["id"]]
        agree = a["decision"] == b["decision"]
        rows.append({"id": case["id"], "expect": case["expect"], "sql": case["sql"],
                     "03": a, "01": b, "decision_agree": agree})
    summary = {
        "cases": len(rows),
        "decision_agree": sum(r["decision_agree"] for r in rows),
        "decision_disagree": [r["id"] for r in rows if not r["decision_agree"]],
        "expected_03": sum(r["03"]["decision"] == r["expect"] for r in rows),
        "expected_01": sum(r["01"]["decision"] == r["expect"] for r in rows),
        "sqlglot_03": "27.20.0",
        "sqlglot_01": "30.24.0",
        "boundary": "03 validator is local/target-level; 01 validator is the consumer's actual source under sqlglot>=30.24.0",
    }
    (HERE / "results.json").write_text(json.dumps({"summary": summary, "rows": rows},
                                                   ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    for r in rows:
        print(f'{r["id"]:4} 03={r["03"]["decision"]:6}({r["03"]["code"]:22}) 01={r["01"]["decision"]:6}({r["01"]["code"]:20}) agree={r["decision_agree"]}')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
