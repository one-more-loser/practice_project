"""模型仅产生有事实引用的资格结论，不决定金额、不取得写工具。"""

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from cs_agent.actions.repository import fingerprint
from cs_agent.models.gateway import failure

CONTRACT = "CS-CANCEL-REVIEW-v1"
RULES = "CS-CANCEL-RULES-v1"
SKILL = "CS-CANCEL-REVIEW-v1"
CHECKS = {"ownership", "confirmation", "paid", "unshipped", "refundable_amount"}
RULE_REFS = {"BR-CS-003", "BR-CS-012", "BR-CS-013", "BR-CS-025", "BR-CS-039"}
REASONS = {
    "eligible": "本人已确认，付款及未发货事实满足普通取消条件",
    "not_owner": "主体与订单归属不符",
    "confirmation_invalid": "顾客确认无效或缺失",
    "not_paid": "付款事实不满足条件",
    "already_shipped": "已有发货事实",
    "amount_inconsistent": "原付款与可退金额不一致",
    "facts_missing": "事实不足，需协助补齐后重新审核",
    "policy_uncertain": "规则适用不确定，需协助取证后重新审核",
}


class Check(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    code: Literal["ownership", "confirmation", "paid", "unshipped", "refundable_amount"]
    result: Literal["passed", "failed", "unknown"]
    fact_refs: list[str] = Field(min_length=1, max_length=8)


class Review(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    contract_version: Literal["CS-CANCEL-REVIEW-v1"]
    rules_revision: Literal["CS-CANCEL-RULES-v1"]
    facts_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    decision: Literal["approved", "rejected", "needs_facts"]
    checks: list[Check] = Field(min_length=5, max_length=5)
    rule_refs: list[str] = Field(min_length=1, max_length=5)
    reason_codes: list[str] = Field(min_length=1, max_length=5)


def review_payload(facts):
    refs = sorted(
        {
            facts.get("order_ref"),
            facts.get("confirmation_ref"),
            facts.get("payment_ref"),
            facts.get("payment_entry_ref"),
            *facts.get("shipment_refs", []),
            *[line["item_ref"] for line in facts.get("items", [])],
        }
        - {None}
    )
    schema = Review.model_json_schema()
    schema["properties"]["facts_digest"]["const"] = fingerprint(facts)
    schema["properties"]["rule_refs"]["items"]["enum"] = sorted(RULE_REFS)
    schema["properties"]["reason_codes"]["items"]["enum"] = sorted(REASONS)
    schema["$defs"]["Check"]["properties"]["fact_refs"]["items"]["enum"] = refs
    specification = {
        "output_schema": schema,
        "required_check_codes": sorted(CHECKS),
        "reason_definitions": REASONS,
    }
    return {
        "messages": [
            {
                "role": "system",
                "content": (
                    "你是普通已付未发货订单取消资格审核Agent。只返回一个符合契约的JSON对象。"
                    "每个check恰好一项且有给定事实引用；不得补造事实。BR-CS-039要求顾客确认、"
                    "真实付款、未发货与可退额核验；BR-CS-012发货后拒绝普通取消；BR-CS-013"
                    "退款与库存必须有原业务依据。全部核验通过才可approved，不确定用needs_facts。"
                    "不得调用工具或执行退款，不决定金额、身份、收款账户，不输出思维链或个人信息。"
                    "checks必须为5个检查对象组成的数组，不能是以code为键的对象。"
                    "只输出符合output_schema的结果实例，不输出schema、type或其他额外字段。"
                    "facts_digest必须逐字复制schema中的const，fact_refs从给定enum取值。"
                    "reason_codes只能选契约提供的机器码。契约："
                    + json.dumps(specification, ensure_ascii=False, sort_keys=True)
                ),
            },
            {"role": "user", "content": json.dumps(facts, ensure_ascii=False, sort_keys=True)},
        ]
    }


def parse_review(message, facts):
    try:
        if (
            message.tool_calls
            or getattr(message, "invalid_tool_calls", [])
            or not isinstance(message.content, str)
            or len(message.content) > 12000
        ):
            raise ValueError()
        value = Review.model_validate_json(message.content).model_dump()
        refs = {
            facts.get("order_ref"),
            facts.get("confirmation_ref"),
            facts.get("payment_ref"),
            facts.get("payment_entry_ref"),
            *facts.get("shipment_refs", []),
            *[line["item_ref"] for line in facts.get("items", [])],
        } - {None}
        if (
            value["facts_digest"] != fingerprint(facts)
            or {item["code"] for item in value["checks"]} != CHECKS
            or not set(value["rule_refs"]) <= RULE_REFS
            or "BR-CS-039" not in value["rule_refs"]
            or not set(value["reason_codes"]) <= REASONS.keys()
            or any(not set(item["fact_refs"]) <= refs for item in value["checks"])
        ):
            raise ValueError()
        if value["decision"] == "approved" and (
            any(item["result"] != "passed" for item in value["checks"])
            or value["reason_codes"] != ["eligible"]
        ):
            raise ValueError()
        if value["decision"] != "approved" and "eligible" in value["reason_codes"]:
            raise ValueError()
        value["basis_summary"] = [REASONS[code] for code in value["reason_codes"]]
        return value
    except Exception:
        raise failure("cancellation_review_protocol_error") from None
