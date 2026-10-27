"""固定测试库、公开政策的有界协议诊断；不记录密钥、私人上下文或业务写操作。"""

import json
import time
from datetime import UTC, datetime

from cs_agent.config import PROJECT_ROOT, Settings
from cs_agent.main import create_app
from cs_agent.runtime.worker import AgentWorker
from cs_agent.runtime_profiles import apply_runtime_profile
from fastapi.testclient import TestClient


def main():
    _, values = apply_runtime_profile("debian")
    settings = Settings(
        database_url=values["CS_TEST_DATABASE_URL"],
        query_database_url=values["CS_TEST_QUERY_DATABASE_URL"],
        expected_database="cs_agent_b0_test",
        agent_worker_enabled=False,
        expiration_worker_enabled=False,
        cookie_secure=False,
    )
    report = {
        "at": datetime.now(UTC).isoformat(),
        "scope": "public synthetic only",
        "cases": [],
    }
    app = create_app(settings)
    origin = {"Origin": "http://127.0.0.1:5173"}
    with TestClient(app) as client:
        worker = AgentWorker(app.state.db, settings, app.state.clock)
        original = worker.gateway.invoke
        active = []

        def observe(capability, payload, **kwargs):
            if capability == "chat" and len(active) >= 3:
                raise RuntimeError("diagnostic request budget exceeded")
            message = original(capability, payload, **kwargs)
            if capability == "chat":
                active.append(
                    {"content": message.content, "tool_calls": message.tool_calls}
                )
            return message

        worker.gateway.invoke = observe
        try:
            for index, question in enumerate(
                [
                    "没发货前，每个订单允许修改几次收货地址？",
                    "我想了解店铺规则：订单还没寄出，可以改几回收货地址？",
                    "按店铺政策，一个订单已经改过一次地址，还可以改第二次吗？",
                    "请查询公开政策：发货以后还能修改收货地址吗？",
                    "店铺政策以什么操作作为已经发货的界限？",
                ]
            ):
                active = []
                client.cookies.clear()
                guest = client.post("/api/v1/guest-session", headers=origin)
                assert guest.status_code == 201
                headers = {**origin, "X-CSRF-Token": guest.json()["data"]["csrf_token"]}
                response = client.post(
                    "/api/v1/conversations", headers=headers, json={}
                )
                assert response.status_code == 201
                ref = response.json()["data"]["conversation_id"]
                response = client.post(
                    f"/api/v1/conversations/{ref}/messages",
                    headers=headers,
                    json={
                        "client_message_id": f"protocol-diagnostic-{index}",
                        "text": question,
                    },
                )
                assert response.status_code == 202
                run = response.json()["data"]["run_id"]
                deadline = time.monotonic() + 50
                while True:
                    worker.process_once()
                    status = client.get("/api/v1/runs/" + run).json()["data"]
                    if (
                        status["status"] not in {"queued", "running"}
                        or time.monotonic() > deadline
                    ):
                        break
                    time.sleep(0.2)
                report["cases"].append(
                    {
                        "question": question,
                        "status": status["status"],
                        "responses": active,
                    }
                )
        finally:
            worker.close()
    output = PROJECT_ROOT / ".runtime/public-model-protocol-diagnostic.json"
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf8"
    )
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
