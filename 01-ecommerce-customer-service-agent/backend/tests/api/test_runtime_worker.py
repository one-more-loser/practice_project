import json
import os
from datetime import timedelta

import pytest
from langchain_core.messages import AIMessage
from sqlalchemy import select

from cs_agent.core.errors import AppError
from cs_agent.db.runtime_tables import runs
from cs_agent.runtime.leases import RuntimeService
from cs_agent.runtime.worker import AgentWorker


class ScriptedGateway:
    """显式测试替身，仅在测试构造器注入；没有HTTP/模型请求。"""

    def __init__(self, *steps):
        self.steps = list(steps)
        self.requests = []

    def invoke(self, capability, payload, remaining=20):
        assert capability == "chat"
        self.requests.append(payload)
        return AIMessage(content=json.dumps(self.steps.pop(0), ensure_ascii=False))


def prepare(client, headers, text_="你好"):
    created = client.post("/api/v1/conversations", headers=headers, json={})
    assert created.status_code == 201, created.text
    ref = created.json()["data"]["conversation_id"]
    response = client.post(
        f"/api/v1/conversations/{ref}/messages",
        headers=headers,
        json={"client_message_id": "test-runtime-1", "text": text_},
    )
    assert response.status_code == 202, response.text
    return ref, response.json()["data"]["run_id"]


@pytest.fixture
def checkpoints(environment):
    from scripts.setup_checkpoints import setup

    setup(os.environ["CS_TEST_MIGRATION_URL"], os.environ["CS_ISOLATION_FILE"])
    return environment


def test_worker_persists_reply_and_restart_does_not_repeat(checkpoints, login_as):
    client, _, clock, *_ = checkpoints
    headers = login_as("customer_a")
    ref, run_id = prepare(client, headers)
    app = client.app
    worker = AgentWorker(app.state.db, app.state.settings, clock)
    worker.gateway = ScriptedGateway({"kind": "answer", "text": "您好，请告诉我需要查询的内容。"})
    try:
        assert worker.process_once() is True
        history = client.get(f"/api/v1/conversations/{ref}/messages").json()["data"]["items"]
        assert [row["source_role"] for row in history] == ["customer", "assistant"]
        assert client.get("/api/v1/runs/" + run_id).json()["data"]["status"] == "completed"
    finally:
        worker.close()
    restarted = AgentWorker(app.state.db, app.state.settings, clock)
    try:
        assert restarted.process_once() is False
    finally:
        restarted.close()


def test_guest_clarification_guides_login_instead_of_requesting_private_order_id(checkpoints):
    client, _, clock, *_ = checkpoints
    origin = {"Origin": "http://127.0.0.1:5173"}
    guest = client.post("/api/v1/guest-session", headers=origin)
    assert guest.status_code == 201
    headers = {**origin, "X-CSRF-Token": guest.json()["data"]["csrf_token"]}
    ref, _ = prepare(client, headers, "查询我的订单和余额")
    worker = AgentWorker(client.app.state.db, client.app.state.settings, clock)
    worker.gateway = ScriptedGateway({"kind": "clarify", "text": "请先登录"})
    try:
        assert worker.process_once()
        history = client.get(f"/api/v1/conversations/{ref}/messages").json()["data"]["items"]
        assert "登录" in history[-1]["text"]
        assert "订单编号" not in history[-1]["text"]
    finally:
        worker.close()


def test_expired_lease_reclaim_fences_previous_worker(checkpoints, login_as):
    client, _, clock, *_ = checkpoints
    headers = login_as("customer_a")
    ref, _ = prepare(client, headers)
    db = client.app.state.db
    runtime = RuntimeService()
    first = runtime.claim(db, "first", clock.now())
    clock.value += timedelta(seconds=31)
    second = runtime.claim(db, "second", clock.now())
    assert second.fence > first.fence
    with pytest.raises(AppError, match="stale_execution"):
        runtime.finish(db, first, clock, "旧回复", {})
    runtime.finish(db, second, clock, "当前回复", {})
    assert [
        row["text"]
        for row in client.get(f"/api/v1/conversations/{ref}/messages").json()["data"]["items"]
    ] == ["你好", "当前回复"]


