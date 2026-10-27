from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from cs_agent.core.context import Role, TrustedContext
from cs_agent.db.agent_action_tables import bindings
from cs_agent.db.seed import BATCH, STORE
from cs_agent.experience.service import ExperienceService
from cs_agent.feedback.aggregation import suppress, weekly_window
from cs_agent.runtime.worker import AgentWorker
from tests.api.test_checkout import create_order, key
from tests.api.test_graph_scripts import ScriptedModels
from tests.api.test_support_human import assign, create_ticket


def test_approved_case_skill_regression_and_source_revocation(environment, login_as):
    client, _, clock, *_ = environment
    ticket = create_ticket(client, login_as("customer_a"))
    assign(client, login_as("supervisor"), ticket)
    staff = login_as("support_a")
    closed = client.post(
        f"/api/v1/staff/tickets/{ticket}/actions",
        headers=staff,
        json={"action": "close", "text": "完成订单政策解释", "expected_version": 2},
    )
    assert closed.status_code == 200
    supervisor = login_as("supervisor")
    definition = {
        "conditions": ["顾客咨询退货条件"],
        "exclusions": ["需要审批或支付的业务"],
        "verification_steps": ["查询当前订单及历史政策"],
        "resolution_summary": "解释适用政策，例外交人工审核",
        "allowed_tools": ["orders.detail", "knowledge.search"],
        "handoff_conditions": ["资料不足或规则例外"],
    }
    payload = {
        "kind": "case",
        "name": "退货政策说明案例",
        "topic": "order",
        "source_ticket_id": ticket,
        "definition": definition,
    }
    created = client.post("/api/v1/staff/experience", headers=supervisor, json=payload)
    assert created.status_code == 200, created.text
    case = created.json()["data"]["document_id"]
    assert (
        client.post(f"/api/v1/staff/experience/{case}/publish", headers=supervisor).status_code
        == 409
    )
    for action in ["review", "publish"]:
        assert (
            client.post(f"/api/v1/staff/experience/{case}/{action}", headers=supervisor).status_code
            == 200
        )
    rollback = client.post(f"/api/v1/staff/experience/{case}/rollback", headers=supervisor)
    assert rollback.status_code == 200 and rollback.json()["data"]["revision"] == 2
    assert rollback.json()["data"]["status"] == "candidate"
    skill = client.post(
        "/api/v1/staff/experience",
        headers=supervisor,
        json={
            "kind": "skill",
            "name": "退货政策核对流程",
            "topic": "order",
            "source_case_ids": [case],
            "definition": definition,
        },
    ).json()["data"]["document_id"]
    assert (
        client.post(f"/api/v1/staff/experience/{skill}/review", headers=supervisor).status_code
        == 200
    )
    assert (
        client.post(f"/api/v1/staff/experience/{skill}/publish", headers=supervisor).status_code
        == 409
    )
    guard = client.post(f"/api/v1/staff/experience/{skill}/regress", headers=supervisor)
    assert guard.status_code == 200 and guard.json()["data"]["passed"] is True
    assert guard.json()["data"]["real_model_effectiveness"] == "unvalidated"
    assert (
        client.post(f"/api/v1/staff/experience/{skill}/publish", headers=supervisor).status_code
        == 200
    )
    ctx = TrustedContext(
        "customer_a", STORE, Role.CUSTOMER, "internal-test", 1, BATCH, 1, "test-experience"
    )
    with client.app.state.db.transaction() as session:
        result = ExperienceService().retrieve(session, ctx, "order", clock.now())
        assert len(result["procedures"]) == 2
        assert "source_ticket_id" not in str(result)
    assert (
        client.post(f"/api/v1/staff/experience/{case}/retire", headers=supervisor).status_code
        == 200
    )
    with client.app.state.db.transaction() as session:
        assert ExperienceService().retrieve(session, ctx, "order", clock.now())["procedures"] == []
    forbidden = {**definition, "allowed_tools": ["wallet.topup"]}
    assert (
        client.post(
            "/api/v1/staff/experience",
            headers=supervisor,
            json={**payload, "definition": forbidden},
        ).status_code
        == 403
    )
    login_as("customer_a")
    assert client.get("/api/v1/staff/experience").status_code == 403


