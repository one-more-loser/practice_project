"""隔离真实PG+HTTP业务验收，不调用模型。"""

from datetime import timedelta

from sqlalchemy import func, select, update

from cs_agent.db.aftersales_tables import refunds
from cs_agent.db.business_tables import inventory, order_items, orders, receipts, wallet_entries
from tests.api.test_checkout import create_order, key, propose_and_submit


def shipped(environment, login_as, received=True, quantity=3, money_sample=False):
    client, engine, *_ = environment
    customer = login_as("customer_a")
    assert (
        client.post(
            "/api/v1/me/topups",
            headers={**customer, **key()},
            json={"currency": "CNY", "amount_minor": "50000"},
        ).status_code
        == 200
    )
    ref = create_order(client, customer, [{"sku_id": "sku_cup_ivory", "qty": quantity}])
    if money_sample:
        # 独立验收样本：商品12000-折扣2000+原运费1000，原实付11000。
        with engine.begin() as connection:
            policy = connection.scalar(
                select(orders.c.policy_snapshot).where(orders.c.order_id == ref)
            )
            policy = {
                **policy,
                "freight_minor": "1000",
                "quality_original_freight_refund_minor": "1000",
                "wrong_item_original_freight_refund_minor": "1000",
                "transport_original_freight_refund_minor": "1000",
            }
            connection.execute(
                update(orders)
                .where(orders.c.order_id == ref)
                .values(
                    discount_minor=2000,
                    freight_minor=1000,
                    payable_minor=11000,
                    policy_snapshot=policy,
                )
            )
            connection.execute(
                update(order_items)
                .where(order_items.c.order_id == ref)
                .values(
                    discount_minor=2000,
                    allocated_minor=10000,
                    unit_allocations={"base_minor": "3333", "tail_minor": "3334", "quantity": 3},
                )
            )
    assert propose_and_submit(client, customer, "order.pay", ref, 1).status_code == 200
    shop = login_as("shop_admin")
    order = client.get(f"/api/v1/shop/orders/{ref}").json()["data"]
    result = client.post(
        f"/api/v1/shop/orders/{ref}/shipments",
        headers={**shop, **key()},
        json={
            "quantities": {line["order_item_id"]: line["ordered_qty"] for line in order["items"]},
            "expected_version": order["version"],
        },
    )
    assert result.status_code == 200, result.text
    customer = login_as("customer_a")
    if received:
        assert (
            propose_and_submit(client, customer, "order.receipt.confirm", ref, 3).status_code == 200
        )
    return ref


