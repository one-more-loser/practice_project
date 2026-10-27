CREATE SCHEMA IF NOT EXISTS csq;

REVOKE ALL ON SCHEMA csq FROM PUBLIC;

GRANT USAGE ON SCHEMA csq TO cs_agent_query_b2;

GRANT SELECT(batch_id,generation,status) ON source_batch TO cs_agent_query_b2;

GRANT SELECT(store_id,status) ON store TO cs_agent_query_b2;

ALTER TABLE customer_order ENABLE ROW LEVEL SECURITY;

ALTER TABLE customer_order FORCE ROW LEVEL SECURITY;

CREATE POLICY b2_service ON customer_order TO cs_agent_owner_b0,cs_agent_app_b0 USING(true) WITH CHECK(true);

CREATE POLICY b2_query ON customer_order FOR SELECT TO cs_agent_query_b2 USING(store_id=current_setting('cs.store',true)
      AND data_batch_id=current_setting('cs.batch',true)
      AND batch_generation::text=current_setting('cs.generation',true)
      AND EXISTS(SELECT 1 FROM source_batch b WHERE b.batch_id=data_batch_id
        AND b.generation=batch_generation AND b.status='active')
      AND EXISTS(SELECT 1 FROM store s WHERE s.store_id=store_id AND s.status='active') AND owner_id=current_setting('cs.owner',true) AND current_setting('cs.role',true)='customer');

GRANT SELECT(order_id,owner_id,store_id,data_batch_id,batch_generation,created_at,paid_at,received_at,trade_status,fulfillment_status,goods_minor,discount_minor,freight_minor,payable_minor,paid_minor,refunded_minor,currency) ON customer_order TO cs_agent_query_b2;

ALTER TABLE order_item ENABLE ROW LEVEL SECURITY;

ALTER TABLE order_item FORCE ROW LEVEL SECURITY;

CREATE POLICY b2_service ON order_item TO cs_agent_owner_b0,cs_agent_app_b0 USING(true) WITH CHECK(true);

CREATE POLICY b2_query ON order_item FOR SELECT TO cs_agent_query_b2 USING(store_id=current_setting('cs.store',true)
      AND data_batch_id=current_setting('cs.batch',true)
      AND batch_generation::text=current_setting('cs.generation',true)
      AND EXISTS(SELECT 1 FROM source_batch b WHERE b.batch_id=data_batch_id
        AND b.generation=batch_generation AND b.status='active')
      AND EXISTS(SELECT 1 FROM store s WHERE s.store_id=store_id AND s.status='active') AND order_id IN (SELECT order_id FROM customer_order) AND current_setting('cs.role',true)='customer');

GRANT SELECT(order_item_id,order_id,sku_id,product_name,ordered_qty,paid_goods_minor,refunded_qty,refunded_goods_minor,reserved_refund_qty,reserved_refund_minor) ON order_item TO cs_agent_query_b2;

ALTER TABLE order_shipment ENABLE ROW LEVEL SECURITY;

ALTER TABLE order_shipment FORCE ROW LEVEL SECURITY;

CREATE POLICY b2_service ON order_shipment TO cs_agent_owner_b0,cs_agent_app_b0 USING(true) WITH CHECK(true);

CREATE POLICY b2_query ON order_shipment FOR SELECT TO cs_agent_query_b2 USING(store_id=current_setting('cs.store',true)
      AND data_batch_id=current_setting('cs.batch',true)
      AND batch_generation::text=current_setting('cs.generation',true)
      AND EXISTS(SELECT 1 FROM source_batch b WHERE b.batch_id=data_batch_id
        AND b.generation=batch_generation AND b.status='active')
      AND EXISTS(SELECT 1 FROM store s WHERE s.store_id=store_id AND s.status='active') AND order_id IN (SELECT order_id FROM customer_order) AND current_setting('cs.role',true)='customer');

GRANT SELECT(shipment_id,order_id,carrier,status,shipped_at,carrier_signed_at,tracking_updated_at) ON order_shipment TO cs_agent_query_b2;

ALTER TABLE wallet_account ENABLE ROW LEVEL SECURITY;

ALTER TABLE wallet_account FORCE ROW LEVEL SECURITY;

CREATE POLICY b2_service ON wallet_account TO cs_agent_owner_b0,cs_agent_app_b0 USING(true) WITH CHECK(true);

CREATE POLICY b2_query ON wallet_account FOR SELECT TO cs_agent_query_b2 USING(store_id=current_setting('cs.store',true)
      AND data_batch_id=current_setting('cs.batch',true)
      AND batch_generation::text=current_setting('cs.generation',true)
      AND EXISTS(SELECT 1 FROM source_batch b WHERE b.batch_id=data_batch_id
        AND b.generation=batch_generation AND b.status='active')
      AND EXISTS(SELECT 1 FROM store s WHERE s.store_id=store_id AND s.status='active') AND owner_id=current_setting('cs.owner',true) AND current_setting('cs.role',true)='customer');

GRANT SELECT(account_id,balance_minor,currency,status) ON wallet_account TO cs_agent_query_b2;

ALTER TABLE wallet_entry ENABLE ROW LEVEL SECURITY;

ALTER TABLE wallet_entry FORCE ROW LEVEL SECURITY;

CREATE POLICY b2_service ON wallet_entry TO cs_agent_owner_b0,cs_agent_app_b0 USING(true) WITH CHECK(true);

CREATE POLICY b2_query ON wallet_entry FOR SELECT TO cs_agent_query_b2 USING(store_id=current_setting('cs.store',true)
      AND data_batch_id=current_setting('cs.batch',true)
      AND batch_generation::text=current_setting('cs.generation',true)
      AND EXISTS(SELECT 1 FROM source_batch b WHERE b.batch_id=data_batch_id
        AND b.generation=batch_generation AND b.status='active')
      AND EXISTS(SELECT 1 FROM store s WHERE s.store_id=store_id AND s.status='active') AND owner_id=current_setting('cs.owner',true) AND current_setting('cs.role',true)='customer');

