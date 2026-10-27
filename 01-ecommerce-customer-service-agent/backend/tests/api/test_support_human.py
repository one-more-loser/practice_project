"""T015/016独立业务预期；只由父执行者在隔离库串行运行。"""

from datetime import timedelta

from sqlalchemy import func, select


def create_ticket(client, headers, subject="订单问题", category="general", conversation_id=None):
    proposed = client.post(
        "/api/v1/action-proposals",
        headers=headers,
        json={
            "action": "ticket.create",
            "resource_ref": conversation_id or "new",
            "parameters": {"subject": subject, "category": category},
            "expected_version": 1,
        },
    )
    assert proposed.status_code == 200, proposed.text
    ref = proposed.json()["data"]["confirmation_id"]
    submitted = client.post(
        f"/api/v1/confirmations/{ref}/submit",
        headers={**headers, "Idempotency-Key": "support-create-" + ref},
        json={"decision": "confirm"},
    )
    assert submitted.status_code == 200, submitted.text
    return submitted.json()["data"]["business_ref"]


def assign(client, headers, ticket, staff_id="support_a", version=1):
    result = client.post(
        f"/api/v1/staff/tickets/{ticket}/assignments",
        headers=headers,
        json={"staff_id": staff_id, "reason": "按工作队列分派", "expected_version": version},
    )
    assert result.status_code == 200, result.text
    return result.json()["data"]


def test_queue_only_summary_and_owner_scope(environment, login_as):
    client, *_ = environment
    ticket = create_ticket(client, login_as("customer_a"), "请核查订单进度")
    login_as("customer_b")
    assert client.get(f"/api/v1/me/tickets/{ticket}").status_code == 404
    login_as("support_a")
    queue = client.get("/api/v1/staff/queue").json()["data"]["items"]
    item = next(item for item in queue if item["ticket_id"] == ticket)
    assert "owner_id" not in item and "order_id" not in item and "entries" not in item
    assert client.get(f"/api/v1/staff/tickets/{ticket}").status_code == 404


def test_assignment_version_and_internal_note_never_reaches_customer(environment, login_as):
    client, *_ = environment
    ticket = create_ticket(client, login_as("customer_a"))
    supervisor = login_as("supervisor")
    assigned = assign(client, supervisor, ticket)
    assert (
        client.post(
            f"/api/v1/staff/tickets/{ticket}/assignments",
            headers=supervisor,
            json={"staff_id": "support_a", "reason": "旧版本", "expected_version": 1},
        ).status_code
        == 409
    )
    staff = login_as("support_a")
    result = client.post(
        f"/api/v1/staff/tickets/{ticket}/actions",
        headers=staff,
        json={"action": "note", "text": "内部核查记录", "expected_version": assigned["version"]},
    )
    assert result.status_code == 200
    login_as("customer_a")
    assert "内部核查记录" not in client.get(f"/api/v1/me/tickets/{ticket}").text


def test_supervisor_cannot_bypass_assigned_support_review(environment, login_as):
    import pytest

    from cs_agent.core.errors import AppError
    from cs_agent.support.service import require_ticket_scope

    client, _, _, *_ = environment
    ticket = create_ticket(client, login_as("customer_a"))
    login_as("supervisor")
    from starlette.requests import Request

    from cs_agent.identity.service import authenticate

    app = client.app
    with app.state.db.transaction() as session:
        request = Request(
            {
                "type": "http",
                "headers": [(b"cookie", ("cs_session=" + client.cookies["cs_session"]).encode())],
                "app": app,
            }
        )
        request.state.request_id = "test-support-scope"
        ctx, *_ = authenticate(session, request)
        with pytest.raises(AppError):
            require_ticket_scope(session, ctx, ticket, allow_supervisor=False)