def apply(client, customer, ref, quantity=1):
    order = client.get(f"/api/v1/me/orders/{ref}").json()["data"]
    response = propose_and_submit(
        client,
        customer,
        "aftersale.apply",
        ref,
        order["version"],
        {
            "quantities": {order["items"][0]["order_item_id"]: quantity},
            "reason": "ordinary",
            "description": "模拟普通退货申请",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


def assigned(client, login_as, ticket):
    supervisor = login_as("supervisor")
    version = client.get(f"/api/v1/staff/tickets/{ticket}").json()["data"]["version"]
    response = client.post(
        f"/api/v1/staff/tickets/{ticket}/assignments",
        headers=supervisor,
        json={"staff_id": "support_a", "reason": "分派售后核验", "expected_version": version},
    )
    assert response.status_code == 200, response.text
    return login_as("support_a")


def review(client, headers, ref, decision="approve", version=1):
    return client.post(
        f"/api/v1/aftersales/{ref}/reviews",
        headers={**headers, **key()},
        json={
            "decision": decision,
            "basis": "核对订单政策和顾客申请",
            "evidence_refs": ["policy:REQ-CS-v0.2"],
            "expected_version": version,
        },
    )


def test_partial_return_no_early_credit_and_one_atomic_refund(environment, login_as):
    client, engine, *_ = environment
    order = shipped(environment, login_as)
    customer = login_as("customer_a")
    request = apply(client, customer, order)
    claim = request["claim_id"]
    assert client.get("/api/v1/me/wallet").json()["data"]["balance_minor"] == "38000"
    login_as("customer_b")
    assert client.get(f"/api/v1/aftersales/{claim}").status_code == 404
    support_b = login_as("support_b")
    assert client.get("/api/v1/aftersales").json()["data"]["items"] == []
    assert review(client, support_b, claim).status_code == 404
    staff = assigned(client, login_as, request["ticket_id"])
    assert client.get("/api/v1/aftersales").json()["data"]["items"][0]["claim_id"] == claim
    related = client.get(f"/api/v1/staff/tickets/{request['ticket_id']}").json()["data"][
        "related_order"
    ]
    assert related["shipping"]["phone_masked"] == "*******3456"
    assert "合成测试路" not in str(related)
    approved = review(client, staff, claim)
    assert approved.status_code == 200, approved.text
    customer = login_as("customer_a")
    assert client.get("/api/v1/me/wallet").json()["data"]["balance_minor"] == "38000"
    sent = client.post(
        f"/api/v1/aftersales/{claim}/return-shipment",
        headers={**customer, **key()},
        json={"carrier": "模拟快递", "tracking_no": "RETURN-001", "expected_version": 2},
    )
    assert sent.status_code == 200, sent.text
    line = client.get(f"/api/v1/aftersales/{claim}").json()["data"]["items"][0]["order_item_id"]
    assert client.get("/api/v1/me/wallet").json()["data"]["balance_minor"] == "38000"
    shop = login_as("shop_admin")
    stable = key()
    body = {
        "lines": {line: {"received_qty": 1, "saleable_qty": 1}},
        "evidence_refs": ["inspection:received-001"],
        "expected_version": 3,
    }
    result = client.post(
        f"/api/v1/refund-claims/{claim}/inspections", headers={**shop, **stable}, json=body
    )
    assert result.status_code == 200, result.text
    assert result.json()["data"]["goods_minor"] == "4000"
    replay = client.post(
        f"/api/v1/refund-claims/{claim}/inspections", headers={**shop, **stable}, json=body
    )
    assert replay.status_code == 200 and replay.json()["data"] == result.json()["data"]
    customer = login_as("customer_a")
    assert client.get("/api/v1/me/wallet").json()["data"]["balance_minor"] == "42000"
    remaining = client.get(f"/api/v1/me/orders/{order}").json()["data"]
    assert remaining["items"][0]["refunded_qty"] == 1
    # 仍有剩余数量可退；整单收货不会变成分批收货。
    second = apply(client, customer, order, 2)
    assert second["goods_minor"] == "8000"
    with engine.connect() as conn:
        assert conn.scalar(select(func.count()).select_from(refunds)) == 1
        assert (
            conn.scalar(
                select(func.count())
                .select_from(wallet_entries)
                .where(wallet_entries.c.kind == "refund")
            )
            == 1
        )


def test_transport_lost_needs_customer_confirm_assignment_and_liability_no_restock(
    environment, login_as
):
    client, engine, *_ = environment
    ref = shipped(environment, login_as, received=False, quantity=1)
    with engine.connect() as conn:
        stock = conn.scalar(
            select(inventory.c.stock_qty).where(inventory.c.sku_id == "sku_cup_ivory")
        )
    customer = login_as("customer_a")
    order = client.get(f"/api/v1/me/orders/{ref}").json()["data"]
    assert not order["allowed_actions"]["aftersale"]
    proposal = client.post(
        "/api/v1/action-proposals",
        headers=customer,
        json={
            "action": "aftersale.apply",
            "resource_ref": ref,
            "expected_version": order["version"],
            "parameters": {
                "reason": "ordinary",
                "description": "未收到货",
                "quantities": {order["items"][0]["order_item_id"]: 1},
            },
        },
    )
    assert proposal.status_code == 409
    created = propose_and_submit(
        client,
        customer,
        "transport.incident.create",
        ref,
        order["version"],
        {"kind": "lost", "description": "运输中未收到货"},
    )
    assert created.status_code == 200, created.text
    incident = created.json()["data"]["incident_id"]
    requested = propose_and_submit(
        client, customer, "transport.refund.request", incident, 1, {"description": "申请核验后退款"}
    )
    assert requested.status_code == 200, requested.text
    current = client.get(f"/api/v1/me/orders/{ref}").json()["data"]
    assert (
        propose_and_submit(
            client, customer, "order.receipt.confirm", ref, current["version"]
        ).status_code
        == 409
    )
    staff = assigned(client, login_as, created.json()["data"]["ticket_id"])

    def action(decision, version):
        return client.post(
            f"/api/v1/transport-incidents/{incident}/reviews",
            headers={**staff, **key()},
            json={
                "decision": decision,
                "basis": "模拟运输责任核验",
                "evidence_refs": ["shipment:carrier-proof-001"],
                "expected_version": version,
            },
        )

    approved = action("approve", 2)
    assert approved.status_code == 200, approved.text
    verified = action("loss_verified", 3)
    assert verified.status_code == 200, verified.text
    login_as("customer_a")
    assert client.get("/api/v1/me/wallet").json()["data"]["balance_minor"] == "50000"
    with engine.connect() as conn:
        assert conn.scalar(select(func.count()).select_from(receipts)) == 0
        assert (
            conn.scalar(select(inventory.c.stock_qty).where(inventory.c.sku_id == "sku_cup_ivory"))
            == stock
        )


def test_expired_return_requires_supervisor_not_assigned_support(environment, login_as):
    client, _, clock, *_ = environment
    order = shipped(environment, login_as)
    clock.value += timedelta(hours=168, microseconds=1)
    request = apply(client, login_as("customer_a"), order)
    assert request["status_name"] == "needs_dispute"
    staff = assigned(client, login_as, request["ticket_id"])
    assert review(client, staff, request["claim_id"]).status_code == 403
    supervisor = login_as("supervisor")
    result = review(client, supervisor, request["claim_id"])
    assert result.status_code == 200, result.text


def test_original_unit_tail_allocation_keeps_freight_and_never_over_refunds(environment, login_as):
    client, engine, *_ = environment
    ref = shipped(environment, login_as, money_sample=True)
    for quantity, expected in [(1, "3333"), (2, "6667")]:
        customer = login_as("customer_a")
        data = apply(client, customer, ref, quantity)
        assert data["goods_minor"] == expected and data["original_freight_minor"] == "0"
        claim = data["claim_id"]
        staff = assigned(client, login_as, data["ticket_id"])
        assert review(client, staff, claim).status_code == 200
        customer = login_as("customer_a")
        assert (
            client.post(
                f"/api/v1/aftersales/{claim}/return-shipment",
                headers={**customer, **key()},
                json={
                    "carrier": "模拟退货快递",
                    "tracking_no": "TAIL-RETURN-" + str(quantity),
                    "expected_version": 2,
                },
            ).status_code
            == 200
        )
        item = client.get(f"/api/v1/aftersales/{claim}").json()["data"]["items"][0]["order_item_id"]
        shop = login_as("shop_admin")
        result = client.post(
            f"/api/v1/refund-claims/{claim}/inspections",
            headers={**shop, **key()},
            json={
                "lines": {item: {"received_qty": quantity, "saleable_qty": quantity}},
                "evidence_refs": ["inspection:tail-sample"],
                "expected_version": 3,
            },
        )
        assert result.status_code == 200, result.text
        assert result.json()["data"]["goods_minor"] == expected
        assert result.json()["data"]["original_paid_fully_refunded"] is False
    login_as("customer_a")
    order = client.get(f"/api/v1/me/orders/{ref}").json()["data"]
    assert order["paid_minor"] == "11000" and order["refunded_minor"] == "10000"
    assert order["trade_status"] == "completed" and order["items"][0]["refunded_qty"] == 3
    assert client.get("/api/v1/me/wallet").json()["data"]["balance_minor"] == "49000"
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(refunds)) == 2
