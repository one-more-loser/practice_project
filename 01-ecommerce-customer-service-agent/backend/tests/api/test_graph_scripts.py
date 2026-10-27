import json
import os

from langchain_core.messages import AIMessage
from sqlalchemy import select

from cs_agent.config import PROJECT_ROOT
from cs_agent.core.errors import AppError
from cs_agent.db.runtime_tables import conversations
from cs_agent.runtime.worker import AgentWorker


class ScriptedModels:
    """只返回固定脚本，不下载、不调用网络模型。"""

    def __init__(self, steps):
        self.steps = list(steps)

    def invoke(self, capability, payload, remaining=20):
        if capability != "chat":
            raise AppError("model_unconfigured", 503, "测试未配置外部能力")
        value = self.steps.pop(0)
        return AIMessage(
            content=value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        )


def test_thirty_native_graph_workflow_scripts(environment, login_as):
    from scripts.setup_checkpoints import setup

    setup(os.environ["CS_TEST_MIGRATION_URL"], os.environ["CS_ISOLATION_FILE"])
    client, _, clock, *_ = environment
    dataset = json.loads(
        (PROJECT_ROOT / "tests/evaluation/conversation/scripted-cases.json").read_text(
            encoding="utf-8"
        )
    )
    assert len(dataset["cases"]) == 30 and not dataset["real_model_quality"]
    worker = AgentWorker(client.app.state.db, client.app.state.settings, clock)
    try:
        for case in dataset["cases"]:
            if case["principal"] == "guest":
                client.cookies.clear()
                response = client.post(
                    "/api/v1/guest-session", headers={"Origin": "http://127.0.0.1:5173"}
                )
                headers = {
                    "Origin": "http://127.0.0.1:5173",
                    "X-CSRF-Token": response.json()["data"]["csrf_token"],
                }
            else:
                headers = login_as("customer_a")
            ref = client.post("/api/v1/conversations", headers=headers, json={}).json()["data"][
                "conversation_id"
            ]
            response = client.post(
                f"/api/v1/conversations/{ref}/messages",
                headers=headers,
                json={"client_message_id": case["id"], "text": case["text"]},
            )
            assert response.status_code == 202, case["id"]
            run_id = response.json()["data"]["run_id"]
            worker.gateway = ScriptedModels(case["steps"])
            assert worker.process_once(), case["id"]
            run = client.get(f"/api/v1/runs/{run_id}").json()["data"]
            assert run["status"] == case["expected_status"], (
                case["id"],
                run["status"],
                run["error_code"],
            )
            history = client.get(f"/api/v1/conversations/{ref}/messages").json()["data"]["items"]
            if case["expected_text"]:
                assert case["expected_text"] in history[-1]["text"], case["id"]
            else:
                assert len(history) == 1, case["id"]
    finally:
        worker.close()


def test_three_clarifications_and_reset_slots(environment, login_as):
    from scripts.setup_checkpoints import setup

    setup(os.environ["CS_TEST_MIGRATION_URL"], os.environ["CS_ISOLATION_FILE"])
    client, _, clock, *_ = environment
    headers = login_as("customer_a")
    ref = client.post("/api/v1/conversations", headers=headers, json={}).json()["data"][
        "conversation_id"
    ]
    worker = AgentWorker(client.app.state.db, client.app.state.settings, clock)
    try:
        for index in range(3):
            result = client.post(
                f"/api/v1/conversations/{ref}/messages",
                headers=headers,
                json={"client_message_id": f"clarify-{index}", "text": "条件还不完整"},
            )
            action = {"kind": "clarify", "text": "请补充", "slots": {"order_ref": "old"}}
            if index == 1:
                action.update(reset_slots=True, slots={"intent": "knowledge"})
            worker.gateway = ScriptedModels([action])
            assert worker.process_once()
            run_id = result.json()["data"]["run_id"]
            assert client.get(f"/api/v1/runs/{run_id}").json()["data"]["status"] == (
                "completed" if index == 2 else "waiting"
            )
            with client.app.state.db.transaction() as session:
                state = session.scalar(
                    select(conversations.c.dialog_state).where(
                        conversations.c.conversation_id == ref
                    )
                )
                if index == 1:
                    assert "order_ref" not in state["slots"]
                if index == 2:
                    assert state == {"slots": {}, "clarification_rounds": 0}
    finally:
        worker.close()
