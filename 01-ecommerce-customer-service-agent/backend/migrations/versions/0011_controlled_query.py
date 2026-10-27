"""当前B1事实的只读投影；查询角色需事先在独立集群核对创建。"""

from alembic import op

revision = "0011_controlled_query"
down_revision = "0010_business_integrity"


def upgrade():
    op.execute("CREATE SCHEMA IF NOT EXISTS csq")
    op.execute("REVOKE ALL ON SCHEMA csq FROM PUBLIC")
    op.execute("GRANT USAGE ON SCHEMA csq TO cs_agent_query_b2")
    op.execute("GRANT SELECT(batch_id,generation,status) ON source_batch TO cs_agent_query_b2")
    op.execute("GRANT SELECT(store_id,status) ON store TO cs_agent_query_b2")
    common = """store_id=current_setting('cs.store',true)
      AND data_batch_id=current_setting('cs.batch',true)
      AND batch_generation::text=current_setting('cs.generation',true)
      AND EXISTS(SELECT 1 FROM source_batch b WHERE b.batch_id=data_batch_id
        AND b.generation=batch_generation AND b.status='active')
      AND EXISTS(SELECT 1 FROM store s WHERE s.store_id=store_id AND s.status='active')"""
    columns = {
        "customer_order": "order_id,owner_id,store_id,data_batch_id,batch_generation,created_at,paid_at,received_at,trade_status,fulfillment_status,goods_minor,discount_minor,freight_minor,payable_minor,paid_minor,refunded_minor,currency",
        "order_item": "order_item_id,order_id,sku_id,product_name,ordered_qty,paid_goods_minor,refunded_qty,refunded_goods_minor,reserved_refund_qty,reserved_refund_minor",
        "order_shipment": "shipment_id,order_id,carrier,status,shipped_at,carrier_signed_at,tracking_updated_at",
        "wallet_account": "account_id,balance_minor,currency,status",
        "wallet_entry": "entry_id,kind,amount_minor,sequence,balance_before_minor,balance_after_minor,occurred_at,currency",
        "product": "product_id,name,category,status",
        "sku": "sku_id,product_id,price_minor,currency,price_version,status,updated_at",
        "inventory": "sku_id,stock_qty,reserved_qty",
    }
    for table, fields in columns.items():
        # 全部标识符为本迁移固定常量，不插入外部数据。
        scope = common
        if table in {"customer_order", "wallet_account", "wallet_entry"}:
            scope += " AND owner_id=current_setting('cs.owner',true) AND current_setting('cs.role',true)='customer'"
        elif table in {"order_item", "order_shipment"}:
            scope += " AND order_id IN (SELECT order_id FROM customer_order) AND current_setting('cs.role',true)='customer'"
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY b2_service ON {table} TO cs_agent_owner_b0,cs_agent_app_b0 USING(true) WITH CHECK(true)"
        )
        op.execute(
            f"CREATE POLICY b2_query ON {table} FOR SELECT TO cs_agent_query_b2 USING({scope})"
        )
        op.execute(f"GRANT SELECT({fields}) ON {table} TO cs_agent_query_b2")
    views = {
        "cs_order_self": "SELECT order_id,created_at,paid_at,received_at,trade_status,fulfillment_status,goods_minor,discount_minor,freight_minor,payable_minor,paid_minor,refunded_minor,paid_minor-refunded_minor AS net_paid_minor,currency FROM public.customer_order",
        "cs_order_item_self": "SELECT order_item_id,order_id,sku_id,product_name,ordered_qty,paid_goods_minor,refunded_qty,refunded_goods_minor,ordered_qty-refunded_qty-reserved_refund_qty AS remaining_qty,paid_goods_minor-refunded_goods_minor-reserved_refund_minor AS remaining_goods_minor FROM public.order_item",
        "cs_shipment_self": "SELECT shipment_id,order_id,carrier,status,shipped_at,carrier_signed_at,tracking_updated_at FROM public.order_shipment",
        "cs_wallet_summary_self": "SELECT account_id,balance_minor,currency,status FROM public.wallet_account",
        "cs_wallet_entry_self": "SELECT entry_id,kind,amount_minor,sequence,balance_before_minor,balance_after_minor,occurred_at,currency FROM public.wallet_entry",
        "cs_product_public": "SELECT p.product_id,s.sku_id,p.name,p.category,s.price_minor,s.currency,i.stock_qty-i.reserved_qty AS available_qty,s.price_version,s.updated_at AS as_of FROM public.product p JOIN public.sku s ON p.product_id=s.product_id JOIN public.inventory i ON i.sku_id=s.sku_id WHERE p.status='active' AND s.status='active'",
    }
    for name, select in views.items():
        op.execute(
            f"CREATE VIEW csq.{name} WITH(security_invoker=true,security_barrier=true) AS {select}"
        )
        op.execute(f"GRANT SELECT ON csq.{name} TO cs_agent_query_b2")


def downgrade():
    raise RuntimeError("保留业务历史；查询视图变更通过后续迁移处理")
