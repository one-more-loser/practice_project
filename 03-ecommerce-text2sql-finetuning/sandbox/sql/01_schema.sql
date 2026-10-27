-- 03 isolated PostgreSQL sandbox, mirroring the 01 B2 six-view surface.
-- Not a copy of 01 production; no 01/02 DSN, credentials, or data.
DROP SCHEMA IF EXISTS app CASCADE;
CREATE SCHEMA app;

CREATE TABLE app.store (
  store_id text PRIMARY KEY,
  status text NOT NULL CHECK (status IN ('active','inactive'))
);

CREATE TABLE app.source_batch (
  batch_id text NOT NULL,
  generation integer NOT NULL CHECK (generation > 0),
  status text NOT NULL CHECK (status IN ('active','inactive')),
  PRIMARY KEY (batch_id, generation)
);

CREATE TABLE app.subject (
  subject_id text PRIMARY KEY
);

CREATE TABLE app.customer_order (
  order_id text PRIMARY KEY,
  owner_id text NOT NULL REFERENCES app.subject(subject_id),
  store_id text NOT NULL REFERENCES app.store(store_id),
  data_batch_id text NOT NULL,
  batch_generation integer NOT NULL,
  created_at timestamptz NOT NULL,
  paid_at timestamptz,
  received_at timestamptz,
  trade_status text NOT NULL CHECK (trade_status IN
    ('awaiting_payment','paid','completed','cancelled_unpaid','cancelled_refunded','closed_refunded')),
  fulfillment_status text NOT NULL CHECK (fulfillment_status IN
    ('unshipped','in_transit','received_by_customer')),
  goods_minor bigint NOT NULL CHECK (goods_minor > 0),
  discount_minor bigint NOT NULL DEFAULT 0 CHECK (discount_minor >= 0),
  freight_minor bigint NOT NULL DEFAULT 0 CHECK (freight_minor >= 0),
  payable_minor bigint NOT NULL,
  paid_minor bigint NOT NULL DEFAULT 0,
  refunded_minor bigint NOT NULL DEFAULT 0,
  refunded_goods_minor bigint NOT NULL DEFAULT 0,
  refunded_freight_minor bigint NOT NULL DEFAULT 0,
  currency text NOT NULL DEFAULT 'CNY' CHECK (currency = 'CNY'),
  CONSTRAINT order_amounts CHECK (
    payable_minor = goods_minor - discount_minor + freight_minor
    AND paid_minor >= 0 AND paid_minor <= payable_minor
    AND refunded_minor >= 0 AND refunded_minor <= paid_minor
    AND refunded_goods_minor >= 0 AND refunded_freight_minor >= 0
    AND refunded_minor = refunded_goods_minor + refunded_freight_minor
  ),
  FOREIGN KEY (data_batch_id, batch_generation)
    REFERENCES app.source_batch(batch_id, generation)
);

CREATE TABLE app.order_item (
  order_item_id text PRIMARY KEY,
  order_id text NOT NULL REFERENCES app.customer_order(order_id),
  sku_id text NOT NULL,
  product_name text NOT NULL,
  store_id text NOT NULL REFERENCES app.store(store_id),
  data_batch_id text NOT NULL,
  batch_generation integer NOT NULL,
  unit_price_minor bigint NOT NULL CHECK (unit_price_minor > 0),
  ordered_qty integer NOT NULL CHECK (ordered_qty > 0),
  goods_minor bigint NOT NULL CHECK (goods_minor > 0),
  discount_minor bigint NOT NULL DEFAULT 0 CHECK (discount_minor >= 0),
  allocated_minor bigint NOT NULL,
  paid_goods_minor bigint NOT NULL DEFAULT 0,
  refunded_qty integer NOT NULL DEFAULT 0,
  reserved_refund_qty integer NOT NULL DEFAULT 0,
  refunded_goods_minor bigint NOT NULL DEFAULT 0,
  reserved_refund_minor bigint NOT NULL DEFAULT 0,
  CONSTRAINT item_amounts CHECK (
    goods_minor = unit_price_minor * ordered_qty
    AND allocated_minor = goods_minor - discount_minor
    AND paid_goods_minor >= 0 AND paid_goods_minor <= allocated_minor
    AND refunded_qty >= 0 AND reserved_refund_qty >= 0
    AND refunded_qty + reserved_refund_qty <= ordered_qty
    AND refunded_goods_minor >= 0 AND refunded_goods_minor <= paid_goods_minor
    AND reserved_refund_minor >= 0
    AND refunded_goods_minor + reserved_refund_minor <= paid_goods_minor
  ),
  FOREIGN KEY (data_batch_id, batch_generation)
    REFERENCES app.source_batch(batch_id, generation)
);

