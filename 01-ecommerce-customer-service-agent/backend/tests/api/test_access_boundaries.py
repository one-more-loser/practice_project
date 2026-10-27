from datetime import timedelta

import pytest
from sqlalchemy import text


def test_guest_public_and_private_login_required(environment):
    client, *_ = environment
    assert client.get("/api/v1/public/service-info").status_code == 200
    assert client.get("/api/v1/me/shipping-inputs/shipping_sim_a").status_code == 401


def test_customer_retains_public_access_but_cannot_read_b(environment, login_as):
    client, *_ = environment
    login_as("customer_a")
    assert client.get("/api/v1/public/service-info").status_code == 200
    own = client.get("/api/v1/me/shipping-inputs/shipping_sim_a")
    assert own.status_code == 200
    assert own.json()["data"]["phone"] == "13800123456"
    foreign = client.get("/api/v1/me/shipping-inputs/shipping_sim_b")
    absent = client.get("/api/v1/me/shipping-inputs/missing")
    assert foreign.status_code == absent.status_code == 404
    assert foreign.json()["error"] == absent.json()["error"]
    # body/query/header均不能选择另一个顾客身份。
    assert (
        client.get(
            "/api/v1/me/shipping-inputs/shipping_sim_b?subject_id=customer_b",
            headers={"X-User-ID": "customer_b"},
        ).status_code
        == 404
    )


@pytest.mark.parametrize(
    "name,expected",
    [
        ("support_a", 200),
        ("support_b", 404),
        ("supervisor", 200),
        ("shop_admin", 404),
        ("knowledge_admin", 404),
        ("customer_a", 404),
    ],
)
def test_support_http_scope_is_independent_from_field_mask(environment, login_as, name, expected):
    client, *_ = environment
    login_as(name)
    result = client.get("/_test/support/shipping_sim_a")
    assert result.status_code == expected
    if expected == 200:
        assert result.json()["data"] == {
            "ref": "shipping_sim_a",
            "phone_masked": "*******3456",
            "address_masked": "广东省深圳市****",
        }
    # 员工即使有服务访问权，也不能调用本人完整资料面。
    if name != "customer_a":
        assert client.get("/api/v1/me/shipping-inputs/shipping_sim_a").status_code == 404


def test_revoked_assignment_denies_next_http_read(environment, login_as):
    client, _, _, assignment, _ = environment
    login_as("support_a")
    assert client.get("/_test/support/shipping_sim_a").status_code == 200
    assignment["active"] = False
    assert client.get("/_test/support/shipping_sim_a").status_code == 404


def test_shop_role_only_reads_necessary_fulfillment_projection(environment, login_as):
    client, _, _, assignment, _ = environment
    login_as("shop_admin")
    assert client.get("/_test/support/shipping_sim_a").status_code == 404
    result = client.get("/_test/fulfillment/shipping_sim_a")
    assert result.status_code == 200
    assert result.json()["data"]["phone_masked"] == "*******3456"
    assignment["fulfillment_required"] = False
    assert client.get("/_test/fulfillment/shipping_sim_a").status_code == 404


def test_switch_account_revokes_old_cookie_and_self_scope(environment, login_as):
    client, *_ = environment
    login_as("customer_a")
    old_cookie = client.cookies.get("cs_session")
    login_as("customer_b")
    assert client.get("/api/v1/me/shipping-inputs/shipping_sim_b").status_code == 200
    assert client.get("/api/v1/me/shipping-inputs/shipping_sim_a").status_code == 404
    client.cookies.clear()
    client.cookies.set("cs_session", old_cookie)
    assert client.get("/api/v1/session").status_code == 401


def test_logout_requires_csrf_and_revokes_cookie_replay(environment, login_as):
    client, *_ = environment
    headers = login_as("customer_a")
    cookie = client.cookies.get("cs_session")
    assert client.delete("/api/v1/session").status_code == 403
    assert (
        client.delete(
            "/api/v1/session", headers={**headers, "Origin": "https://evil.invalid"}
        ).status_code
        == 403
    )
    assert client.delete("/api/v1/session", headers=headers).status_code == 200
    client.cookies.set("cs_session", cookie)
    assert client.get("/api/v1/session").status_code == 401


