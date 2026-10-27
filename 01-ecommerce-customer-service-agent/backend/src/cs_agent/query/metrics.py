"""稳定口径目录；所有金额单位为CNY分，不混合不同实体粒度。"""

METRICS_REVISION = "CS-METRIC-B2-v1"
METRICS = {
    "orders.count": {
        "grain": "one row per customer_order",
        "definition": "orders created in the requested half-open time window, excluding unpaid and unpaid-cancelled orders",
        "time_field": "created_at",
        "amount_unit": None,
    },
    "orders.paid_minor": {
        "grain": "one row per customer_order",
        "definition": "sum of original paid_minor for paid orders created in the requested window",
        "time_field": "created_at",
        "amount_unit": "CNY minor units",
    },
    "orders.refunded_minor": {
        "grain": "one row per customer_order",
        "definition": "sum of refunded_minor for those same paid orders, independent of refund-entry count",
        "time_field": "created_at",
        "amount_unit": "CNY minor units",
    },
    "orders.net_paid_minor": {
        "grain": "one row per customer_order",
        "definition": "sum of paid_minor minus refunded_minor for those same paid orders",
        "time_field": "created_at",
        "amount_unit": "CNY minor units",
    },
}
