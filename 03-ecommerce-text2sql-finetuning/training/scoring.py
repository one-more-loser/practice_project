"""Shared, model-free scoring for Base/SFT predictions.

The raw `generated` string is the only source of truth. Cached strict/lenient
fields in a prediction file are ignored. Strict JSON means the entire decoded
text is one JSON object; Markdown/prose-wrapped JSON is reported only as
lenient diagnostics and never counts for protocol or business pass.
"""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from typing import Callable

from common import extract_json

KINDS = ("query", "clarify", "unsupported")
MAX_OUTPUT_BYTES = 32768


def index_predictions(preds, rows=None):
    """Reject ambiguous/corrupt files; missing predictions still score zero."""
    if not isinstance(preds, list):
        raise ValueError('Predictions must be a list')
    allowed = None
    if rows is not None:
        ids = [r.get('id') for r in rows]
        if any(not isinstance(i, str) or not i for i in ids) or len(set(ids)) != len(ids):
            raise ValueError('Evaluation row IDs must be nonempty and unique')
        allowed = set(ids)
    result = {}
    for pred in preds:
        if not isinstance(pred, dict) or not isinstance(pred.get('id'), str) or not pred['id']:
            raise ValueError('Prediction ID must be a nonempty string')
        key = pred['id']
        if key in result:
            raise ValueError(f'Duplicate prediction ID: {key}')
        if allowed is not None and key not in allowed:
            raise ValueError(f'Unknown prediction ID: {key}')
        if 'generated' in pred and not isinstance(pred['generated'], str):
            raise ValueError(f'generated must be a string: {key}')
        result[key] = pred
    return result


def prediction_error(pred):
    text = pred.get('generated', '')
    if not isinstance(text, str):
        return 'non_string_output'
    if len(text.encode('utf-8')) > MAX_OUTPUT_BYTES:
        return 'output_too_large'
    for flag in ('skipped', 'prompt_truncated', 'generation_truncated', 'timed_out', 'error'):
        if pred.get(flag):
            return flag
    return ''


def prediction_object(pred):
    return None if prediction_error(pred) else strict_json(pred.get('generated', ''))