def test_native_graph_proposal_replay_human_fence_and_owned_feedback(environment, login_as):
    import os

    from scripts.setup_checkpoints import setup

    setup(os.environ["CS_TEST_MIGRATION_URL"], os.environ["CS_ISOLATION_FILE"])
    client, engine, clock, *_ = environment
    customer = login_as("customer_a")
    order = create_order(client, customer)
    ref = client.post("/api/v1/conversations", headers=customer, json={}).json()["data"][
        "conversation_id"
    ]
    client.post(
        f"/api/v1/conversations/{ref}/messages",
        headers=customer,
        json={"client_message_id": "proposal-test", "text": "请取消我的这个订单"},
    )
    worker = AgentWorker(client.app.state.db, client.app.state.settings, clock)
    tool = {
        "kind": "tool_call",
        "tool": "action.propose",
        "arguments": {
            "action": "order.cancel",
            "resource_ref": order,
            "expected_version": 1,
            "parameters": {},
        },
    }
    try:
        worker.gateway = ScriptedModels([tool, tool, {"kind": "answer", "text": "已准备"}])
        assert worker.process_once()
    finally:
        worker.close()
    proposals = client.get(f"/api/v1/conversations/{ref}/action-proposals").json()["data"]["items"]
    assert len(proposals) == 1
    with engine.connect() as conn:
        assert conn.scalar(select(func.count()).select_from(bindings)) == 1
    history = client.get(f"/api/v1/conversations/{ref}/messages").json()["data"]["items"]
    answer = history[-1]
    assert "确认卡" in answer["text"]
    feedback = {"message_id": answer["message_id"], "rating": "helpful", "topics": ["order"]}
    first = client.post(f"/api/v1/conversations/{ref}/feedback", headers=customer, json=feedback)
    assert first.status_code == 200
    assert (
        client.post(
            f"/api/v1/conversations/{ref}/feedback", headers=customer, json=feedback
        ).json()["data"]
        == first.json()["data"]
    )
    assert (
        client.post(
            f"/api/v1/conversations/{ref}/feedback",
            headers=customer,
            json={**feedback, "rating": "unhelpful"},
        ).status_code
        == 409
    )
    conv = client.get(f"/api/v1/conversations/{ref}").json()["data"]
    queued = client.post(
        f"/api/v1/conversations/{ref}/human-requests",
        headers=customer,
        json={
            "reason": "请人工核对",
            "expected_epoch": conv["mode_epoch"],
            "expected_version": conv["version"],
        },
    )
    assert queued.status_code == 200
    submission = client.post(
        f"/api/v1/confirmations/{proposals[0]['confirmation_id']}/submit",
        headers={**customer, **key()},
        json={"decision": "confirm"},
    )
    assert submission.status_code == 409
    assert (
        client.get(f"/api/v1/me/orders/{order}").json()["data"]["trade_status"]
        == "awaiting_payment"
    )
    other = login_as("customer_b")
    assert (
        client.post(
            f"/api/v1/conversations/{ref}/feedback", headers=other, json=feedback
        ).status_code
        == 404
    )