CREATE TABLE app.order_shipment (
  shipment_id text PRIMARY KEY,
  order_id text NOT NULL UNIQUE REFERENCES app.customer_order(order_id),
  carrier text NOT NULL,
  status text NOT NULL CHECK (status IN ('in_transit','carrier_signed')),
  shipped_at timestamptz NOT NULL,
  carrier_signed_at timestamptz,
  tracking_updated_at timestamptz,
  store_id text NOT NULL REFERENCES app.store(store_id),
  data_batch_id text NOT NULL,
  batch_generation integer NOT NULL,
  FOREIGN KEY (data_batch_id, batch_generation)
    REFERENCES app.source_batch(batch_id, generation)
);

CREATE TABLE app.wallet_account (
  account_id text PRIMARY KEY,
  owner_id text NOT NULL REFERENCES app.subject(subject_id),
  store_id text NOT NULL REFERENCES app.store(store_id),
  data_batch_id text NOT NULL,
  batch_generation integer NOT NULL,
  balance_minor bigint NOT NULL DEFAULT 0 CHECK (balance_minor >= 0),
  currency text NOT NULL DEFAULT 'CNY' CHECK (currency = 'CNY'),
  status text NOT NULL CHECK (status IN ('active','frozen')),
  ledger_sequence bigint NOT NULL DEFAULT 0 CHECK (ledger_sequence >= 0),
  UNIQUE (owner_id, store_id),
  FOREIGN KEY (data_batch_id, batch_generation)
    REFERENCES app.source_batch(batch_id, generation)
);

CREATE TABLE app.wallet_entry (
  entry_id text PRIMARY KEY,
  account_id text NOT NULL REFERENCES app.wallet_account(account_id),
  owner_id text NOT NULL REFERENCES app.subject(subject_id),
  store_id text NOT NULL REFERENCES app.store(store_id),
  data_batch_id text NOT NULL,
  batch_generation integer NOT NULL,
  kind text NOT NULL CHECK (kind IN ('initialization','topup','payment','refund')),
  currency text NOT NULL DEFAULT 'CNY' CHECK (currency = 'CNY'),
  amount_minor bigint NOT NULL,
  sequence bigint NOT NULL,
  balance_before_minor bigint NOT NULL CHECK (balance_before_minor >= 0),
  balance_after_minor bigint NOT NULL CHECK (balance_after_minor >= 0),
  occurred_at timestamptz NOT NULL,
  UNIQUE (account_id, sequence),
  CONSTRAINT wallet_amount CHECK (
    (kind = 'initialization' AND amount_minor = 0 AND sequence = 0
      AND balance_before_minor = 0 AND balance_after_minor = 0)
    OR (kind <> 'initialization' AND amount_minor > 0 AND sequence > 0)
  ),
  CONSTRAINT wallet_direction CHECK (
    (kind = 'payment' AND balance_after_minor = balance_before_minor - amount_minor)
    OR (kind <> 'payment' AND balance_after_minor = balance_before_minor + amount_minor)
  ),
  FOREIGN KEY (data_batch_id, batch_generation)
    REFERENCES app.source_batch(batch_id, generation)
);

CREATE TABLE app.product (
  product_id text PRIMARY KEY,
  name text NOT NULL,
  category text NOT NULL,
  status text NOT NULL CHECK (status IN ('active','inactive')),
  store_id text NOT NULL REFERENCES app.store(store_id),
  data_batch_id text NOT NULL,
  batch_generation integer NOT NULL,
  FOREIGN KEY (data_batch_id, batch_generation)
    REFERENCES app.source_batch(batch_id, generation)
);

