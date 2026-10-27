from datetime import timedelta

from tests.api.test_checkout import create_order, key, propose_and_submit


def test_fulfillment_masks_shop_and_preserves_one_address_change(environment, login_as):
    client, _, _, _, _ = environment
    customer = login_as("customer_a")
    client.post(
        "/api/v1/me/topups",
        headers={**customer, **key()},
        json={"currency": "CNY", "amount_minor": "10000"},
    )
    order_id = create_order(client, customer)
    assert propose_and_submit(client, customer, "order.pay", order_id, 1).status_code == 200
    # 专用本人资料通过新录入产生，不从模型或客服上下文取得。
    changed = client.post(
        "/api/v1/me/shipping-inputs",
        headers={**customer, **key()},
        json={
            "recipient": "模拟收件人",
            "phone": "13900007777",
            "province": "浙江省",
            "city": "杭州市",
            "district": "西湖区",
            "street": "演示路88号",
            "purpose": "address_change",
        },
    )
    assert changed.status_code == 200, changed.text
    new_ref = changed.json()["data"]["business_ref"]
    response = propose_and_submit(
        client, customer, "order.address.change", order_id, 2, {"shipping_input_ref": new_ref}
    )
    assert response.status_code == 200, response.text
    detail = client.get(f"/api/v1/me/orders/{order_id}").json()["data"]
    assert detail["change_count"] == 1 and not detail["allowed_actions"]["change_address"]
    assert detail["shipping"]["street"] == "演示路88号"
    second = client.post(
        "/api/v1/action-proposals",
        headers=customer,
        json={
            "action": "order.address.change",
            "resource_ref": order_id,
            "expected_version": 3,
            "parameters": {"shipping_input_ref": new_ref},
        },
    )
    assert second.status_code == 409
    staff = login_as("shop_admin")
    detail = client.get(f"/api/v1/shop/orders/{order_id}").json()["data"]
    assert detail["shipping"]["phone_masked"] == "*******7777"
    assert detail["shipping"]["address_masked"] == "浙江省杭州市****"
    assert "演示路" not in str(detail) and "13900007777" not in str(detail)
    quantities = {row["order_item_id"]: row["ordered_qty"] for row in detail["items"]}
    response = client.post(
        f"/api/v1/shop/orders/{order_id}/shipments",
        headers={**staff, **key()},
        json={"quantities": quantities, "expected_version": 3},
    )
    assert response.status_code == 200, response.text
    tracking = client.post(
        f"/api/v1/shop/orders/{order_id}/shipments/{response.json()['data']['shipment_ref']}/tracking",
        headers={**staff, **key()},
        json={"status": "carrier_signed", "expected_version": 1},
    )
    assert tracking.status_code == 200, tracking.text
    customer = login_as("customer_a")
    detail = client.get(f"/api/v1/me/orders/{order_id}").json()["data"]
    assert detail["shipments"][0]["status"] == "carrier_signed"
    assert (
        detail["fulfillment_status"] == "in_transit" and not detail["allowed_actions"]["aftersale"]
    )
    assert (
        propose_and_submit(client, customer, "order.receipt.confirm", order_id, 4).status_code
        == 200
    )
    assert client.get(f"/api/v1/me/orders/{order_id}").json()["data"]["trade_status"] == "completed"


def test_unpaid_timeout_releases_reservation_without_wallet_change(environment, login_as):
    client, _, clock, _, _ = environment
    customer = login_as("customer_a")
    order_id = create_order(client, customer, [{"sku_id": "sku_vase", "qty": 1}])
    clock.value += timedelta(minutes=15)
    from cs_agent.commerce.expiration import expire_due

    with client.app.state.db.transaction() as session:
        assert expire_due(session, clock.now()) == 1
        assert expire_due(session, clock.now()) == 0
    detail = client.get(f"/api/v1/me/orders/{order_id}").json()["data"]
    assert detail["trade_status"] == "cancelled_unpaid"
    assert client.get("/api/v1/me/wallet").json()["data"]["balance_minor"] == "0"
    products = client.get("/api/v1/products").json()["data"]["items"]
    vase = next(p for p in products if p["product_id"] == "product_vase")
    assert vase["skus"][0]["available_qty"] == 1