def test_weekly_snapshot_no_raw_small_samples_and_machine_auth_fail_closed(environment, login_as):
    client, *_ = environment
    supervisor = login_as("supervisor")
    start = "2026-10-12T00:00:00+08:00"
    built = client.post(
        "/api/v1/staff/support-summary", headers=supervisor, json={"week_start": start}
    )
    assert built.status_code == 200, built.text
    payload = built.json()["data"]
    assert payload["timezone"] == "Asia/Shanghai" and payload["source_kind"] == "synthetic"
    assert len(payload["lineage_digest"]) == 64
    assert (
        client.post(
            "/api/v1/staff/support-summary",
            headers=supervisor,
            json={"week_start": "2026-01-05T00:00:00+08:00"},
        ).status_code
        == 410
    )
    assert (
        payload["privacy"]["min_subjects"] == 5 and payload["privacy"]["raw_data_exported"] is False
    )
    assert "source_ids" not in str(payload) and "owner_id" not in str(payload)
    replay = client.post(
        "/api/v1/staff/support-summary", headers=supervisor, json={"week_start": start}
    )
    assert replay.json()["data"] == payload
    assert (
        client.get(
            "/api/v1/internal/stores/store_sim/support-summary", params={"week_start": start}
        ).status_code
        == 503
    )
    from pydantic import SecretStr

    client.app.state.settings.operations_api_key = SecretStr("test-only-api-key")
    client.app.state.settings.operations_store_id = STORE
    path = "/api/v1/internal/stores/store_sim/support-summary"
    assert client.get(path, params={"week_start": start}).status_code == 401
    good = client.get(
        path, params={"week_start": start}, headers={"Authorization": "Bearer test-only-api-key"}
    )
    assert good.status_code == 200 and good.json()["data"] == payload


def test_small_sample_and_secondary_suppression_are_distinct_owner_based():
    records = [
        {"id": str(i), "owner": "one-owner", "family": "tickets", "topics": ["order"]}
        for i in range(20)
    ]
    assert suppress(records)[2]["cells"][0]["count"] is None
    records = [
        {"id": str(i), "owner": str(i), "family": "tickets", "topics": ["order"]} for i in range(5)
    ]
    assert suppress(records)[2]["cells"][0]["count"] == 5
    records.append({"id": "small", "owner": "six", "family": "tickets", "topics": ["complaint"]})
    assert all(cell["count"] is None for cell in suppress(records)[2]["cells"])
    start, end = weekly_window(datetime(2026, 10, 12, tzinfo=ZoneInfo("Asia/Shanghai")))
    assert end - start == timedelta(days=7)


def test_revision_replaces_same_snapshot_and_consumer_replay_does_not_add_counts(environment):
    from cs_agent.core.context import Role, TrustedContext
    from cs_agent.db.seed import BATCH, STORE
    from cs_agent.feedback.aggregation import FeedbackService

    client, _, clock, *_ = environment

    class FixtureRecords(FeedbackService):
        """显式的生产端输入替身：只验证聚合/修订契约，不冒充02真实消费。"""

        size = 5

        def _records(self, session, ctx, start, end):
            return [
                {
                    "id": str(i),
                    "owner": str(i),
                    "family": "tickets",
                    "topics": ["order"],
                    "digest": "fixture",
                }
                for i in range(self.size)
            ]

    producer = FixtureRecords()
    ctx = TrustedContext(
        "supervisor", STORE, Role.SUPERVISOR, "test-internal", 1, BATCH, 1, "contract-fixture"
    )
    start = datetime(2026, 10, 12, tzinfo=ZoneInfo("Asia/Shanghai"))
    with client.app.state.db.transaction() as session:
        first = producer.build(session, ctx, start, clock.now())
    producer.size = 6
    with client.app.state.db.transaction() as session:
        assert producer.build(session, ctx, start, clock.now() + timedelta(days=1)) == first
    producer.size = 10
    with client.app.state.db.transaction() as session:
        second = producer.build(session, ctx, start, clock.now() + timedelta(days=1))
    assert first["snapshot_id"] == second["snapshot_id"]
    assert second["revision"] == 2 and second["supersedes_revision"] == 1
    seen = set()
    consumed = {}
    for event in [first, first, second, second]:
        if event["event_id"] not in seen:
            seen.add(event["event_id"])
            consumed[event["snapshot_id"]] = event
    assert len(consumed) == 1 and consumed[first["snapshot_id"]]["revision"] == 2
