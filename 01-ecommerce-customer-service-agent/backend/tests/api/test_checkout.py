from uuid import uuid4


def key():
    return {"Idempotency-Key": "test_" + uuid4().hex}


def propose_and_submit(client, headers, action, resource, version, parameters=None):
    proposed = client.post(
        "/api/v1/action-proposals",
        headers=headers,
        json={
            "action": action,
            "resource_ref": resource,
            "expected_version": version,
            "parameters": parameters or {},
        },
    )
    assert proposed.status_code == 200, proposed.text
    response = client.post(
        f"/api/v1/confirmations/{proposed.json()['data']['confirmation_id']}/submit",
        headers={**headers, **key()},
        json={"decision": "confirm"},
    )
    return response


def create_order(client, headers, items=None):
    quote = client.post(
        "/api/v1/me/quotes",
        headers={**headers, **key()},
        json={
            "items": items or [{"sku_id": "sku_cup_ivory", "qty": 1}],
            "shipping_input_ref": "shipping_sim_a",
        },
    )
    assert quote.status_code == 200, quote.text
    quote_id = quote.json()["data"]["quote_id"]
    result = propose_and_submit(
        client, headers, "order.create", quote_id, 1, {"quote_ref": quote_id}
    )
    assert result.status_code == 200, result.text
    return result.json()["data"]["business_ref"]


def test_real_http_checkout_payment_and_multi_sku_totals(environment, login_as):
    client, *_ = environment
    headers = login_as("customer_a")
    assert client.get("/api/v1/me/wallet").json()["data"]["balance_minor"] == "0"
    topup = client.post(
        "/api/v1/me/topups",
        headers={**headers, **key()},
        json={"currency": "CNY", "amount_minor": "10000"},
    )
    assert topup.status_code == 200
    order_id = create_order(client, headers)
    order = client.get(f"/api/v1/me/orders/{order_id}").json()["data"]
    assert order["payable_minor"] == "4000" and order["paid_minor"] == "0"
    result = propose_and_submit(client, headers, "order.pay", order_id, order["version"])
    assert result.status_code == 200, result.text
    assert client.get("/api/v1/me/wallet").json()["data"]["balance_minor"] == "6000"
    row = client.get(f"/api/v1/me/orders/{order_id}").json()["data"]
    assert row["trade_status"] == "paid" and row["fulfillment_status"] == "unshipped"
    assert row["paid_minor"] == "4000" and row["refunded_minor"] == "0"
    second = create_order(
        client,
        headers,
        [{"sku_id": "sku_cup_ivory", "qty": 2}, {"sku_id": "sku_cup_coral", "qty": 1}],
    )
    row = client.get(f"/api/v1/me/orders/{second}").json()["data"]
    assert row["goods_minor"] == row["payable_minor"] == "12200"
    assert sum(int(item["allocated_minor"]) for item in row["items"]) == 12200
    assert all(item["paid_goods_minor"] == "0" for item in row["items"])
