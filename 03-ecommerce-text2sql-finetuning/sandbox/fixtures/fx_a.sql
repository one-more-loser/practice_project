-- FX-A: main review fixture. All values synthetic; no 01/02 data.
TRUNCATE app.order_item, app.order_shipment, app.wallet_entry, app.wallet_account,
  app.customer_order, app.inventory, app.sku, app.product, app.subject,
  app.source_batch, app.store CASCADE;

INSERT INTO app.store VALUES ('store_a','active'), ('store_b','inactive'), ('store_c','active');
INSERT INTO app.source_batch VALUES ('batch_1',1,'active'), ('batch_old',1,'inactive');
INSERT INTO app.subject VALUES ('cust_a'), ('cust_b'), ('cust_c');

INSERT INTO app.customer_order
(order_id,owner_id,store_id,data_batch_id,batch_generation,created_at,paid_at,received_at,
 trade_status,fulfillment_status,goods_minor,discount_minor,freight_minor,payable_minor,
 paid_minor,refunded_minor,refunded_goods_minor,refunded_freight_minor)
VALUES
('O1','cust_a','store_a','batch_1',1,'2026-10-31T16:30:00+00','2026-11-01T01:00:00+00',NULL,'paid','unshipped',10000,1000,500,9500,9500,0,0,0),
('O2','cust_a','store_a','batch_1',1,'2026-11-30T15:30:00+00','2026-11-30T16:00:00+00','2026-12-02T02:00:00+00','completed','received_by_customer',20000,0,0,20000,20000,5000,5000,0),
('O3','cust_a','store_a','batch_1',1,'2026-11-30T16:30:00+00','2026-12-01T01:00:00+00',NULL,'paid','unshipped',30000,0,1000,31000,31000,0,0,0),
('O4','cust_a','store_a','batch_1',1,'2026-11-15T02:00:00+00',NULL,NULL,'awaiting_payment','unshipped',5000,0,0,5000,0,0,0,0),
('O5','cust_a','store_a','batch_1',1,'2026-11-16T02:00:00+00',NULL,NULL,'cancelled_unpaid','unshipped',6000,0,0,6000,0,0,0,0),
('O6','cust_a','store_a','batch_1',1,'2026-11-17T02:00:00+00','2026-11-17T03:00:00+00',NULL,'cancelled_refunded','unshipped',8000,0,0,8000,8000,8000,8000,0),
('O7','cust_a','store_a','batch_1',1,'2026-11-18T02:00:00+00','2026-11-18T03:00:00+00',NULL,'closed_refunded','unshipped',12000,0,500,12500,12500,12500,12000,500),
('O8','cust_a','store_a','batch_1',1,'2026-11-19T02:00:00+00','2026-11-19T03:00:00+00',NULL,'paid','in_transit',4000,0,0,4000,4000,0,0,0),
('O9','cust_a','store_a','batch_1',1,'2026-11-20T02:00:00+00','2026-11-20T03:00:00+00',NULL,'paid','unshipped',6000,0,600,6600,6600,0,0,0),
('O10','cust_a','store_a','batch_1',1,'2026-10-15T02:00:00+00','2026-10-15T03:00:00+00',NULL,'paid','unshipped',5000,0,0,5000,5000,0,0,0),
('O11','cust_a','store_a','batch_1',1,'2026-11-25T02:00:00+00','2026-11-25T03:00:00+00','2026-11-28T02:00:00+00','completed','received_by_customer',7000,0,0,7000,7000,0,0,0),
('O20','cust_a','store_b','batch_1',1,'2026-11-10T02:00:00+00','2026-11-10T03:00:00+00',NULL,'paid','unshipped',9999,0,0,9999,9999,0,0,0),
('O30','cust_c','store_c','batch_1',1,'2026-11-11T02:00:00+00','2026-11-11T03:00:00+00',NULL,'paid','unshipped',5000,0,0,5000,5000,0,0,0);

INSERT INTO app.order_item
(order_item_id,order_id,sku_id,product_name,store_id,data_batch_id,batch_generation,
 unit_price_minor,ordered_qty,goods_minor,discount_minor,allocated_minor,paid_goods_minor,
 refunded_qty,reserved_refund_qty,refunded_goods_minor,reserved_refund_minor)
