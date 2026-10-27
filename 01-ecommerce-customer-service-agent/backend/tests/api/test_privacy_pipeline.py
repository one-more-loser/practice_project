import json

from sqlalchemy import text


def test_request_logs_audit_and_session_storage_exclude_raw_secrets(environment, login_as, caplog):
    client, engine, _, _, accounts = environment
    with caplog.at_level("INFO", logger="cs_agent.requests"):
        headers = login_as("customer_a")
        cookie = client.cookies.get("cs_session")
        assert client.get("/api/v1/me/shipping-inputs/shipping_sim_a").status_code == 200
        response = client.patch(
            "/api/v1/session/role",
            headers=headers,
            json={"role": "13800123456", "address": "合成测试路88号"},
        )
        assert response.status_code == 422
        assert client.get("/api/v1/session").headers["cache-control"] == "no-store"
    with engine.connect() as conn:
        audit = json.dumps(
            [dict(row) for row in conn.execute(text("SELECT * FROM audit_event")).mappings()],
            default=str,
            ensure_ascii=False,
        )
        stored = conn.execute(
            text("SELECT token_digest,csrf_digest FROM login_session WHERE subject_id='customer_a'")
        ).one()
        assert stored.token_digest != cookie
        assert stored.csrf_digest != headers["X-CSRF-Token"]
    for forbidden in [
        cookie,
        headers["X-CSRF-Token"],
        accounts["customer_a"],
        "13800123456",
        "合成测试路88号",
    ]:
        assert forbidden not in caplog.text
        assert forbidden not in audit
        assert forbidden not in response.text


def test_login_cookie_is_http_only_and_no_reveal_route(environment, login_as):
    client, *_ = environment
    login_as("customer_a")
    response = client.post(
        "/api/v1/session",
        headers={"Origin": "https://evil.invalid"},
        json={"login_name": "customer_a", "password": "wrong"},
    )
    assert response.status_code == 403
    assert client.get("/api/v1/staff/shipping-inputs/shipping_sim_a/reveal").status_code == 404
