"""真实双Uvicorn进程、WS补偿及已确认消息重启重放；只允许固定测试库。"""

import json
import os
import socket
import subprocess
import sys
import time

import httpx
from cs_agent.config import PROJECT_ROOT, Settings
from cs_agent.db.session import verify_target
from cs_agent.runtime_profiles import apply_runtime_profile
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from websockets.sync.client import connect


def main():
    _, values = apply_runtime_profile("debian")
    url = values["CS_TEST_MIGRATION_URL"]
    if make_url(url).database != "cs_agent_b0_test":
        raise RuntimeError("fixed test database required")
    engine = create_engine(url, hide_parameters=True)
    verify_target(
        engine, Settings(expected_database="cs_agent_b0_test"), migration=True
    )
    engine.dispose()
    for port in (8000, 5173, 8002, 8003):
        with socket.socket() as probe:
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                raise RuntimeError("verification port already occupied")
    env = {
        **os.environ,
        "CS_DATABASE_URL": values["CS_TEST_DATABASE_URL"],
        "CS_QUERY_DATABASE_URL": values["CS_TEST_QUERY_DATABASE_URL"],
        "CS_EXPECTED_DATABASE": "cs_agent_b0_test",
        "CS_COOKIE_SECURE": "false",
        "CS_EXPIRATION_WORKER_ENABLED": "false",
        "CS_AGENT_WORKER_ENABLED": "false",
    }
    children = []
    logs = []

    def start(port):
        log = (PROJECT_ROOT / f".runtime/v1-human-{port}.log").open("ab")
        logs.append(log)
        child = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "cs_agent.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--no-access-log",
                "--log-level",
                "warning",
            ],
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        children.append(child)
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            if child.poll() is not None:
                raise RuntimeError("verification process exited; inspect private log")
            try:
                if (
                    httpx.get(
                        f"http://127.0.0.1:{port}/health/ready",
                        timeout=1,
                        trust_env=False,
                    ).status_code
                    == 200
                ):
                    return child
            except httpx.HTTPError:
                pass
            time.sleep(0.1)
        raise RuntimeError("verification readiness timed out")

    accounts = json.loads(
        (PROJECT_ROOT / ".runtime/demo-accounts.json").read_text(encoding="utf8")
    )
    origin = "http://127.0.0.1:5173"

    def login(client, name):
        response = client.post(
            "/api/v1/session",
            json={"login_name": name, "password": accounts[name]},
            headers={"Origin": origin},
        )
        assert response.status_code == 200
        return {"Origin": origin, "X-CSRF-Token": response.json()["data"]["csrf_token"]}

    def data(response):
        if response.status_code not in {200, 201, 202}:
            raise RuntimeError(
                f"fixed verification request failed status={response.status_code}"
            )
        return response.json()["data"]

    def frame(ws, kind, *, message_type=None):
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            item = json.loads(ws.recv(timeout=3))
            if item["type"] == "ping":
                ws.send(json.dumps({"type": "pong"}))
                continue
            if item["type"] == kind and (
                message_type is None
                or item.get("event", {}).get("type") == message_type
            ):
                return item
        raise RuntimeError("expected websocket frame not received")

    def websocket(client, port, csrf, ref, epoch, after):
        cookies = "; ".join(f"{key}={value}" for key, value in client.cookies.items())
        ws = connect(
            f"ws://127.0.0.1:{port}/api/v1/realtime",
            origin=origin,
            additional_headers={"Cookie": cookies},
            proxy=None,
            open_timeout=3,
            close_timeout=3,
            ping_interval=None,
            compression=None,
        )
        ws.send(
            json.dumps(
                {
                    "type": "hello",
                    "csrf_token": csrf,
                    "subscriptions": [
                        {
                            "conversation_id": ref,
                            "after_seq": after,
                            "expected_epoch": epoch,
                        }
                    ],
                }
            )
        )
        frame(ws, "ready")
        return ws

    started = time.monotonic()
    try:
        a, b = start(8002), start(8003)
        with (
            httpx.Client(
                base_url="http://127.0.0.1:8002", timeout=10, trust_env=False
            ) as customer,
            httpx.Client(
                base_url="http://127.0.0.1:8002", timeout=10, trust_env=False
            ) as manager,
            httpx.Client(
                base_url="http://127.0.0.1:8003", timeout=10, trust_env=False
            ) as staff,
        ):
            ch = login(customer, "customer_a")
            mh = login(manager, "supervisor")
            sh = login(staff, "support_a")
            conv = data(customer.post("/api/v1/conversations", headers=ch, json={}))
            ref = conv["conversation_id"]
            queued = data(
                customer.post(
                    f"/api/v1/conversations/{ref}/human-requests",
                    headers=ch,
                    json={
                        "reason": "双进程消息可靠性核验",
                        "expected_epoch": 1,
                        "expected_version": 1,
                    },
                )
            )
            ticket = queued["ticket_id"]
            assignment = data(
                manager.post(
                    f"/api/v1/staff/tickets/{ticket}/assignments",
                    headers=mh,
                    json={
                        "staff_id": "support_a",
                        "reason": "核验跨进程分派",
                        "expected_version": 1,
                    },
                )
            )
            taken = data(
                staff.post(
                    f"/api/v1/staff/conversations/{ref}/takeover",
                    headers=sh,
                    json={
                        "assignment_id": assignment["assignment_id"],
                        "assignment_version": 1,
                        "expected_epoch": queued["mode_epoch"],
                        "expected_version": queued["version"],
                    },
                )
            )
            epoch = taken["mode_epoch"]
            body = {
                "client_message_id": "cross-process-stable-001",
                "text": "跨进程持久消息验证",
                "assignment_version": 1,
                "expected_epoch": epoch,
            }
            with websocket(customer, 8002, ch["X-CSRF-Token"], ref, epoch, 0) as ws:
                original = data(
                    staff.post(
                        f"/api/v1/staff/conversations/{ref}/messages",
                        headers=sh,
                        json=body,
                    )
                )
                received = frame(ws, "event", message_type="message.accepted")["event"]
                assert received["data"]["message_id"] == original["message_id"]
            old_pid = b.pid
            b.terminate()
            b.wait(timeout=10)
            restarted = start(8003)
            replay = data(
                staff.post(
                    f"/api/v1/staff/conversations/{ref}/messages", headers=sh, json=body
                )
            )
            assert replay["message_id"] == original["message_id"]
            with websocket(
                customer, 8003, ch["X-CSRF-Token"], ref, epoch, received["seq"] - 1
            ) as ws:
                compensated = frame(ws, "event", message_type="message.accepted")[
                    "event"
                ]
                assert compensated["event_id"] == received["event_id"]
            items = data(customer.get(f"/api/v1/conversations/{ref}/messages"))["items"]
            assert (
                len([item for item in items if item["source_role"] == "support"]) == 1
            )
            with websocket(
                staff, 8003, sh["X-CSRF-Token"], ref, epoch, received["seq"]
            ) as ws:
                current = data(manager.get(f"/api/v1/staff/tickets/{ticket}"))
                data(
                    manager.post(
                        f"/api/v1/staff/tickets/{ticket}/assignments",
                        headers=mh,
                        json={
                            "staff_id": "support_b",
                            "reason": "核验旧员工撤权",
                            "expected_version": current["version"],
                        },
                    )
                )
                assert frame(ws, "access.revoked")["code"] in {"not_found", "forbidden"}
            assert staff.post(
                f"/api/v1/staff/conversations/{ref}/messages",
                headers=sh,
                json={**body, "client_message_id": "revoked-attempt"},
            ).status_code in {403, 404}
            report = {
                "scope": "human_only_two_real_api_processes_no_agent_workers",
                "database": "cs_agent_b0_test",
                "cross_process_delivery": True,
                "restart_after_ack_replayed_same_message": True,
                "reconnect_same_event_id": True,
                "support_messages": 1,
                "reassignment_revokes_old_socket_and_http": True,
                "process_ids": [a.pid, old_pid, restarted.pid],
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "real_model_calls": 0,
                "limits": "Not a mid-commit crash, Agent recovery, load or eight-hour availability result",
            }
            target = PROJECT_ROOT / "docs/06-acceptance/V1-business/multiprocess.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf8"
            )
            print(json.dumps(report))
    finally:
        for child in children:
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=5)
        for log in logs:
            log.close()


if __name__ == "__main__":
    main()
