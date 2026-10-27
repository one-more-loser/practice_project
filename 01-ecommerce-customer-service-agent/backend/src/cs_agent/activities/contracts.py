"""XOPS-ACTIVITY-v0.1 严格边界；摘要始终使用原始 Manifest。"""

import hashlib
import json
import re
from datetime import datetime

from cs_agent.core.errors import AppError

VERSION = "XOPS-ACTIVITY-v0.1"
MAX = 2**63 - 1


def reject(code="activity_contract_invalid", status=422):
    raise AppError(code, status, "活动契约或当前状态不符合要求")


def exact(value, keys):
    if not isinstance(value, dict) or set(value) != set(keys.split()):
        reject()


def ident(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 100:
        reject()


def integer(value, minimum=1):
    if type(value) is not int or not minimum <= value <= MAX:
        reject()


def money(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,19}", value) or int(value) > MAX:
        reject()
    return int(value)


def instant(value):
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None:
            reject()
        return result
    except (ValueError, TypeError, AttributeError):
        reject()


def manifest_digest(manifest):
    return hashlib.sha256(
        json.dumps(
            manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    ).hexdigest()


def validate_manifest(m):
    exact(
        m,
        "contract_version store_id plan_id plan_version rule currency start_at end_at scenario_policy budget_cap_minor fixed_fee_minor variable_budget_cap_minor lines source_snapshot_id source_snapshot_revision source_kind",
    )
    if m["contract_version"] != VERSION or m["rule"] != "direct" or m["currency"] != "CNY":
        reject()
    for key in ("store_id", "plan_id", "source_snapshot_id"):
        ident(m[key])
    integer(m["plan_version"])
    integer(m["source_snapshot_revision"])
    if not isinstance(m["scenario_policy"], str) or not isinstance(m["source_kind"], str):
        reject()
    if m["scenario_policy"] not in {"low", "base", "high", "all"} or m["source_kind"] not in {
        "synthetic",
        "derived",
        "business_import",
    }:
        reject()
    if instant(m["start_at"]) >= instant(m["end_at"]):
        reject()
    if money(m["fixed_fee_minor"]) + money(m["variable_budget_cap_minor"]) != money(
        m["budget_cap_minor"]
    ):
        reject()
    if not isinstance(m["lines"], list) or not 1 <= len(m["lines"]) <= 200:
        reject()
    ids = []
    for line in m["lines"]:
        exact(line, "sku_id base_price_minor activity_price_minor quantity_cap price_version")
        ident(line["sku_id"])
        integer(line["quantity_cap"])
        integer(line["price_version"])
        if not 0 < money(line["activity_price_minor"]) <= money(line["base_price_minor"]):
            reject()
        if money(line["base_price_minor"]) * line["quantity_cap"] > MAX:
            reject()
        ids.append(line["sku_id"])
    if ids != sorted(set(ids)):
        reject()
    return m


def validate_grant(g, expected, manifest, now):
    exact(
        g,
        "store_id plan_id approval_ref intent_id manifest_digest actor_ref auth_version capability_revision preview_ref contract_version grant_ref plan_version manifest issued_at expires_at",
    )
    validate_manifest(g["manifest"])
    for key in ("auth_version", "capability_revision", "plan_version"):
        integer(g[key])
    if (
        g["contract_version"] != VERSION
        or g["manifest"] != manifest
        or g["manifest_digest"] != manifest_digest(manifest)
        or g["plan_version"] != manifest["plan_version"]
        or any(g.get(k) != v for k, v in expected.items())
    ):
        reject("activity_grant_binding", 409)
    ident(g["grant_ref"])
    issued, expires = instant(g["issued_at"]), instant(g["expires_at"])
    if not issued <= now < expires or not 0 < (expires - issued).total_seconds() <= 60:
        reject("activity_grant_expired", 409)
    return g