GRANT SELECT(entry_id,kind,amount_minor,sequence,balance_before_minor,balance_after_minor,occurred_at,currency) ON wallet_entry TO cs_agent_query_b2;

ALTER TABLE product ENABLE ROW LEVEL SECURITY;

ALTER TABLE product FORCE ROW LEVEL SECURITY;

CREATE POLICY b2_service ON product TO cs_agent_owner_b0,cs_agent_app_b0 USING(true) WITH CHECK(true);

CREATE POLICY b2_query ON product FOR SELECT TO cs_agent_query_b2 USING(store_id=current_setting('cs.store',true)
      AND data_batch_id=current_setting('cs.batch',true)
      AND batch_generation::text=current_setting('cs.generation',true)
      AND EXISTS(SELECT 1 FROM source_batch b WHERE b.batch_id=data_batch_id
        AND b.generation=batch_generation AND b.status='active')
      AND EXISTS(SELECT 1 FROM store s WHERE s.store_id=store_id AND s.status='active'));

GRANT SELECT(product_id,name,category,status) ON product TO cs_agent_query_b2;

ALTER TABLE sku ENABLE ROW LEVEL SECURITY;

ALTER TABLE sku FORCE ROW LEVEL SECURITY;

CREATE POLICY b2_service ON sku TO cs_agent_owner_b0,cs_agent_app_b0 USING(true) WITH CHECK(true);

CREATE POLICY b2_query ON sku FOR SELECT TO cs_agent_query_b2 USING(store_id=current_setting('cs.store',true)
      AND data_batch_id=current_setting('cs.batch',true)
      AND batch_generation::text=current_setting('cs.generation',true)
      AND EXISTS(SELECT 1 FROM source_batch b WHERE b.batch_id=data_batch_id
        AND b.generation=batch_generation AND b.status='active')
      AND EXISTS(SELECT 1 FROM store s WHERE s.store_id=store_id AND s.status='active'));

GRANT SELECT(sku_id,product_id,price_minor,currency,price_version,status,updated_at) ON sku TO cs_agent_query_b2;

ALTER TABLE inventory ENABLE ROW LEVEL SECURITY;

ALTER TABLE inventory FORCE ROW LEVEL SECURITY;

CREATE POLICY b2_service ON inventory TO cs_agent_owner_b0,cs_agent_app_b0 USING(true) WITH CHECK(true);

CREATE POLICY b2_query ON inventory FOR SELECT TO cs_agent_query_b2 USING(store_id=current_setting('cs.store',true)
      AND data_batch_id=current_setting('cs.batch',true)
      AND batch_generation::text=current_setting('cs.generation',true)
      AND EXISTS(SELECT 1 FROM source_batch b WHERE b.batch_id=data_batch_id
        AND b.generation=batch_generation AND b.status='active')
      AND EXISTS(SELECT 1 FROM store s WHERE s.store_id=store_id AND s.status='active'));

GRANT SELECT(sku_id,stock_qty,reserved_qty) ON inventory TO cs_agent_query_b2;

CREATE VIEW csq.cs_order_self WITH(security_invoker=true,security_barrier=true) AS SELECT order_id,created_at,paid_at,received_at,trade_status,fulfillment_status,goods_minor,discount_minor,freight_minor,payable_minor,paid_minor,refunded_minor,paid_minor-refunded_minor AS net_paid_minor,currency FROM public.customer_order;

GRANT SELECT ON csq.cs_order_self TO cs_agent_query_b2;

CREATE VIEW csq.cs_order_item_self WITH(security_invoker=true,security_barrier=true) AS SELECT order_item_id,order_id,sku_id,product_name,ordered_qty,paid_goods_minor,refunded_qty,refunded_goods_minor,ordered_qty-refunded_qty-reserved_refund_qty AS remaining_qty,paid_goods_minor-refunded_goods_minor-reserved_refund_minor AS remaining_goods_minor FROM public.order_item;

GRANT SELECT ON csq.cs_order_item_self TO cs_agent_query_b2;

CREATE VIEW csq.cs_shipment_self WITH(security_invoker=true,security_barrier=true) AS SELECT shipment_id,order_id,carrier,status,shipped_at,carrier_signed_at,tracking_updated_at FROM public.order_shipment;

GRANT SELECT ON csq.cs_shipment_self TO cs_agent_query_b2;

CREATE VIEW csq.cs_wallet_summary_self WITH(security_invoker=true,security_barrier=true) AS SELECT account_id,balance_minor,currency,status FROM public.wallet_account;

GRANT SELECT ON csq.cs_wallet_summary_self TO cs_agent_query_b2;

CREATE VIEW csq.cs_wallet_entry_self WITH(security_invoker=true,security_barrier=true) AS SELECT entry_id,kind,amount_minor,sequence,balance_before_minor,balance_after_minor,occurred_at,currency FROM public.wallet_entry;

GRANT SELECT ON csq.cs_wallet_entry_self TO cs_agent_query_b2;

CREATE VIEW csq.cs_product_public WITH(security_invoker=true,security_barrier=true) AS SELECT p.product_id,s.sku_id,p.name,p.category,s.price_minor,s.currency,i.stock_qty-i.reserved_qty AS available_qty,s.price_version,s.updated_at AS as_of FROM public.product p JOIN public.sku s ON p.product_id=s.product_id JOIN public.inventory i ON i.sku_id=s.sku_id WHERE p.status='active' AND s.status='active';

GRANT SELECT ON csq.cs_product_public TO cs_agent_query_b2;