def test_completed_tool_result_is_grounded_in_real_balance(checkpoints, login_as):
    client, _, clock, *_ = checkpoints
    headers = login_as("customer_a")
    ref, _ = prepare(client, headers, "查一下我的余额")
    worker = AgentWorker(client.app.state.db, client.app.state.settings, clock)
    worker.gateway = ScriptedGateway(
        {"kind": "tool_call", "tool": "wallet.summary", "arguments": {}},
        {"kind": "answer", "text": "您的余额是999999元。"},
    )
    try:
        assert worker.process_once()
        history = client.get(f"/api/v1/conversations/{ref}/messages").json()["data"]["items"]
        assert "999999" not in history[-1]["text"]
        assert "0.00" in history[-1]["text"]
        assert worker.gateway.requests[1]["messages"][-1]["role"] == "user"
        assert worker.gateway.requests[1]["messages"][-1]["content"].startswith("只讀工具結果")
    finally:
        worker.close()


def test_cancelled_task_cannot_publish(checkpoints, login_as):
    client, _, clock, *_ = checkpoints
    headers = login_as("customer_a")
    ref, run_id = prepare(client, headers)
    db = client.app.state.db
    runtime = RuntimeService()
    lease = runtime.claim(db, "cancelled-worker", clock.now())
    assert client.post(f"/api/v1/runs/{run_id}/cancel", headers=headers, json={}).status_code == 200
    with pytest.raises(AppError, match="stale_execution"):
        runtime.finish(db, lease, clock, "不应展示", {})
    assert len(client.get(f"/api/v1/conversations/{ref}/messages").json()["data"]["items"]) == 1


def test_logout_during_model_call_prevents_answer(checkpoints, login_as):
    client, _, clock, *_ = checkpoints
    headers = login_as("customer_a")
    ref, run_id = prepare(client, headers)

    class LogoutGateway:
        def invoke(self, *args, **kwargs):
            assert client.delete("/api/v1/session", headers=headers).status_code == 200
            return AIMessage(content='{"kind":"answer","text":"不应展示"}')

    worker = AgentWorker(client.app.state.db, client.app.state.settings, clock)
    worker.gateway = LogoutGateway()
    try:
        assert worker.process_once()
        with client.app.state.db.transaction() as session:
            assert session.scalar(select(runs.c.status).where(runs.c.run_id == run_id)) == "failed"
            from cs_agent.db.runtime_tables import messages

            assert (
                len(
                    session.execute(select(messages).where(messages.c.conversation_id == ref)).all()
                )
                == 1
            )
    finally:
        worker.close()


def test_completed_checkpoint_recovers_before_reply_commit(checkpoints, login_as, monkeypatch):
    client, _, clock, *_ = checkpoints
    headers = login_as("customer_a")
    ref, run_id = prepare(client, headers)
    worker = AgentWorker(client.app.state.db, client.app.state.settings, clock)
    worker.gateway = ScriptedGateway({"kind": "answer", "text": "你好"})

    def interrupted(*args, **kwargs):
        raise AppError("model_timeout", 503, "模拟完成检查点后尚未提交回复")

    monkeypatch.setattr(worker.runtime, "finish", interrupted)
    try:
        assert worker.process_once()
    finally:
        worker.close()
    clock.value += timedelta(seconds=3)
    resumed = AgentWorker(client.app.state.db, client.app.state.settings, clock)
    resumed.gateway = ScriptedGateway()  # 恢复已完成检查点，不重复调用模型。
    try:
        assert resumed.process_once()
        assert client.get(f"/api/v1/runs/{run_id}").json()["data"]["status"] == "completed"
        assert len(client.get(f"/api/v1/conversations/{ref}/messages").json()["data"]["items"]) == 2
        assert resumed.process_once() is False
    finally:
        resumed.close()