CREATE TABLE app.sku (
  sku_id text PRIMARY KEY,
  product_id text NOT NULL REFERENCES app.product(product_id),
  price_minor bigint NOT NULL CHECK (price_minor > 0),
  currency text NOT NULL DEFAULT 'CNY' CHECK (currency = 'CNY'),
  price_version integer NOT NULL CHECK (price_version > 0),
  status text NOT NULL CHECK (status IN ('active','inactive')),
  updated_at timestamptz NOT NULL,
  store_id text NOT NULL REFERENCES app.store(store_id),
  data_batch_id text NOT NULL,
  batch_generation integer NOT NULL,
  FOREIGN KEY (data_batch_id, batch_generation)
    REFERENCES app.source_batch(batch_id, generation)
);

CREATE TABLE app.inventory (
  sku_id text PRIMARY KEY REFERENCES app.sku(sku_id),
  stock_qty integer NOT NULL CHECK (stock_qty >= 0),
  reserved_qty integer NOT NULL DEFAULT 0 CHECK (reserved_qty >= 0),
  store_id text NOT NULL REFERENCES app.store(store_id),
  data_batch_id text NOT NULL,
  batch_generation integer NOT NULL,
  CHECK (reserved_qty <= stock_qty),
  FOREIGN KEY (data_batch_id, batch_generation)
    REFERENCES app.source_batch(batch_id, generation)
);

-- Session-scoped identity/scope variables, as in 01 migration 0011.
-- cs.owner, cs.store, cs.batch, cs.generation, cs.role are set by the
-- evaluator per transaction; the model never receives or sets them.
ALTER TABLE app.customer_order ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.customer_order FORCE ROW LEVEL SECURITY;
ALTER TABLE app.order_item ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.order_item FORCE ROW LEVEL SECURITY;
ALTER TABLE app.order_shipment ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.order_shipment FORCE ROW LEVEL SECURITY;
ALTER TABLE app.wallet_account ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.wallet_account FORCE ROW LEVEL SECURITY;
ALTER TABLE app.wallet_entry ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.wallet_entry FORCE ROW LEVEL SECURITY;
ALTER TABLE app.product ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.product FORCE ROW LEVEL SECURITY;
ALTER TABLE app.sku ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.sku FORCE ROW LEVEL SECURITY;
ALTER TABLE app.inventory ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.inventory FORCE ROW LEVEL SECURITY;

-- Policies synchronized to 01 migration 0012 (query scope), with the
-- complete trusted context: owner/store/batch/generation/role plus
-- cs.start/cs.end. 0012's RLS window is closed [start, end]; business SQL
-- still uses half-open [start_at, end_at), so the effective result is the
-- intersection. The old 0011 policy and its RED evidence remain in
-- results/ and are not overwritten.
CREATE POLICY q_common ON app.customer_order FOR SELECT TO csq_agent_query USING (
  app.customer_order.store_id = current_setting('cs.store', true)
  AND app.customer_order.data_batch_id = current_setting('cs.batch', true)
  AND app.customer_order.batch_generation::text = current_setting('cs.generation', true)
  AND EXISTS (SELECT 1 FROM app.source_batch b
              WHERE b.batch_id = app.customer_order.data_batch_id
                AND b.generation = app.customer_order.batch_generation
                AND b.status = 'active')
  AND EXISTS (SELECT 1 FROM app.store s
              WHERE s.store_id = app.customer_order.store_id
                AND s.status = 'active')
  AND app.customer_order.owner_id = current_setting('cs.owner', true)
  AND current_setting('cs.role', true) = 'customer'
  AND app.customer_order.created_at >= nullif(current_setting('cs.start', true), '')::timestamptz
  AND app.customer_order.created_at <= nullif(current_setting('cs.end', true), '')::timestamptz
);

