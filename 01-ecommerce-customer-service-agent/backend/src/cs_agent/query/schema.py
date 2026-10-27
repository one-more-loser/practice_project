import hashlib
import json

REVISION = "CS-QUERY-B2-v1"
METRICS_REVISION = "CS-METRIC-B2-v1"
# 这里只登记当前已实现资源；运输异常与售后由B3增量加入。
SCHEMA = {
    "cs_order_self": {
        "order_id": "TEXT",
        "created_at": "TIMESTAMPTZ",
        "paid_at": "TIMESTAMPTZ",
        "received_at": "TIMESTAMPTZ",
        "trade_status": "TEXT",
        "fulfillment_status": "TEXT",
        "goods_minor": "BIGINT",
        "discount_minor": "BIGINT",
        "freight_minor": "BIGINT",
        "payable_minor": "BIGINT",
        "paid_minor": "BIGINT",
        "refunded_minor": "BIGINT",
        "net_paid_minor": "BIGINT",
        "currency": "TEXT",
    },
    "cs_order_item_self": {
        "order_item_id": "TEXT",
        "order_id": "TEXT",
        "sku_id": "TEXT",
        "product_name": "TEXT",
        "ordered_qty": "INT",
        "paid_goods_minor": "BIGINT",
        "refunded_qty": "INT",
        "refunded_goods_minor": "BIGINT",
        "remaining_qty": "INT",
        "remaining_goods_minor": "BIGINT",
    },
    "cs_shipment_self": {
        "shipment_id": "TEXT",
        "order_id": "TEXT",
        "carrier": "TEXT",
        "status": "TEXT",
        "shipped_at": "TIMESTAMPTZ",
        "carrier_signed_at": "TIMESTAMPTZ",
        "tracking_updated_at": "TIMESTAMPTZ",
    },
    "cs_wallet_summary_self": {
        "account_id": "TEXT",
        "balance_minor": "BIGINT",
        "currency": "TEXT",
        "status": "TEXT",
    },
    "cs_wallet_entry_self": {
        "entry_id": "TEXT",
        "kind": "TEXT",
        "amount_minor": "BIGINT",
        "sequence": "BIGINT",
        "balance_before_minor": "BIGINT",
        "balance_after_minor": "BIGINT",
        "occurred_at": "TIMESTAMPTZ",
        "currency": "TEXT",
    },
    "cs_product_public": {
        "product_id": "TEXT",
        "sku_id": "TEXT",
        "name": "TEXT",
        "category": "TEXT",
        "price_minor": "BIGINT",
        "currency": "TEXT",
        "available_qty": "INT",
        "price_version": "INT",
        "as_of": "TIMESTAMPTZ",
    },
}
DIGEST = hashlib.sha256(json.dumps(SCHEMA, sort_keys=True).encode()).hexdigest()
PARAMETERS = {"start_at", "end_at", "order_id", "sku_id", "status"}