@pytest.mark.parametrize(
    "change", ["disabled", "auth_version", "expired", "batch_retired", "role_revoked"]
)
def test_stale_authorization_is_rechecked(environment, login_as, change):
    client, engine, clock, _, _ = environment
    login_as("support_a")
    if change == "expired":
        clock.value += timedelta(hours=9)
    else:
        with engine.begin() as conn:
            statements = {
                "disabled": "UPDATE subject SET account_status='disabled' WHERE subject_id='support_a'",
                "auth_version": "UPDATE subject SET auth_version=2 WHERE subject_id='support_a'",
                "batch_retired": "UPDATE source_batch SET status='retired'",
                "role_revoked": "UPDATE staff_role_grant SET grant_status='revoked',revoked_at=now(),revocation_reason='test' WHERE staff_id='support_a'",
            }
            conn.execute(text(statements[change]))
    assert client.get("/api/v1/session").status_code == 401
    assert client.get("/_test/support/shipping_sim_a").status_code == 401
    with engine.connect() as conn:
        actor = conn.execute(
            text(
                "SELECT actor_id FROM audit_event WHERE action='http.request.rejected' ORDER BY recorded_at DESC LIMIT 1"
            )
        ).scalar_one()
    assert actor == "support_a"


def test_role_switch_does_not_union_privileges_and_is_audited(environment, login_as):
    client, engine, *_ = environment
    headers = login_as("multi_role")
    response = client.patch("/api/v1/session/role", json={"role": "support"}, headers=headers)
    assert response.status_code == 200
    assert (
        client.patch(
            "/api/v1/session/role", json={"role": "supervisor"}, headers=headers
        ).status_code
        == 403
    )
    assert (
        client.patch(
            "/api/v1/session/role", json={"role": "knowledge_admin"}, headers=headers
        ).status_code
        == 200
    )
    assert client.get("/_test/support/shipping_sim_a").status_code == 404
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT summary FROM audit_event WHERE action='session.role.switched' ORDER BY recorded_at DESC LIMIT 1"
            )
        ).scalar_one()
    assert row["from_role"] == "support" and row["to_role"] == "knowledge_admin"


def test_unknown_role_and_identity_in_login_payload_rejected_without_echo(environment):
    client, _, _, _, accounts = environment
    response = client.post(
        "/api/v1/session",
        headers={"Origin": "http://127.0.0.1:5173"},
        json={
            "login_name": "customer_a",
            "password": accounts["customer_a"],
            "subject_id": "customer_b",
        },
    )
    assert response.status_code == 422
    assert accounts["customer_a"] not in response.text
    assert "customer_b" not in response.text


def test_database_commit_failure_never_issues_successful_login_cookie(environment):
    client, engine, _, _, accounts = environment
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE FUNCTION cs_test_commit_failure() RETURNS trigger LANGUAGE plpgsql AS $$BEGIN RAISE EXCEPTION 'synthetic commit failure'; END$$"
            )
        )
        conn.execute(
            text(
                "CREATE CONSTRAINT TRIGGER cs_test_commit_failure AFTER INSERT ON login_session DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION cs_test_commit_failure()"
            )
        )
    try:
        response = client.post(
            "/api/v1/session",
            headers={"Origin": "http://127.0.0.1:5173"},
            json={"login_name": "customer_a", "password": accounts["customer_a"]},
        )
        assert response.status_code == 503
        assert "cs_session" not in response.headers.get("set-cookie", "")
        with engine.connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM login_session")).scalar_one() == 0
    finally:
        with engine.begin() as conn:
            conn.execute(text("DROP TRIGGER cs_test_commit_failure ON login_session"))
            conn.execute(text("DROP FUNCTION cs_test_commit_failure()"))