def test_complaint_sla_reopen_window_and_history(environment, login_as):
    client, _, clock, *_ = environment
    ticket = create_ticket(client, login_as("customer_a"), "重复投诉请升级", "complaint")
    supervisor = login_as("supervisor")
    detail = client.get(f"/api/v1/staff/tickets/{ticket}").json()["data"]
    assert detail["priority"] == "high"
    clock.value += timedelta(minutes=11)
    assert client.get("/api/v1/staff/queue").json()["data"]["items"][0]["sla_overdue"] is True
    closed = client.post(
        f"/api/v1/staff/tickets/{ticket}/actions",
        headers=supervisor,
        json={"action": "close", "text": "核查结论已告知", "expected_version": detail["version"]},
    ).json()["data"]
    customer = login_as("customer_a")
    clock.value += timedelta(days=7, seconds=1)
    customer = login_as("customer_a")
    reopened = client.post(
        f"/api/v1/me/tickets/{ticket}/reopen",
        headers=customer,
        json={"reason": "同一诉求仍未解决", "expected_version": closed["version"]},
    )
    assert reopened.status_code == 200
    assert reopened.json()["data"]["linked_from_ticket_id"] == ticket
    assert reopened.json()["data"]["ticket_id"] != ticket


def test_queued_message_has_no_agent_task_and_cancel_does_not_close_ticket(environment, login_as):
    from cs_agent.db.runtime_tables import tasks

    client, engine, *_ = environment
    headers = login_as("customer_a")
    ref = client.post("/api/v1/conversations", headers=headers, json={}).json()["data"][
        "conversation_id"
    ]
    queued = client.post(
        f"/api/v1/conversations/{ref}/human-requests",
        headers=headers,
        json={"reason": "需要人工协助", "expected_epoch": 1, "expected_version": 1},
    )
    assert queued.status_code == 200
    q = queued.json()["data"]
    assert q["mode"] == "queued" and q["connected"] is False
    body = {"client_message_id": "queued-message-1", "text": "我补充一下情况"}
    received = client.post(f"/api/v1/conversations/{ref}/messages", headers=headers, json=body)
    assert received.status_code == 202 and received.json()["data"]["run_id"] is None
    assert (
        client.post(f"/api/v1/conversations/{ref}/messages", headers=headers, json=body).json()[
            "data"
        ]
        == received.json()["data"]
    )
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(tasks)) == 0
    current = client.get(f"/api/v1/conversations/{ref}").json()["data"]
    cancelled = client.post(
        f"/api/v1/conversations/{ref}/cancel-human-request",
        headers=headers,
        json={"expected_epoch": current["mode_epoch"], "expected_version": current["version"]},
    )
    assert cancelled.json()["data"]["mode"] == "agent"
    assert client.get(f"/api/v1/me/tickets/{q['ticket_id']}").json()["data"]["status"] == "open"


def test_takeover_end_keeps_conversation_open_and_stale_epoch_rejected(environment, login_as):
    client, *_ = environment
    customer = login_as("customer_a")
    ref = client.post("/api/v1/conversations", headers=customer, json={}).json()["data"][
        "conversation_id"
    ]
    queued = client.post(
        f"/api/v1/conversations/{ref}/human-requests",
        headers=customer,
        json={"reason": "需要协助", "expected_epoch": 1, "expected_version": 1},
    ).json()["data"]
    assigned = assign(client, login_as("supervisor"), queued["ticket_id"])
    staff = login_as("support_a")
    taken = client.post(
        f"/api/v1/staff/conversations/{ref}/takeover",
        headers=staff,
        json={
            "assignment_id": assigned["assignment_id"],
            "assignment_version": 1,
            "expected_epoch": queued["mode_epoch"],
            "expected_version": queued["version"],
        },
    )
    assert taken.status_code == 200, taken.text
    h = taken.json()["data"]
    ended = client.post(
        f"/api/v1/staff/conversations/{ref}/end-service",
        headers=staff,
        json={
            "assignment_id": assigned["assignment_id"],
            "assignment_version": 1,
            "expected_epoch": h["mode_epoch"],
            "expected_version": h["version"],
            "reason": "已处理",
        },
    )
    assert ended.status_code == 200 and ended.json()["data"]["mode"] == "agent"
    login_as("customer_a")
    assert client.get(f"/api/v1/conversations/{ref}").json()["data"]["status"] == "open"


