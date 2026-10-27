from datetime import timedelta


def demo_snapshot(now):
    """明确标注模拟来源；不是用户真实经营数据。"""
    return {
        "snapshot_id": "snapshot_demo", "revision": 1, "store_id": "store_demo",
        "source_kind": "synthetic", "as_of": now.isoformat(), "currency": "CNY",
        "skus": [
            {"sku_id": "sku_A", "title": "轻量通勤双肩包", "category": "箱包",
             "base_price_minor": "10000", "cost_minor": "6000", "variable_fee_minor": "500",
             "available_qty": 120, "price_version": 1},
            {"sku_id": "sku_B", "title": "日常随行保温杯", "category": "生活用品",
             "base_price_minor": "20000", "cost_minor": "11000", "variable_fee_minor": "800",
             "available_qty": 80, "price_version": 1},
        ],
        "orders": [
            {"order_id": "order_demo_1", "paid_at": (now - timedelta(days=1)).isoformat(),
             "status": "paid", "channel": "店铺自然流量", "paid_minor": "30000",
             "items": [{"sku_id": "sku_A", "qty": 1, "paid_minor": "10000"},
                       {"sku_id": "sku_B", "qty": 1, "paid_minor": "20000"}]},
            {"order_id": "order_demo_old", "paid_at": (now - timedelta(days=40)).isoformat(),
             "status": "paid", "channel": "店铺自然流量", "paid_minor": "5000",
             "items": [{"sku_id": "sku_A", "qty": 1, "paid_minor": "5000"}]},
        ],
        "refunds": [
            {"refund_id": "refund_demo_1", "order_id": "order_demo_1",
             "occurred_at": (now - timedelta(hours=12)).isoformat(),
             "status": "succeeded", "amount_minor": "10000"},
            {"refund_id": "refund_demo_old", "order_id": "order_demo_old",
             "occurred_at": (now - timedelta(hours=2)).isoformat(),
             "status": "succeeded", "amount_minor": "5000"},
        ],
        "execution_supported_rules": ["direct"],
    }
