-- FX-C: RLS inactive-store probe + legal multi-row fan-out.
TRUNCATE app.order_item, app.order_shipment, app.wallet_entry, app.wallet_account,
  app.customer_order, app.inventory, app.sku, app.product, app.subject,
  app.source_batch, app.store CASCADE;

INSERT INTO app.store VALUES ('store_a','active'), ('store_b','inactive');
INSERT INTO app.source_batch VALUES ('batch_1',1,'active');
INSERT INTO app.subject VALUES ('cust_a');

INSERT INTO app.customer_order
(order_id,owner_id,store_id,data_batch_id,batch_generation,created_at,paid_at,received_at,
 trade_status,fulfillment_status,goods_minor,discount_minor,freight_minor,payable_minor,
 paid_minor,refunded_minor,refunded_goods_minor,refunded_freight_minor)
VALUES
('C1','cust_a','store_a','batch_1',1,'2026-11-01T02:00:00+00','2026-11-01T03:00:00+00',NULL,'paid','unshipped',1000,0,0,1000,1000,0,0,0),
('C2','cust_a','store_a','batch_1',1,'2026-11-02T02:00:00+00','2026-11-02T03:00:00+00',NULL,'paid','unshipped',4000,0,0,4000,4000,0,0,0),
('C3','cust_a','store_b','batch_1',1,'2026-11-03T02:00:00+00','2026-11-03T03:00:00+00',NULL,'paid','unshipped',3000,0,0,3000,3000,0,0,0),
('C4','cust_a','store_a','batch_1',1,'2026-11-04T02:00:00+00',NULL,NULL,'awaiting_payment','unshipped',1000,0,0,1000,0,0,0,0);

INSERT INTO app.order_item
(order_item_id,order_id,sku_id,product_name,store_id,data_batch_id,batch_generation,
 unit_price_minor,ordered_qty,goods_minor,discount_minor,allocated_minor,paid_goods_minor,
 refunded_qty,reserved_refund_qty,refunded_goods_minor,reserved_refund_minor)
VALUES
('CI1','C1','sku_1','Alpha','store_a','batch_1',1,1000,1,1000,0,1000,1000,0,0,0,0),
('CI2','C2','sku_1','Alpha','store_a','batch_1',1,2000,1,2000,0,2000,2000,0,0,0,0),
('CI3','C2','sku_2','Beta','store_a','batch_1',1,2000,1,2000,0,2000,2000,0,0,0,0),
('CI4','C3','sku_1','Alpha','store_b','batch_1',1,3000,1,3000,0,3000,3000,0,0,0,0),
('CI5','C4','sku_1','Alpha','store_a','batch_1',1,1000,1,1000,0,1000,0,0,0,0,0);

INSERT INTO app.wallet_account
(account_id,owner_id,store_id,data_batch_id,batch_generation,balance_minor,currency,status,ledger_sequence)
VALUES ('WA1','cust_a','store_a','batch_1',1,0,'CNY','active',1);

INSERT INTO app.wallet_entry
(entry_id,account_id,owner_id,store_id,data_batch_id,batch_generation,kind,currency,
 amount_minor,sequence,balance_before_minor,balance_after_minor,occurred_at)
VALUES ('WE1','WA1','cust_a','store_a','batch_1',1,'initialization','CNY',0,0,0,0,'2026-10-01T00:00:00+00');

INSERT INTO app.product
(product_id,name,category,status,store_id,data_batch_id,batch_generation)
VALUES ('P1','Alpha','cat_a','active','store_a','batch_1',1),
       ('P2','Beta','cat_b','active','store_a','batch_1',1);

INSERT INTO app.sku
(sku_id,product_id,price_minor,currency,price_version,status,updated_at,store_id,data_batch_id,batch_generation)
VALUES ('sku_1','P1',10000,'CNY',1,'active','2026-11-01T00:00:00+00','store_a','batch_1',1),
       ('sku_2','P2',5000,'CNY',1,'active','2026-11-01T00:00:00+00','store_a','batch_1',1);

INSERT INTO app.inventory
(sku_id,stock_qty,reserved_qty,store_id,data_batch_id,batch_generation)
VALUES ('sku_1',6,2,'store_a','batch_1',1),
       ('sku_2',2,0,'store_a','batch_1',1);
