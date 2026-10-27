"""Collect 01 validator decisions with sqlglot 30.24.0.

Run with PYTHONPATH pointing at 01/backend/src so the comparison uses 01's
actual validator, not a reimplementation.
"""
import json
import sys
from pathlib import Path

from cs_agent.core.errors import AppError
from cs_agent.query.validator import validate_query

CASES = Path(__file__).with_name("cases_compare.json")
out = {}
for case in json.loads(CASES.read_text()):
    try:
        p = validate_query(case["sql"])
        out[case["id"]] = {
            "decision": "accept",
            "code": "ok",
            "message": "",
            "rewritten": p.sql,
            "columns": p.columns,
            "parameters": sorted(p.parameters),
        }
    except AppError as exc:
        out[case["id"]] = {"decision": "reject", "code": exc.code,
                           "message": str(exc), "rewritten": "", "columns": None}
    except Exception as exc:  # noqa: BLE001
        out[case["id"]] = {"decision": "reject", "code": "exception",
                           "message": f"{type(exc).__name__}: {exc}", "rewritten": "", "columns": None}
print(json.dumps(out, ensure_ascii=False, indent=2))
