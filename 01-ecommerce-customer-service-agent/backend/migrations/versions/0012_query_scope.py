"""收紧查询的店铺有效性和默认时间窗；修正同名列关联的歧义。"""

from alembic import op

revision = "0012_query_scope"
down_revision = "0011_controlled_query"


def upgrade():
    for table in (
        "customer_order",
        "order_item",
        "order_shipment",
        "wallet_account",
        "wallet_entry",
        "product",
        "sku",
        "inventory",
    ):
        scope = f"""{table}.store_id=current_setting('cs.store',true)
          AND {table}.data_batch_id=current_setting('cs.batch',true)
          AND {table}.batch_generation::text=current_setting('cs.generation',true)
          AND EXISTS(SELECT 1 FROM source_batch b WHERE b.batch_id={table}.data_batch_id
            AND b.generation={table}.batch_generation AND b.status='active')
          AND EXISTS(SELECT 1 FROM store s WHERE s.store_id={table}.store_id AND s.status='active')"""
        if table in {"customer_order", "wallet_account", "wallet_entry"}:
            scope += " AND owner_id=current_setting('cs.owner',true) AND current_setting('cs.role',true)='customer'"
        if table in {"order_item", "order_shipment"}:
            scope += " AND order_id IN (SELECT order_id FROM customer_order) AND current_setting('cs.role',true)='customer'"
        timestamp = (
            "created_at"
            if table == "customer_order"
            else "occurred_at"
            if table == "wallet_entry"
            else None
        )
        if timestamp:
            scope += f" AND {timestamp}>=nullif(current_setting('cs.start',true),'')::timestamptz AND {timestamp}<=nullif(current_setting('cs.end',true),'')::timestamptz"
        op.execute(f"ALTER POLICY b2_query ON {table} USING({scope})")


def downgrade():
    raise RuntimeError("查询范围收紧不自动回退")
