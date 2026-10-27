"""真实Uvicorn进程重启、持久ACK与SSE重放；固定测试库、已审核FAQ，无模型替身。"""

import json
import os
import subprocess
import sys
import time

import httpx
from cs_agent.config import PROJECT_ROOT, Settings
from cs_agent.db.session import verify_target
from cs_agent.runtime_profiles import apply_runtime_profile
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url


def main():
    _, values = apply_runtime_profile("debian")
    url = values["CS_TEST_MIGRATION_URL"]
    if make_url(url).database != "cs_agent_b0_test":
        raise RuntimeError("fixed test database required")
    engine = create_engine(url, hide_parameters=True)
    verify_target(
        engine, Settings(expected_database="cs_agent_b0_test"), migration=True
    )
    with engine.connect() as connection:
        assert (
            connection.execute(
                text(
                    "SELECT count(*) FROM faq_revision WHERE faq_id='FAQ-SIM-001' AND status='published'"
                )
            ).scalar_one()
            == 1
        ), "publish the test FAQ through the browser journey first"
    engine.dispose()
    env = os.environ.copy()
    env.update(
        CS_DATABASE_URL=values["CS_TEST_DATABASE_URL"],
        CS_QUERY_DATABASE_URL=values["CS_TEST_QUERY_DATABASE_URL"],
        CS_EXPECTED_DATABASE="cs_agent_b0_test",
        CS_COOKIE_SECURE="false",
        CS_EXPIRATION_WORKER_ENABLED="false",
    )
    server = None
    base = "http://127.0.0.1:8002"
    # 不抢占已有端口；不会终止其他工程的进程。
    import socket

    with socket.socket() as probe:
        if probe.connect_ex(("127.0.0.1", 8002)) == 0:
            raise RuntimeError("verification port already occupied")
    log = (PROJECT_ROOT / ".runtime/b2-http-server.log").open("wb")

    def start(enabled):
        child_env = {**env, "CS_AGENT_WORKER_ENABLED": "true" if enabled else "false"}
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "cs_agent.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                "8002",
                "--no-access-log",
                "--log-level",
                "warning",
            ],
            cwd=PROJECT_ROOT,
            env=child_env,
            stdout=log,
            stderr=log,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(
                    "verification server exited; inspect private fixed-code log"
                )
            try:
                if httpx.get(base + "/health/ready", timeout=1).status_code == 200:
                    return process
            except httpx.HTTPError:
                pass
            time.sleep(0.1)
        process.terminate()
        process.wait(timeout=5)
        raise RuntimeError("verification server did not become ready")

    try:
        server = start(False)
        with httpx.Client(base_url=base, timeout=10, trust_env=False) as client:
            origin = {"Origin": "http://127.0.0.1:5173"}
            guest = client.post("/api/v1/guest-session", headers=origin)
            assert guest.status_code == 201
            headers = {**origin, "X-CSRF-Token": guest.json()["data"]["csrf_token"]}
            created = client.post("/api/v1/conversations", headers=headers, json={})
            assert created.status_code == 201
            ref = created.json()["data"]["conversation_id"]
            body = {
                "client_message_id": "http-restart-fixed-1",
                "text": "在哪里查看订单？",
            }
            accepted = client.post(
                f"/api/v1/conversations/{ref}/messages", headers=headers, json=body
            )
            assert accepted.status_code == 202
            run_id = accepted.json()["data"]["run_id"]
            assert (
                client.get(f"/api/v1/runs/{run_id}").json()["data"]["status"]
                == "queued"
            )
            server.terminate()
            server.wait(timeout=5)
            server = None
            server = start(True)
            started = time.monotonic()
            while time.monotonic() - started < 15:
                if (
                    client.get(f"/api/v1/runs/{run_id}").json()["data"]["status"]
                    == "completed"
                ):
                    break
                time.sleep(0.1)
            else:
                raise RuntimeError("durable run did not recover after process restart")
            history = client.get(f"/api/v1/conversations/{ref}/messages").json()[
                "data"
            ]["items"]
            assert [item["source_role"] for item in history] == [
                "customer",
                "assistant",
            ]
            assert "登录后进入" in history[-1]["text"]
            duplicate = client.post(
                f"/api/v1/conversations/{ref}/messages", headers=headers, json=body
            )
            assert duplicate.json()["data"] == accepted.json()["data"]
            replay = client.get(f"/api/v1/conversations/{ref}/events?follow=false")
            assert replay.status_code == 200 and replay.headers[
                "content-type"
            ].startswith("text/event-stream")
            ids = [
                int(line[4:])
                for line in replay.text.splitlines()
                if line.startswith("id: ")
            ]
            assert ids == sorted(set(ids)) and "event: run.completed" in replay.text
            resumed = client.get(
                f"/api/v1/conversations/{ref}/events?follow=false",
                headers={"Last-Event-ID": str(ids[-1])},
            )
            assert resumed.status_code == 200 and "id:" not in resumed.text
            summary = {
                "scope": "isolated_test_database",
                "real_http_process_restart": True,
                "durable_ack_recovered": True,
                "messages": len(history),
                "unique_ordered_events": len(ids),
                "idempotent_replay": True,
                "last_event_id_compensation": True,
                "recovery_observation_seconds": round(time.monotonic() - started, 3),
                "real_model_calls": 0,
                "synthetic_model_gateway": False,
                "faq_source": "approved_test_FAQ-SIM-001",
            }
            target = PROJECT_ROOT / "docs/06-acceptance/B2/http-restart.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
            print(json.dumps(summary))
    finally:
        if server is not None and server.poll() is None:
            server.terminate()
            server.wait(timeout=5)
        log.close()


if __name__ == "__main__":
    main()
