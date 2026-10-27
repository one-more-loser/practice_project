from uuid import uuid4


def test_durable_accept_idempotence_history_and_other_customer(environment, login_as):
    client, *_ = environment
    headers = login_as("customer_a")
    created = client.post("/api/v1/conversations", headers=headers, json={})
    assert created.status_code == 201
    ref = created.json()["data"]["conversation_id"]
    body = {"client_message_id": str(uuid4()), "text": "我的订单有什么进度？"}
    first = client.post(f"/api/v1/conversations/{ref}/messages", headers=headers, json=body)
    assert first.status_code == 202
    again = client.post(f"/api/v1/conversations/{ref}/messages", headers=headers, json=body)
    assert again.json()["data"] == first.json()["data"]
    changed = client.post(
        f"/api/v1/conversations/{ref}/messages",
        headers=headers,
        json={**body, "text": "另一个问题"},
    )
    assert changed.status_code == 409
    assert len(client.get(f"/api/v1/conversations/{ref}/messages").json()["data"]["items"]) == 1
    login_as("customer_b")
    assert client.get(f"/api/v1/conversations/{ref}").status_code == 404


def test_guest_public_session_is_server_bound_and_not_merged_on_login(environment, login_as):
    client, *_ = environment
    assert (
        client.post(
            "/api/v1/guest-session", headers={"Origin": "http://127.0.0.1:5173"}
        ).status_code
        == 201
    )
    identity = client.get("/api/v1/guest-session").json()["data"]
    headers = {"Origin": "http://127.0.0.1:5173", "X-CSRF-Token": identity["csrf_token"]}
    ref = client.post("/api/v1/conversations", headers=headers, json={}).json()["data"][
        "conversation_id"
    ]
    response = client.post(
        f"/api/v1/conversations/{ref}/messages",
        headers=headers,
        json={"client_message_id": "guest-1", "text": "订单可以修改地址吗？"},
    )
    assert response.status_code == 202
    login_as("customer_a")
    assert client.get(f"/api/v1/conversations/{ref}").status_code == 404


def test_future_cursor_closed_history_and_private_input(environment, login_as):
    client, *_ = environment
    headers = login_as("customer_a")
    ref = client.post("/api/v1/conversations", headers=headers, json={}).json()["data"][
        "conversation_id"
    ]
    assert (
        client.get(f"/api/v1/conversations/{ref}/events?after_seq=999&follow=false").status_code
        == 409
    )
    result = client.post(
        f"/api/v1/conversations/{ref}/messages",
        headers=headers,
        json={"client_message_id": "pii", "text": "我的收货地址是深圳市南山区测试路88号"},
    )
    assert result.status_code == 422
    assert client.get(f"/api/v1/conversations/{ref}/messages").json()["data"]["items"] == []
    assert (
        client.post(
            f"/api/v1/conversations/{ref}/close", headers=headers, json={"expected_version": 1}
        ).status_code
        == 200
    )
    assert client.get(f"/api/v1/conversations/{ref}/messages").status_code == 200
    assert (
        client.post(
            f"/api/v1/conversations/{ref}/messages",
            headers=headers,
            json={"client_message_id": "closed", "text": "你好"},
        ).status_code
        == 409
    )
