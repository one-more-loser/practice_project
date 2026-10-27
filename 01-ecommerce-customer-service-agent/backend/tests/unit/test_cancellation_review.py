"""审核协议的无数据库单元用例；此处结果不代表真实模型验收。"""

import json

import pytest
from langchain_core.messages import AIMessage

from cs_agent.cancellation.review import CONTRACT, RULES, parse_review, review_payload
from cs_agent.core.errors import AppError


def facts():
    return {
        "order_ref": "order_test",
        "order_version": 2,
        "confirmation_ref": "confirm_test",
        "payment_ref": "payment_test",
        "payment_entry_ref": "entry_test",
        "trade_status": "paid",
        "fulfillment_status": "unshipped",
        "paid_minor": "4000",
        "refundable_minor": "4000",
        "currency": "CNY",
        "owner_matches_confirmation": True,
        "confirmation_verified": True,
        "shipment_refs": [],
    }


def response(decision="approved", **changes):
    from cs_agent.actions.repository import fingerprint

    result = {
        "contract_version": CONTRACT,
        "rules_revision": RULES,
        "facts_digest": fingerprint(facts()),
        "decision": decision,
        "checks": [
            {"code": code, "result": "passed", "fact_refs": ["order_test"]}
            for code in ("ownership", "confirmation", "paid", "unshipped", "refundable_amount")
        ],
        "rule_refs": ["BR-CS-039", "BR-CS-012", "BR-CS-013"],
        "reason_codes": ["eligible"],
    }
    result.update(changes)
    return AIMessage(content=json.dumps(result))


def test_approved_is_structured_and_bound_to_exact_facts():
    result = parse_review(response(), facts())
    assert result["decision"] == "approved"
    assert "eligible" in result["reason_codes"]
    with pytest.raises(AppError, match="cancellation_review_protocol_error"):
        parse_review(response(facts_digest="0" * 64), facts())


@pytest.mark.parametrize(
    "changes",
    [
        {"checks": []},
        {"decision": "succeeded"},
        {"amount_minor": "1"},
        {"reason_codes": ["任意含个人信息的模型文本"]},
        {"rule_refs": ["invented-rule"]},
        {"checks": [{"code": "ownership", "result": "passed", "fact_refs": ["other"]}]},
        {"rules_revision": "old"},
    ],
)
def test_invalid_or_unbound_model_claim_never_authorizes(changes):
    with pytest.raises(AppError):
        parse_review(response(**changes), facts())


def test_tools_and_partial_json_are_not_review_approval():
    with pytest.raises(AppError):
        parse_review(AIMessage(content='{"decision":"approved"'), facts())
    with pytest.raises(AppError):
        parse_review(
            AIMessage(
                content=response().content, tool_calls=[{"name": "refund", "args": {}, "id": "x"}]
            ),
            facts(),
        )


def test_prompt_contains_only_allowlisted_business_facts_and_no_write_tool():
    payload = review_payload(facts())
    assert set(payload) == {"messages"}
    assert "BR-CS-039" in payload["messages"][0]["content"]
    assert "4000" in payload["messages"][1]["content"]
    assert "tools" not in payload


def test_review_prompt_schema_matches_the_strict_array_output_contract():
    from cs_agent.actions.repository import fingerprint

    specification = json.loads(
        review_payload(facts())["messages"][0]["content"].split("契约：", 1)[1]
    )
    schema = specification["output_schema"]
    assert schema["properties"]["checks"]["type"] == "array"
    assert schema["additionalProperties"] is False
    assert schema["properties"]["facts_digest"]["const"] == fingerprint(facts())
