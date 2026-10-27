"""真实PG/HTTP业务回归，审核是显式协议替身，不能当真实模型验收。

此文件只由父执行者串行运行；environment会清理已隔离测试库。
"""

import json
from contextlib import contextmanager
from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from cs_agent.actions.repository import fingerprint
from cs_agent.cancellation import CancellationWorker
from cs_agent.cancellation.review import CHECKS, CONTRACT, RULES
from cs_agent.db.business_tables import (
    inventory,
    operations,
    orders,
    payments,
    stock_movements,
    wallet_entries,
)
from cs_agent.db.cancellation_tables import refunds, reviews
from cs_agent.models.gateway import ModelGateway, ServiceConfig
from tests.api.test_checkout import create_order, key, propose_and_submit


def queued(environment, login_as):
    client, engine, *_ = environment
    customer = login_as("customer_a")
    topup = client.post(
        "/api/v1/me/topups",
        headers={**customer, **key()},
        json={"currency": "CNY", "amount_minor": "10000"},
    )
    assert topup.status_code == 200
    order_ref = create_order(client, customer)
    assert propose_and_submit(client, customer, "order.pay", order_ref, 1).status_code == 200
    proposed = client.post(
        "/api/v1/action-proposals",
        headers=customer,
        json={
            "action": "order.cancel",
            "resource_ref": order_ref,
            "expected_version": 2,
            "parameters": {},
        },
    )
    assert proposed.status_code == 200, proposed.text
    confirmation = proposed.json()["data"]["confirmation_id"]
    stable = key()
    response = client.post(
        f"/api/v1/confirmations/{confirmation}/submit",
        headers={**customer, **stable},
        json={"decision": "confirm"},
    )
    assert response.status_code == 202, response.text
    with engine.connect() as connection:
        stock = connection.execute(
            select(inventory.c.stock_qty).where(inventory.c.sku_id == "sku_cup_ivory")
        ).scalar_one()
    return customer, order_ref, confirmation, stable, response.json()["data"], stock


def gateway(decision="approved", *, callback=None, invalid=False):
    """测试源码显式注入的chat-completions协议替身，不提供业务写工具。"""

    def respond(request):
        payload = json.loads(request.content)
        assert "tools" not in payload
        facts = json.loads(payload["messages"][-1]["content"])
        if callback:
            callback()
        data = {
            "contract_version": CONTRACT,
            "rules_revision": RULES,
            "facts_digest": fingerprint(facts),
            "decision": decision,
            "checks": [
                {"code": code, "result": "passed", "fact_refs": [facts["order_ref"]]}
                for code in sorted(CHECKS)
            ],
            "rule_refs": ["BR-CS-039", "BR-CS-012", "BR-CS-013"],
            "reason_codes": ["eligible" if decision == "approved" else "policy_uncertain"],
        }
        if decision != "approved":
            data["checks"][0]["result"] = "unknown" if decision == "needs_facts" else "failed"
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "{" if invalid else json.dumps(data)}}]}
        )

    config = ServiceConfig(
        base_url="http://explicit-test-double.invalid",
        model="explicit-cancellation-review-test-double",
        service_revision="test-only-v1",
        protocol="chat-completions",
    )
    return ModelGateway({"chat": config}, transport=httpx.MockTransport(respond))


def worker(environment, review_gateway=None, database=None):
    client, _, clock, *_ = environment
    return CancellationWorker(
        database or client.app.state.db,
        client.app.state.settings,
        clock,
        gateway=review_gateway if review_gateway is not None else ModelGateway(),
    )


def read_review(client, data):
    response = client.get(f"/api/v1/me/cancellation-reviews/{data['review_id']}")
    assert response.status_code == 200, response.text
    return response.json()["data"]