VALUES
('OI1','O1','sku_1','Alpha','store_a','batch_1',1,5000,1,5000,500,4500,4500,0,0,0,0),
('OI2','O1','sku_2','Beta','store_a','batch_1',1,5000,1,5000,500,4500,4500,0,0,0,0),
('OI3','O2','sku_1','Alpha','store_a','batch_1',1,10000,2,20000,0,20000,20000,1,0,5000,0),
('OI4','O3','sku_1','Alpha','store_a','batch_1',1,10000,3,30000,0,30000,30000,0,0,0,0),
('OI5','O4','sku_2','Beta','store_a','batch_1',1,5000,1,5000,0,5000,0,0,0,0,0),
('OI6','O5','sku_1','Alpha','store_a','batch_1',1,6000,1,6000,0,6000,0,0,0,0,0),
('OI7','O6','sku_2','Beta','store_a','batch_1',1,8000,1,8000,0,8000,8000,1,0,8000,0),
('OI8','O7','sku_3','Gamma','store_a','batch_1',1,12000,1,12000,0,12000,12000,1,0,12000,0),
('OI9','O8','sku_1','Alpha','store_a','batch_1',1,4000,1,4000,0,4000,4000,0,0,0,0),
('OI10','O9','sku_2','Beta','store_a','batch_1',1,6000,1,6000,0,6000,6000,0,0,0,0),
('OI11','O10','sku_1','Alpha','store_a','batch_1',1,5000,1,5000,0,5000,5000,0,0,0,0),
('OI12','O11','sku_1','Alpha','store_a','batch_1',1,7000,1,7000,0,7000,7000,0,0,0,0),
('OI20','O20','sku_1','Alpha','store_b','batch_1',1,9999,1,9999,0,9999,9999,0,0,0,0),
('OI30','O30','sku_2','Beta','store_c','batch_1',1,5000,1,5000,0,5000,5000,0,0,0,0);

INSERT INTO app.order_shipment
(shipment_id,order_id,carrier,status,shipped_at,carrier_signed_at,tracking_updated_at,
 store_id,data_batch_id,batch_generation)
VALUES
('SH1','O8','SF','in_transit','2026-11-20T03:00:00+00',NULL,'2026-11-21T03:00:00+00','store_a','batch_1',1),
('SH2','O2','SF','carrier_signed','2026-11-30T17:00:00+00','2026-12-02T02:00:00+00','2026-12-02T02:00:00+00','store_a','batch_1',1);

INSERT INTO app.wallet_account
(account_id,owner_id,store_id,data_batch_id,batch_generation,balance_minor,currency,status,ledger_sequence)
VALUES
('WA1','cust_a','store_a','batch_1',1,36500,'CNY','active',7),
('WB1','cust_a','store_b','batch_1',1,0,'CNY','active',0);

INSERT INTO app.wallet_entry
(entry_id,account_id,owner_id,store_id,data_batch_id,batch_generation,kind,currency,
 amount_minor,sequence,balance_before_minor,balance_after_minor,occurred_at)
VALUES
('WE1','WA1','cust_a','store_a','batch_1',1,'initialization','CNY',0,0,0,0,'2026-10-01T00:00:00+00'),
('WE2','WA1','cust_a','store_a','batch_1',1,'topup','CNY',10000,1,0,10000,'2026-10-02T00:00:00+00'),
('WE3','WA1','cust_a','store_a','batch_1',1,'payment','CNY',9500,2,10000,500,'2026-11-01T01:00:00+00'),
('WE4','WA1','cust_a','store_a','batch_1',1,'refund','CNY',5000,3,500,5500,'2026-11-20T03:00:00+00'),
('WE5','WA1','cust_a','store_a','batch_1',1,'refund','CNY',8000,4,5500,13500,'2026-11-21T03:00:00+00'),
('WE6','WA1','cust_a','store_a','batch_1',1,'refund','CNY',12500,5,13500,26000,'2026-11-22T03:00:00+00'),
('WE7','WA1','cust_a','store_a','batch_1',1,'topup','CNY',10000,6,26000,36000,'2026-11-25T03:00:00+00'),
('WE8','WA1','cust_a','store_a','batch_1',1,'refund','CNY',500,7,36000,36500,'2026-12-02T03:00:00+00');

INSERT INTO app.product
(product_id,name,category,status,store_id,data_batch_id,batch_generation)
VALUES
('P1','Alpha','cat_a','active','store_a','batch_1',1),
('P2','Beta','cat_b','active','store_a','batch_1',1),
('P3','Gamma','cat_c','inactive','store_a','batch_1',1);

INSERT INTO app.sku
(sku_id,product_id,price_minor,currency,price_version,status,updated_at,store_id,data_batch_id,batch_generation)
VALUES
('sku_1','P1',10000,'CNY',3,'active','2026-11-01T00:00:00+00','store_a','batch_1',1),
('sku_2','P2',20000,'CNY',1,'active','2026-11-01T00:00:00+00','store_a','batch_1',1),
('sku_3','P3',30000,'CNY',2,'active','2026-11-01T00:00:00+00','store_a','batch_1',1),
('sku_4','P1',15000,'CNY',4,'active','2026-11-01T00:00:00+00','store_a','batch_1',1);

INSERT INTO app.inventory
(sku_id,stock_qty,reserved_qty,store_id,data_batch_id,batch_generation)
VALUES
('sku_1',10,2,'store_a','batch_1',1),
('sku_2',5,0,'store_a','batch_1',1),
('sku_3',7,1,'store_a','batch_1',1),
('sku_4',2,0,'store_a','batch_1',1);