CREATE POLICY q_order_item ON app.order_item FOR SELECT TO csq_agent_query USING (
  app.order_item.store_id = current_setting('cs.store', true)
  AND app.order_item.data_batch_id = current_setting('cs.batch', true)
  AND app.order_item.batch_generation::text = current_setting('cs.generation', true)
  AND EXISTS (SELECT 1 FROM app.source_batch b
              WHERE b.batch_id = app.order_item.data_batch_id
                AND b.generation = app.order_item.batch_generation
                AND b.status = 'active')
  AND EXISTS (SELECT 1 FROM app.store s
              WHERE s.store_id = app.order_item.store_id
                AND s.status = 'active')
  AND app.order_item.order_id IN (SELECT order_id FROM app.customer_order)
  AND current_setting('cs.role', true) = 'customer'
);

CREATE POLICY q_shipment ON app.order_shipment FOR SELECT TO csq_agent_query USING (
  app.order_shipment.store_id = current_setting('cs.store', true)
  AND app.order_shipment.data_batch_id = current_setting('cs.batch', true)
  AND app.order_shipment.batch_generation::text = current_setting('cs.generation', true)
  AND EXISTS (SELECT 1 FROM app.source_batch b
              WHERE b.batch_id = app.order_shipment.data_batch_id
                AND b.generation = app.order_shipment.batch_generation
                AND b.status = 'active')
  AND EXISTS (SELECT 1 FROM app.store s
              WHERE s.store_id = app.order_shipment.store_id
                AND s.status = 'active')
  AND app.order_shipment.order_id IN (SELECT order_id FROM app.customer_order)
  AND current_setting('cs.role', true) = 'customer'
);

CREATE POLICY q_wallet_account ON app.wallet_account FOR SELECT TO csq_agent_query USING (
  app.wallet_account.store_id = current_setting('cs.store', true)
  AND app.wallet_account.data_batch_id = current_setting('cs.batch', true)
  AND app.wallet_account.batch_generation::text = current_setting('cs.generation', true)
  AND EXISTS (SELECT 1 FROM app.source_batch b
              WHERE b.batch_id = app.wallet_account.data_batch_id
                AND b.generation = app.wallet_account.batch_generation
                AND b.status = 'active')
  AND EXISTS (SELECT 1 FROM app.store s
              WHERE s.store_id = app.wallet_account.store_id
                AND s.status = 'active')
  AND app.wallet_account.owner_id = current_setting('cs.owner', true)
  AND current_setting('cs.role', true) = 'customer'
);

CREATE POLICY q_wallet_entry ON app.wallet_entry FOR SELECT TO csq_agent_query USING (
  app.wallet_entry.store_id = current_setting('cs.store', true)
  AND app.wallet_entry.data_batch_id = current_setting('cs.batch', true)
  AND app.wallet_entry.batch_generation::text = current_setting('cs.generation', true)
  AND EXISTS (SELECT 1 FROM app.source_batch b
              WHERE b.batch_id = app.wallet_entry.data_batch_id
                AND b.generation = app.wallet_entry.batch_generation
                AND b.status = 'active')
  AND EXISTS (SELECT 1 FROM app.store s
              WHERE s.store_id = app.wallet_entry.store_id
                AND s.status = 'active')
  AND app.wallet_entry.owner_id = current_setting('cs.owner', true)
  AND current_setting('cs.role', true) = 'customer'
  AND app.wallet_entry.occurred_at >= nullif(current_setting('cs.start', true), '')::timestamptz
  AND app.wallet_entry.occurred_at <= nullif(current_setting('cs.end', true), '')::timestamptz
);

CREATE POLICY q_product ON app.product FOR SELECT TO csq_agent_query USING (
  app.product.store_id = current_setting('cs.store', true)
  AND app.product.data_batch_id = current_setting('cs.batch', true)
  AND app.product.batch_generation::text = current_setting('cs.generation', true)
  AND EXISTS (SELECT 1 FROM app.source_batch b
              WHERE b.batch_id = app.product.data_batch_id
                AND b.generation = app.product.batch_generation
                AND b.status = 'active')
  AND EXISTS (SELECT 1 FROM app.store s
              WHERE s.store_id = app.product.store_id
                AND s.status = 'active')
);