def test_websocket_requires_origin_and_first_frame_csrf(environment, login_as):
    import pytest
    from starlette.websockets import WebSocketDisconnect

    client, *_ = environment
    login_as("customer_a")
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(
            "/api/v1/realtime", headers={"origin": "https://untrusted.invalid"}
        ):
            pass
    with client.websocket_connect(
        "/api/v1/realtime", headers={"origin": "http://127.0.0.1:5173"}
    ) as ws:
        ws.send_json({"type": "hello", "csrf_token": "invalid", "subscriptions": []})
        assert ws.receive_json()["type"] == "access.revoked"


def test_websocket_ack_replay_and_assignment_revocation(environment, login_as):
    from cs_agent.db.models import AuditEvent

    client, engine, *_ = environment
    customer = login_as("customer_a")
    ref = client.post("/api/v1/conversations", headers=customer, json={}).json()["data"][
        "conversation_id"
    ]
    q = client.post(
        f"/api/v1/conversations/{ref}/human-requests",
        headers=customer,
        json={"reason": "需要协助", "expected_epoch": 1, "expected_version": 1},
    ).json()["data"]
    a = assign(client, login_as("supervisor"), q["ticket_id"])
    staff = login_as("support_a")
    h = client.post(
        f"/api/v1/staff/conversations/{ref}/takeover",
        headers=staff,
        json={
            "assignment_id": a["assignment_id"],
            "assignment_version": 1,
            "expected_epoch": q["mode_epoch"],
            "expected_version": q["version"],
        },
    ).json()["data"]
    with client.websocket_connect(
        "/api/v1/realtime", headers={"origin": "http://127.0.0.1:5173"}
    ) as ws:
        ws.send_json(
            {
                "type": "hello",
                "csrf_token": staff["X-CSRF-Token"],
                "subscriptions": [
                    {"conversation_id": ref, "after_seq": 0, "expected_epoch": h["mode_epoch"]}
                ],
            }
        )
        assert ws.receive_json()["type"] == "ready"
        frame = {
            "type": "staff.reply",
            "conversation_id": ref,
            "assignment_version": 1,
            "expected_epoch": h["mode_epoch"],
            "client_message_id": "ws-replay-001",
            "text": "已收到您的咨询",
        }

        def ack():
            for _ in range(12):
                response = ws.receive_json()
                if response["type"] == "ping":
                    ws.send_json({"type": "pong"})
                if response["type"] == "ack":
                    return response
            raise AssertionError("missing message ACK")

        ws.send_json(frame)
        first = ack()
        ws.send_json(frame)
        second = ack()
        assert first["message_id"] == second["message_id"]
        supervisor = login_as("supervisor")
        ticket = client.get(f"/api/v1/staff/tickets/{q['ticket_id']}").json()["data"]
        assign(client, supervisor, q["ticket_id"], "support_b", ticket["version"])
        for _ in range(12):
            response = ws.receive_json()
            if response["type"] == "access.revoked":
                break
        else:
            raise AssertionError("old assignment still subscribed")
    login_as("customer_a")
    history = client.get(f"/api/v1/conversations/{ref}/messages").json()["data"]["items"]
    assert len([m for m in history if m["source_role"] == "support"]) == 1
    with engine.connect() as connection:
        actors = connection.scalars(
            select(AuditEvent.actor_id).where(
                AuditEvent.action == "human.message.sent",
                AuditEvent.resource_ref == first["message_id"],
            )
        ).all()
        assert actors == ["support_a"]  # 重放不重复，改派后仍能归因到原发送人。