def test_submission_only_dispatches_and_one_confirmation_has_one_operation(environment, login_as):
    client, engine, *_ = environment
    customer, order_ref, confirmation, stable, data, _ = queued(environment, login_as)
    assert data["status"] == "accepted" and data["business_effect"] == "none"
    assert read_review(client, data)["status"] == "queued"
    assert client.get(f"/api/v1/me/orders/{order_ref}").json()["data"]["trade_status"] == "paid"
    assert client.get("/api/v1/me/wallet").json()["data"]["balance_minor"] == "6000"
    for headers in (stable, key()):
        replay = client.post(
            f"/api/v1/confirmations/{confirmation}/submit",
            headers={**customer, **headers},
            json={"decision": "confirm"},
        )
        assert replay.status_code == 202 and replay.json()["data"] == data
    conflict = client.post(
        f"/api/v1/confirmations/{confirmation}/submit",
        headers={**customer, **stable},
        json={"decision": "cancel"},
    )
    assert conflict.status_code == 409
    with engine.connect() as connection:
        assert (
            connection.execute(
                select(func.count())
                .select_from(reviews)
                .where(reviews.c.confirmation_id == confirmation)
            ).scalar_one()
            == 1
        )
        assert connection.execute(select(func.count()).select_from(refunds)).scalar_one() == 0
    login_as("customer_b")
    assert client.get(f"/api/v1/me/cancellation-reviews/{data['review_id']}").status_code == 404
    login_as("support_a")
    assert client.get(f"/api/v1/me/cancellation-reviews/{data['review_id']}").status_code == 403


def test_no_model_is_unavailable_and_never_approves_or_refunds(environment, login_as):
    client, engine, *_ = environment
    _, order_ref, _, _, data, _ = queued(environment, login_as)
    assert worker(environment).process_once()
    review = read_review(client, data)
    assert review["status"] == "unavailable" and review["decision"] is None
    assert review["error_code"] == "model_unconfigured"
    assert review["model_identifier"] is None and review["refund_id"] is None
    assert client.get(f"/api/v1/me/orders/{order_ref}").json()["data"]["trade_status"] == "paid"
    assert client.get("/api/v1/me/wallet").json()["data"]["balance_minor"] == "6000"
    with engine.connect() as connection:
        operation = (
            connection.execute(
                select(operations).where(operations.c.operation_id == data["operation_id"])
            )
            .mappings()
            .one()
        )
        assert operation["status"] == "rejected" and operation["http_status"] == 503
        assert connection.execute(select(func.count()).select_from(refunds)).scalar_one() == 0


def test_explicit_review_refunds_original_payment_and_replay_after_day_is_once(
    environment, login_as
):
    client, engine, clock, *_ = environment
    customer, order_ref, confirmation, stable, data, stock = queued(environment, login_as)
    runner = worker(environment, gateway())
    assert runner.process_once() and not runner.process_once()
    review = read_review(client, data)
    assert review["decision"] == "approved" and review["status"] == "succeeded"
    assert review["model_identifier"] == "explicit-cancellation-review-test-double"
    detail = client.get(f"/api/v1/me/orders/{order_ref}").json()["data"]
    assert detail["trade_status"] == "cancelled_refunded"
    assert detail["paid_minor"] == detail["refunded_minor"] == "4000"
    assert client.get("/api/v1/me/wallet").json()["data"]["balance_minor"] == "10000"
    # 模拟业务响应丢失：客户端只按原确认/键查结果；24小时后也不解除业务唯一性。
    clock.value += timedelta(days=2)
    customer = login_as("customer_a")
    for headers in (stable, key()):
        replay = client.post(
            f"/api/v1/confirmations/{confirmation}/submit",
            headers={**customer, **headers},
            json={"decision": "confirm"},
        )
        assert replay.status_code == 200, replay.text
        assert replay.json()["data"]["operation_id"] == data["operation_id"]
    with engine.connect() as connection:
        refund = (
            connection.execute(
                select(refunds).where(refunds.c.operation_id == data["operation_id"])
            )
            .mappings()
            .one()
        )
        payment = (
            connection.execute(select(payments).where(payments.c.order_id == order_ref))
            .mappings()
            .one()
        )
        assert refund["payment_id"] == payment["payment_id"]
        assert refund["payment_entry_id"] == payment["entry_id"]
        assert refund["amount_minor"] == payment["amount_minor"] == 4000
        assert (
            connection.execute(
                select(func.count())
                .select_from(wallet_entries)
                .where(
                    wallet_entries.c.operation_id == data["operation_id"],
                    wallet_entries.c.kind == "refund",
                )
            ).scalar_one()
            == 1
        )
        assert (
            connection.execute(
                select(func.count())
                .select_from(stock_movements)
                .where(
                    stock_movements.c.operation_id == data["operation_id"],
                    stock_movements.c.kind == "cancellation_restock",
                )
            ).scalar_one()
            == 1
        )
        assert (
            connection.execute(
                select(inventory.c.stock_qty).where(inventory.c.sku_id == "sku_cup_ivory")
            ).scalar_one()
            == stock + 1
        )


