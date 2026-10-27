"""Collect 03 validator decisions for the comparison matrix."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runner"))
from validator import validate  # noqa: E402

CASES = Path(__file__).with_name("cases_compare.json")
out = {}
for case in json.loads(CASES.read_text()):
    try:
        v = validate(case["sql"], case.get("parameters", []))
        out[case["id"]] = {
            "decision": "accept" if v.ok else "reject",
            "code": v.code,
            "message": v.message,
            "rewritten": v.staged_sql,
            "columns": None,
        }
    except Exception as exc:  # noqa: BLE001
        out[case["id"]] = {"decision": "reject", "code": "exception",
                           "message": f"{type(exc).__name__}: {exc}", "rewritten": "", "columns": None}
print(json.dumps(out, ensure_ascii=False, indent=2))
