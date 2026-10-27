"""Prompt/answer contract shared by data conversion, SFT and evaluation."""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_TEXT = (ROOT / "training" / "schema_customer_self.txt").read_text(encoding="utf-8")

SYSTEM = (
    "你是只读 SQL 候选生成器。只输出一个 JSON 对象，不要 Markdown、不要解释。\n"
    "允许的响应 kind: query / clarify / unsupported。\n"
    "- query: {\"kind\":\"query\",\"sql\":\"...\",\"parameter_names\":[\"start_at\",\"end_at\"]}\n"
    "  SQL 只读 SELECT；使用 :name 命名占位符；金额为整数分；窗口为半开 [start_at,end_at)。\n"
    "- clarify: {\"kind\":\"clarify\",\"missing\":[...],\"reason_code\":\"...\"}；缺时间/口径/槽位时使用。\n"
    "- unsupported: {\"kind\":\"unsupported\",\"reason_code\":\"...\"}；写操作、跨顾客、无授权资源时使用。\n"
    "不能生成身份/权限条件，不能执行 SQL，不能编造未知金额或指标。"
)

PROMPT_FORMAT = """[SYSTEM]
{system}
[SCHEMA]
{schema}
[QUESTION]
{question}
[ANSWER]
"""


def prompt(row: dict) -> str:
    if row.get('profile', 'customer_self') != 'customer_self':
        raise ValueError('this prompt supports customer_self only; explicit profile adaptation is required')
    return PROMPT_FORMAT.format(system=SYSTEM, schema=SCHEMA_TEXT.strip(), question=row["question"].strip())


def answer(row: dict) -> str:
    kind = row["expected_kind"]
    if kind == "query":
        obj = {"kind": "query", "sql": row["sql"], "parameter_names": list(row.get("parameter_names") or [])}
    elif kind == "clarify":
        obj = {"kind": "clarify", "missing": list(row.get("clarify_slots") or []), "reason_code": row.get("reason_code", "")}
    elif kind == "unsupported":
        obj = {"kind": "unsupported", "reason_code": row.get("reason_code", "")}
    else:
        raise ValueError(f"unknown kind {kind}")
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def completion_text(row: dict) -> str:
    return answer(row) + "<|endoftext|>"


def extract_json(text: str) -> dict | None:
    text = text.strip()
    text = re.sub(r"^```(?:json)?", "", text).strip()
    text = re.sub(r"```$", "", text).strip()
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            obj = json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            return None
    return obj if isinstance(obj, dict) else None


def load_jsonl(path: str | Path) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]