def strict_json(text: str) -> dict | None:
    if not isinstance(text, str) or len(text.encode('utf-8')) > MAX_OUTPUT_BYTES:
        return None
    s = text.strip()
    if not s.startswith("{") or not s.endswith("}"):
        return None
    try:
        def unique_object(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError('duplicate JSON key')
                result[key] = value
            return result
        def invalid_constant(value):
            raise ValueError('nonfinite JSON constant: ' + value)
        obj = json.loads(s, object_pairs_hook=unique_object, parse_constant=invalid_constant)
    except Exception:  # noqa: BLE001
        return None
    return obj if isinstance(obj, dict) else None


def is_str_list(v) -> bool:
    return isinstance(v, list) and all(isinstance(x, str) for x in v)


def protocol_ok(row: dict, obj: dict | None) -> bool:
    if not isinstance(obj, dict) or obj.get("kind") != row["expected_kind"]:
        return False
    k = row["expected_kind"]
    if k == "query":
        return (not any(key in obj for key in ('missing', 'clarify', 'unsupported', 'error'))
                and isinstance(obj.get("sql"), str) and bool(obj["sql"].strip())
                and isinstance(obj.get("parameter_names"), list)
                and is_str_list(obj["parameter_names"])
                and sorted(obj["parameter_names"]) == sorted(row.get("parameter_names") or []))
    if k == "clarify":
        if any(key in obj for key in ('sql', 'parameter_names', 'unsupported', 'error')):
            return False
        if not is_str_list(obj.get("missing")):
            return False
        if sorted(obj["missing"]) != sorted(row.get("clarify_slots") or []):
            return False
        expected_reason = row.get("reason_code")
        return not expected_reason or obj.get("reason_code") == expected_reason
    if k == "unsupported":
        return (not any(key in obj for key in ('sql', 'parameter_names', 'missing', 'clarify', 'error'))
                and isinstance(obj.get("reason_code"), str) and obj["reason_code"] == row.get("reason_code"))
    return False


def score_rows(rows: list[dict], preds: list[dict],
               query_validator: Callable[[str, list[str]], bool] | None = None) -> dict:
    """Return fixed-denominator metrics plus per-row records.

    `query_validator(sql, parameter_names)` is optional; when omitted, the 03
    validator is imported lazily. A validator exception counts as invalid, not
    as a skipped row.
    """
    if query_validator is None:
        from validator import validate  # type: ignore
        query_validator = lambda sql, params: bool(validate(sql, params).ok)  # noqa: E731

    by_id = index_predictions(preds, rows)
    total = Counter(r["expected_kind"] for r in rows)
    c = Counter()
    per_template = defaultdict(Counter)
    records = []
    for row in rows:
        pred = by_id.get(row["id"], {})
        gen = pred.get("generated", "") or ""
        strict = strict_json(gen)
        lenient = extract_json(gen) if len(gen.encode('utf-8')) <= MAX_OUTPUT_BYTES else None
        obj = prediction_object(pred)  # failed/skipped outputs never score
        t = per_template[row["template_id"]]
        t["n"] += 1
        c["n"] += 1
        c["prompt_truncated"] += int(bool(pred.get("prompt_truncated")))
        c["skipped"] += int(bool(pred.get("skipped")))
        c["empty_output"] += int(not gen.strip())
        c["strict_json"] += int(strict is not None)
        c["lenient_json"] += int(lenient is not None)
        c["parse_fail"] += int(lenient is None)
        got_kind = obj.get("kind") if isinstance(obj, dict) else "invalid"
        c[f"got_kind_{got_kind}"] += 1
        proto = bool(protocol_ok(row, obj))
        c["protocol_ok"] += int(proto)
        rec = {
            "id": row["id"], "template_id": row["template_id"],
            "expected_kind": row["expected_kind"], "got_kind": got_kind,
            "strict_json": strict is not None, "lenient_json": lenient is not None,
            "protocol_ok": proto, "generated": gen,
            "prediction_error": prediction_error(pred),
        }
        if row["expected_kind"] == "query":
            c["query_total"] += 1
            c["query_protocol_ok"] += int(proto)
            if proto:
                sql = obj["sql"]
                declared = obj["parameter_names"]
                try:
                    valid = bool(query_validator(sql, declared))
                except Exception:  # noqa: BLE001
                    valid = False
                    c["query_validator_error"] += 1
                c["query_valid_03"] += int(valid)
                rec["query_valid"] = valid
                try:
                    from sqlglot import parse_one
                    a = parse_one(sql, read="postgres").sql(dialect="postgres", normalize=True)
                    b = parse_one(row["sql"], read="postgres").sql(dialect="postgres", normalize=True)
                    c["sql_exact"] += int(a == b)
                    rec["sql_exact"] = a == b
                except Exception:  # noqa: BLE001
                    c["sql_normalize_error"] += 1
                    rec["sql_exact"] = False
        elif row["expected_kind"] == "clarify":
            c["clarify_total"] += 1
            c["clarify_protocol"] += int(proto)
        elif row["expected_kind"] == "unsupported":
            c["unsupported_total"] += 1
            c["unsupported_protocol"] += int(proto)
        records.append(rec)

    qn = total["query"]
    cn = total["clarify"]
    un = total["unsupported"]
    n = len(rows)
    summary = {
        "rows": n,
        "expected_by_kind": dict(total),
        "prompt_truncated": c["prompt_truncated"], "skipped": c["skipped"],
        "empty_output": c["empty_output"],
        "strict_json_count": c["strict_json"],
        "strict_json_rate": round(c["strict_json"] / max(n, 1), 4),
        "lenient_json_count": c["lenient_json"],
        "lenient_json_rate": round(c["lenient_json"] / max(n, 1), 4),
        "parse_fail_count": c["parse_fail"],
        "protocol_correct": c["protocol_ok"],
        "protocol_rate_all": round(c["protocol_ok"] / max(n, 1), 4),
        "query_total": qn,
        "query_protocol_correct": c["query_protocol_ok"],
        "query_protocol_rate": round(c["query_protocol_ok"] / max(qn, 1), 4),
        "query_valid_03_rate": round(c["query_valid_03"] / max(qn, 1), 4),
        "sql_exact_rate": round(c["sql_exact"] / max(qn, 1), 4),
        "clarify_total": cn,
        "clarify_protocol_rate": round(c["clarify_protocol"] / max(cn, 1), 4),
        "unsupported_total": un,
        "unsupported_protocol_rate": round(c["unsupported_protocol"] / max(un, 1), 4),
        "got_kind": {k.replace("got_kind_", ""): v for k, v in c.items() if k.startswith("got_kind_")},
        "query_validator_error": c["query_validator_error"],
        "sql_normalize_error": c["sql_normalize_error"],
        "per_template": {k: dict(v) for k, v in sorted(per_template.items())},
        "boundary": "strict JSON only for protocol/business; lenient is diagnostic; fixed denominators from rows",
    }
    return {"summary": summary, "records": records}