@pytest.mark.parametrize("decision", ["rejected", "needs_facts"])
def test_agent_rejection_and_needs_facts_preserve_business(environment, login_as, decision):
    client, engine, *_ = environment
    _, order_ref, _, _, data, stock = queued(environment, login_as)
    assert worker(environment, gateway(decision)).process_once()
    review = read_review(client, data)
    assert review["status"] == review["decision"] == decision
    assert review["basis_summary"] and review["model_revision"] == "test-only-v1"
    with engine.connect() as connection:
        assert (
            connection.execute(
                select(orders.c.trade_status).where(orders.c.order_id == order_ref)
            ).scalar_one()
            == "paid"
        )
        assert (
            connection.execute(
                select(inventory.c.stock_qty).where(inventory.c.sku_id == "sku_cup_ivory")
            ).scalar_one()
            == stock
        )
        assert connection.execute(select(func.count()).select_from(refunds)).scalar_one() == 0


def test_partial_json_never_counts_as_approval(environment, login_as):
    client, *_ = environment
    _, _, _, _, data, _ = queued(environment, login_as)
    assert worker(environment, gateway(invalid=True)).process_once()
    review = read_review(client, data)
    assert review["status"] == "unavailable" and review["decision"] is None
    assert review["error_code"] == "cancellation_review_protocol_error"
    assert client.get("/api/v1/me/wallet").json()["data"]["balance_minor"] == "6000"


def shop_session(environment):
    client, _, _, _, accounts = environment
    shop = TestClient(client.app)
    response = shop.post(
        "/api/v1/session",
        json={"login_name": "shop_admin", "password": accounts["shop_admin"]},
        headers={"Origin": "http://127.0.0.1:5173"},
    )
    assert response.status_code == 200
    headers = {
        "Origin": "http://127.0.0.1:5173",
        "X-CSRF-Token": response.json()["data"]["csrf_token"],
    }
    return shop, headers


def ship(shop, headers, order_ref, version):
    detail = shop.get(f"/api/v1/shop/orders/{order_ref}").json()["data"]
    return shop.post(
        f"/api/v1/shop/orders/{order_ref}/shipments",
        headers={**headers, **key()},
        json={
            "expected_version": version,
            "quantities": {item["order_item_id"]: item["ordered_qty"] for item in detail["items"]},
        },
    )


def test_shipping_during_model_call_wins_and_old_approval_cannot_refund(environment, login_as):
    client, engine, *_ = environment
    _, order_ref, _, _, data, _ = queued(environment, login_as)
    shop, shop_headers = shop_session(environment)

    def shipping_wins():
        # 若模型等待仍持订单锁，该HTTP将超时/失败；必须在模型外呼期间成功发货。
        result = ship(shop, shop_headers, order_ref, 2)
        assert result.status_code == 200, result.text

    try:
        assert worker(environment, gateway(callback=shipping_wins)).process_once()
    finally:
        shop.close()
    review = read_review(client, data)
    assert review["decision"] == "approved" and review["status"] == "execution_rejected"
    assert review["error_code"] in {"version_conflict", "state_conflict"}
    assert (
        client.get(f"/api/v1/me/orders/{order_ref}").json()["data"]["fulfillment_status"]
        == "in_transit"
    )
    with engine.connect() as connection:
        assert connection.execute(select(func.count()).select_from(refunds)).scalar_one() == 0


