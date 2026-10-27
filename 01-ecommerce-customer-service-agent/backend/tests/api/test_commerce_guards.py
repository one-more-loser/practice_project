from datetime import timedelta

from tests.api.test_checkout import create_order, key, propose_and_submit


def test_confirmation_expiry_cancel_and_original_result(environment, login_as):
    client, _, clock, _, _ = environment
    headers = login_as("customer_a")
    order_id = create_order(client, headers)

    def propose():
        response = client.post(
            "/api/v1/action-proposals",
            headers=headers,
            json={"action": "order.cancel", "resource_ref": order_id, "expected_version": 1},
        )
        assert response.status_code == 200
        return response.json()["data"]["confirmation_id"]

    expired = propose()
    clock.value += timedelta(minutes=5)
    assert (
        client.post(
            f"/api/v1/confirmations/{expired}/submit",
            headers={**headers, **key()},
            json={"decision": "confirm"},
        ).status_code
        == 409
    )
    cancelled = propose()
    assert (
        client.post(
            f"/api/v1/confirmations/{cancelled}/submit",
            headers={**headers, **key()},
            json={"decision": "cancel"},
        ).status_code
        == 200
    )
    assert (
        client.get(f"/api/v1/me/orders/{order_id}").json()["data"]["trade_status"]
        == "awaiting_payment"
    )
    confirmed = propose()
    same_key = key()
    first = client.post(
        f"/api/v1/confirmations/{confirmed}/submit",
        headers={**headers, **same_key},
        json={"decision": "confirm"},
    )
    assert first.status_code == 200
    # 会话最长24小时，延后重放需重新登录；去重不依赖旧会话在线。
    clock.value += timedelta(hours=25)
    headers = login_as("customer_a")
    replay = client.post(
        f"/api/v1/confirmations/{confirmed}/submit",
        headers={**headers, **key()},
        json={"decision": "confirm"},
    )
    assert replay.status_code == 200 and replay.json()["data"] == first.json()["data"]
    assert (
        client.post(
            f"/api/v1/confirmations/{confirmed}/submit",
            headers={**headers, **same_key},
            json={"decision": "cancel"},
        ).status_code
        == 409
    )


def test_insufficient_balance_rollback_owner_and_role_boundaries(environment, login_as):
    client, *_ = environment
    headers = login_as("customer_a")
    client.post(
        "/api/v1/me/topups",
        headers={**headers, **key()},
        json={"currency": "CNY", "amount_minor": "3000"},
    )
    order_id = create_order(client, headers)
    assert propose_and_submit(client, headers, "order.pay", order_id, 1).status_code == 409
    assert client.get("/api/v1/me/wallet").json()["data"]["balance_minor"] == "3000"
    assert client.get(f"/api/v1/me/orders/{order_id}").json()["data"]["paid_minor"] == "0"
    headers_b = login_as("customer_b")
    assert client.get(f"/api/v1/me/orders/{order_id}").status_code == 404
    assert client.get("/api/v1/me/orders/nonexistent").status_code == 404
    assert (
        client.post(
            "/api/v1/action-proposals",
            headers=headers_b,
            json={"action": "order.pay", "resource_ref": order_id, "expected_version": 1},
        ).status_code
        == 404
    )
    for role in ("support_a", "supervisor", "knowledge_admin", "shop_admin"):
        staff = login_as(role)
        assert client.get("/api/v1/me/wallet").status_code == 403
        assert (
            client.post(
                "/api/v1/me/topups",
                headers={**staff, **key()},
                json={"currency": "CNY", "amount_minor": "1"},
            ).status_code
            == 403
        )