CREATE POLICY q_sku ON app.sku FOR SELECT TO csq_agent_query USING (
  app.sku.store_id = current_setting('cs.store', true)
  AND app.sku.data_batch_id = current_setting('cs.batch', true)
  AND app.sku.batch_generation::text = current_setting('cs.generation', true)
  AND EXISTS (SELECT 1 FROM app.source_batch b
              WHERE b.batch_id = app.sku.data_batch_id
                AND b.generation = app.sku.batch_generation
                AND b.status = 'active')
  AND EXISTS (SELECT 1 FROM app.store s
              WHERE s.store_id = app.sku.store_id
                AND s.status = 'active')
);

CREATE POLICY q_inventory ON app.inventory FOR SELECT TO csq_agent_query USING (
  app.inventory.store_id = current_setting('cs.store', true)
  AND app.inventory.data_batch_id = current_setting('cs.batch', true)
  AND app.inventory.batch_generation::text = current_setting('cs.generation', true)
  AND EXISTS (SELECT 1 FROM app.source_batch b
              WHERE b.batch_id = app.inventory.data_batch_id
                AND b.generation = app.inventory.batch_generation
                AND b.status = 'active')
  AND EXISTS (SELECT 1 FROM app.store s
              WHERE s.store_id = app.inventory.store_id
                AND s.status = 'active')
);

CREATE VIEW app.cs_order_self WITH (security_invoker=true, security_barrier=true) AS
SELECT order_id, created_at, paid_at, received_at, trade_status, fulfillment_status,
       goods_minor, discount_minor, freight_minor, payable_minor, paid_minor,
       refunded_minor, paid_minor - refunded_minor AS net_paid_minor, currency
FROM app.customer_order;

CREATE VIEW app.cs_order_item_self WITH (security_invoker=true, security_barrier=true) AS
SELECT order_item_id, order_id, sku_id, product_name, ordered_qty, paid_goods_minor,
       refunded_qty, refunded_goods_minor,
       ordered_qty - refunded_qty - reserved_refund_qty AS remaining_qty,
       paid_goods_minor - refunded_goods_minor - reserved_refund_minor AS remaining_goods_minor
FROM app.order_item;

CREATE VIEW app.cs_shipment_self WITH (security_invoker=true, security_barrier=true) AS
SELECT shipment_id, order_id, carrier, status, shipped_at, carrier_signed_at, tracking_updated_at
FROM app.order_shipment;

CREATE VIEW app.cs_wallet_summary_self WITH (security_invoker=true, security_barrier=true) AS
SELECT account_id, balance_minor, currency, status FROM app.wallet_account;

CREATE VIEW app.cs_wallet_entry_self WITH (security_invoker=true, security_barrier=true) AS
SELECT entry_id, kind, amount_minor, sequence, balance_before_minor,
       balance_after_minor, occurred_at, currency
FROM app.wallet_entry;

CREATE VIEW app.cs_product_public WITH (security_invoker=true, security_barrier=true) AS
SELECT p.product_id, s.sku_id, p.name, p.category, s.currency, s.price_minor,
       i.stock_qty - i.reserved_qty AS available_qty, s.price_version, s.updated_at AS as_of
FROM app.product p
JOIN app.sku s ON p.product_id = s.product_id
JOIN app.inventory i ON i.sku_id = s.sku_id
WHERE p.status = 'active' AND s.status = 'active';

REVOKE ALL ON SCHEMA app FROM PUBLIC;
GRANT USAGE ON SCHEMA app TO csq_agent_query;

-- The reader sees only the views plus the two scope tables needed by RLS.
GRANT SELECT ON app.cs_order_self, app.cs_order_item_self, app.cs_shipment_self,
  app.cs_wallet_summary_self, app.cs_wallet_entry_self, app.cs_product_public
  TO csq_agent_query;
GRANT SELECT (store_id, status) ON app.store TO csq_agent_query;
GRANT SELECT (batch_id, generation, status) ON app.source_batch TO csq_agent_query;
GRANT SELECT ON app.customer_order, app.order_item, app.order_shipment,
  app.wallet_account, app.wallet_entry, app.product, app.sku, app.inventory
  TO csq_agent_query;
