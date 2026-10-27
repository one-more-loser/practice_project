from tests.api.test_checkout import create_order, key, propose_and_submit


def test_shipping_success_invalidates_pending_address_confirmation(environment, login_as):
    client, *_ = environment
    customer = login_as("customer_a")
    client.post(
        "/api/v1/me/topups",
        headers={**customer, **key()},
        json={"currency": "CNY", "amount_minor": "10000"},
    )
    order_id = create_order(client, customer)
    assert propose_and_submit(client, customer, "order.pay", order_id, 1).status_code == 200
    shipping = client.post(
        "/api/v1/me/shipping-inputs",
        headers={**customer, **key()},
        json={
            "recipient": "模拟顾客",
            "phone": "13900007777",
            "province": "浙江省",
            "city": "杭州市",
            "district": "西湖区",
            "street": "演示路88号",
            "purpose": "address_change",
        },
    )
    proposed = client.post(
        "/api/v1/action-proposals",
        headers=customer,
        json={
            "action": "order.address.change",
            "resource_ref": order_id,
            "expected_version": 2,
            "parameters": {"shipping_input_ref": shipping.json()["data"]["business_ref"]},
        },
    )
    assert proposed.status_code == 200
    shop = login_as("shop_admin")
    before = client.get(f"/api/v1/shop/orders/{order_id}").json()["data"]
    quantities = {item["order_item_id"]: item["ordered_qty"] for item in before["items"]}
    assert (
        client.post(
            f"/api/v1/shop/orders/{order_id}/shipments",
            headers={**shop, **key()},
            json={"expected_version": 2, "quantities": quantities},
        ).status_code
        == 200
    )
    customer = login_as("customer_a")
    confirmation = proposed.json()["data"]["confirmation_id"]
    assert (
        client.post(
            f"/api/v1/confirmations/{confirmation}/submit",
            headers={**customer, **key()},
            json={"decision": "confirm"},
        ).status_code
        == 409
    )
    detail = client.get(f"/api/v1/me/orders/{order_id}").json()["data"]
    assert detail["change_count"] == 0 and detail["fulfillment_status"] == "in_transit"
    assert not detail["allowed_actions"]["change_address"]