def test_cancellation_wins_and_later_shipping_is_rejected(environment, login_as):
    _, _, *_ = environment
    _, order_ref, _, _, _, _ = queued(environment, login_as)
    assert worker(environment, gateway()).process_once()
    shop, headers = shop_session(environment)
    try:
        assert ship(shop, headers, order_ref, 3).status_code == 409
    finally:
        shop.close()


def test_expired_lease_old_worker_cannot_overwrite_new_worker(environment, login_as):
    client, engine, clock, *_ = environment
    _, _, _, _, data, _ = queued(environment, login_as)

    def new_worker_wins():
        clock.value += timedelta(seconds=91)
        assert worker(environment, gateway()).process_once()

    assert worker(environment, gateway(callback=new_worker_wins)).process_once()
    assert read_review(client, data)["status"] == "succeeded"
    with engine.connect() as connection:
        review = (
            connection.execute(select(reviews).where(reviews.c.review_id == data["review_id"]))
            .mappings()
            .one()
        )
        assert review["fence"] == review["attempts"] == 2
        assert connection.execute(select(func.count()).select_from(refunds)).scalar_one() == 1


@pytest.mark.parametrize("lost_at", ["before_commit", "after_commit"])
def test_unknown_commit_outcome_is_reconciled_without_second_refund(environment, login_as, lost_at):
    client, engine, clock, *_ = environment
    _, _, _, _, data, _ = queued(environment, login_as)

    class UncertainDatabase:
        def __init__(self):
            self.calls = 0

        @contextmanager
        def transaction(self):
            self.calls += 1
            fail = self.calls == 3
            with client.app.state.db.transaction() as session:
                yield session
                if fail and lost_at == "before_commit":
                    raise OSError("explicit test-only uncertain database result")
            if fail and lost_at == "after_commit":
                raise OSError("explicit test-only lost commit acknowledgement")

    runner = worker(environment, gateway(), UncertainDatabase())
    assert runner.process_once()
    if lost_at == "before_commit":
        assert read_review(client, data)["status"] == "running"
    else:
        assert read_review(client, data)["status"] == "succeeded"
    clock.value += timedelta(seconds=91)
    assert worker(environment, gateway()).process_once() is (lost_at == "before_commit")
    assert read_review(client, data)["status"] == "succeeded"
    with engine.connect() as connection:
        assert (
            connection.execute(
                select(func.count())
                .select_from(refunds)
                .where(refunds.c.operation_id == data["operation_id"])
            ).scalar_one()
            == 1
        )


def test_declining_confirmation_and_expiry_never_queue_or_refund(environment, login_as):
    client, engine, clock, *_ = environment
    customer, order_ref, _, _, _, _ = queued(environment, login_as)
    proposed = client.post(
        "/api/v1/action-proposals",
        headers=customer,
        json={
            "action": "order.cancel",
            "resource_ref": order_ref,
            "expected_version": 2,
            "parameters": {},
        },
    )
    assert proposed.status_code == 200
    confirmation = proposed.json()["data"]["confirmation_id"]
    decline = client.post(
        f"/api/v1/confirmations/{confirmation}/submit",
        headers={**customer, **key()},
        json={"decision": "cancel"},
    )
    assert decline.status_code == 200 and decline.json()["data"]["business_effect"] == "none"
    proposed = client.post(
        "/api/v1/action-proposals",
        headers=customer,
        json={
            "action": "order.cancel",
            "resource_ref": order_ref,
            "expected_version": 2,
            "parameters": {},
        },
    )
    confirmation = proposed.json()["data"]["confirmation_id"]
    clock.value += timedelta(minutes=5)
    expired = client.post(
        f"/api/v1/confirmations/{confirmation}/submit",
        headers={**customer, **key()},
        json={"decision": "confirm"},
    )
    assert expired.status_code == 409
    with engine.connect() as connection:
        assert connection.execute(select(func.count()).select_from(reviews)).scalar_one() == 1
        assert connection.execute(select(func.count()).select_from(refunds)).scalar_one() == 0
