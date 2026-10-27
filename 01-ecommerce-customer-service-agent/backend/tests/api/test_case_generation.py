import json

from langchain_core.messages import AIMessage

from tests.api.test_support_human import assign, create_ticket


def test_case_generation_unconfigured_then_explicit_protocol_double_only_candidate(
    environment, login_as
):
    client, *_ = environment
    ticket = create_ticket(client, login_as("customer_a"))
    assign(client, login_as("supervisor"), ticket)
    staff = login_as("support_a")
    assert (
        client.post(
            f"/api/v1/staff/tickets/{ticket}/actions",
            headers=staff,
            json={
                "action": "close",
                "text": "已向customer_a说明订单适用政策，未执行退款",
                "expected_version": 2,
            },
        ).status_code
        == 200
    )
    manager = login_as("supervisor")
    assert (
        client.post(
            "/api/v1/staff/experience/generate-case",
            headers=manager,
            json={"source_ticket_id": ticket},
        ).status_code
        == 503
    )

    class CandidateDouble:
        def invoke(self, capability, payload, remaining):
            raw = json.dumps(payload, ensure_ascii=False)
            assert capability == "chat" and "customer_a" not in raw and ticket not in raw
            assert not client.app.state.db.engine.pool.checkedout()
            return AIMessage(
                content=json.dumps(
                    {
                        "name": "订单政策说明案例",
                        "topic": "order",
                        "definition": {
                            "conditions": ["顾客查询退货规则"],
                            "exclusions": ["不可将历史结论当作新顾客退款资格"],
                            "verification_steps": ["核对当前本人订单适用政策"],
                            "resolution_summary": "解释规则并告知受控入口",
                            "allowed_tools": ["orders.detail"],
                            "handoff_conditions": ["争议或政策缺失转人工核对"],
                        },
                    },
                    ensure_ascii=False,
                )
            )

    client.app.state.model_gateway = CandidateDouble()
    generated = client.post(
        "/api/v1/staff/experience/generate-case", headers=manager, json={"source_ticket_id": ticket}
    )
    assert generated.status_code == 200, generated.text
    assert generated.json()["data"]["status"] == "candidate"
    support = login_as("support_a")
    assert (
        client.post(
            "/api/v1/staff/experience/generate-case",
            headers=support,
            json={"source_ticket_id": ticket},
        ).status_code
        == 403
    )