def test_catalog_price_change_invalidates_quote_and_reserved_inventory_is_protected(
    environment, login_as
):
    client, *_ = environment
    headers = login_as("customer_a")
    quote = client.post(
        "/api/v1/me/quotes",
        headers={**headers, **key()},
        json={"items": [{"sku_id": "sku_vase", "qty": 1}], "shipping_input_ref": "shipping_sim_a"},
    ).json()["data"]
    proposal = client.post(
        "/api/v1/action-proposals",
        headers=headers,
        json={"action": "order.create", "resource_ref": quote["quote_id"], "expected_version": 1},
    ).json()["data"]
    shop = login_as("shop_admin")
    assert (
        client.patch(
            "/api/v1/shop/skus/sku_vase",
            headers={**shop, **key()},
            json={"price_minor": "3100", "expected_version": 1},
        ).status_code
        == 200
    )
    headers = login_as("customer_a")
    assert (
        client.post(
            f"/api/v1/confirmations/{proposal['confirmation_id']}/submit",
            headers={**headers, **key()},
            json={"decision": "confirm"},
        ).status_code
        == 409
    )
    order_id = create_order(client, headers, [{"sku_id": "sku_vase", "qty": 1}])
    shop = login_as("shop_admin")
    assert (
        client.post(
            "/api/v1/shop/skus/sku_vase/inventory-adjustments",
            headers={**shop, **key()},
            json={"delta": -1, "expected_version": 2, "reason": "模拟盘点"},
        ).status_code
        == 409
    )
    assert client.get(f"/api/v1/shop/orders/{order_id}").json()["data"]["paid_minor"] == "0"


def test_cart_quote_consumes_only_bound_items_and_partial_shipping_is_rejected(
    environment, login_as
):
    client, *_ = environment
    headers = login_as("customer_a")
    for sku in ("sku_cup_ivory", "sku_cup_coral"):
        assert (
            client.put(
                f"/api/v1/me/cart/items/{sku}",
                headers={**headers, **key()},
                json={"qty": 1, "selected": True, "expected_version": 0},
            ).status_code
            == 200
        )
    cart = client.get("/api/v1/me/cart").json()["data"]
    quote = client.post(
        "/api/v1/me/quotes",
        headers={**headers, **key()},
        json={
            "cart_item_ids": [cart["items"][0]["item_id"]],
            "shipping_input_ref": "shipping_sim_a",
        },
    ).json()["data"]
    result = propose_and_submit(client, headers, "order.create", quote["quote_id"], 1)
    assert result.status_code == 200
    assert len(client.get("/api/v1/me/cart").json()["data"]["items"]) == 1
    order_id = result.json()["data"]["business_ref"]
    client.post(
        "/api/v1/me/topups",
        headers={**headers, **key()},
        json={"currency": "CNY", "amount_minor": "10000"},
    )
    assert propose_and_submit(client, headers, "order.pay", order_id, 1).status_code == 200
    proposal = client.post(
        "/api/v1/action-proposals",
        headers=headers,
        json={"action": "order.cancel", "resource_ref": order_id, "expected_version": 2},
    )
    assert proposal.status_code == 200
    assert proposal.json()["data"]["summary"]["requires_agent_review"] is True
    assert client.get(f"/api/v1/me/orders/{order_id}").json()["data"]["trade_status"] == "paid"
    shop = login_as("shop_admin")
    assert (
        client.post(
            f"/api/v1/shop/orders/{order_id}/shipments",
            headers={**shop, **key()},
            json={"quantities": {}, "expected_version": 2},
        ).status_code
        == 422
    )


def test_shop_can_edit_product_but_customer_cannot(environment, login_as):
    client, *_ = environment
    headers = login_as("customer_a")
    payload = {"name": "日常陶瓷杯·新版", "expected_version": 1}
    assert (
        client.patch(
            "/api/v1/shop/products/product_cup", headers={**headers, **key()}, json=payload
        ).status_code
        == 403
    )
    headers = login_as("shop_admin")
    assert (
        client.patch(
            "/api/v1/shop/products/product_cup", headers={**headers, **key()}, json=payload
        ).status_code
        == 200
    )
    assert client.get("/api/v1/products/product_cup").json()["data"]["name"] == "日常陶瓷杯·新版"
