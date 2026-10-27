"""B1业务表定义；迁移保存生成后复核的独立DDL快照，不导入本模块执行历史迁移。"""

from datetime import UTC, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Table,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

from cs_agent.db.models import Base

MAX_MINOR = 2**63 - 1


def timestamp():
    return datetime.now(UTC)


def col(name, type_=String(80), *, nullable=False, **kwargs):
    return Column(name, type_, nullable=nullable, **kwargs)


def business_table(name, *columns, constraints=()):
    return Table(
        name,
        Base.metadata,
        *columns,
        col("store_id", String(80)),
        col("source_kind", String(32), default="derived"),
        col("data_batch_id", String(80)),
        col("batch_generation", Integer),
        col("version", Integer, default=1, server_default="1"),
        col("created_at", DateTime(timezone=True), default=timestamp, server_default=func.now()),
        col(
            "updated_at",
            DateTime(timezone=True),
            default=timestamp,
            server_default=func.now(),
            onupdate=timestamp,
        ),
        ForeignKeyConstraint(["store_id"], ["store.store_id"]),
        ForeignKeyConstraint(
            ["data_batch_id", "batch_generation"],
            ["source_batch.batch_id", "source_batch.generation"],
        ),
        CheckConstraint("version > 0"),
        CheckConstraint("source_kind IN ('synthetic','derived','business_import')"),
        *constraints,
    )


operations = business_table(
    "business_operation",
    col("operation_id", primary_key=True),
    col("actor_id", String(80)),
    col("actor_role", String(32)),
    col("action", String(60)),
    col("idempotency_key", String(128)),
    col("parameters_digest", String(64)),
    col("resource_ref", nullable=True),
    col("confirmation_id", nullable=True),
    col("status", String(32)),
    col("result", JSONB, default=dict),
    col("http_status", Integer, default=200),
    constraints=(
        UniqueConstraint(
            "actor_id",
            "actor_role",
            "store_id",
            "data_batch_id",
            "batch_generation",
            "action",
            "idempotency_key",
        ),
        UniqueConstraint("confirmation_id"),
        ForeignKeyConstraint(["actor_id"], ["subject.subject_id"]),
        CheckConstraint("status IN ('accepted','succeeded','rejected')"),
    ),
)

confirmations = business_table(
    "action_confirmation",
    col("confirmation_id", primary_key=True),
    col("owner_id"),
    col("actor_role", String(32)),
    col("auth_version", Integer),
    col("action", String(60)),
    col("resource_ref"),
    col("parameters", JSONB),
    col("parameters_digest", String(64)),
    col("summary", JSONB),
    col("expected_version", Integer),
    col("expires_at", DateTime(timezone=True)),
    col("status", String(16), default="pending"),
    col("confirmed_at", DateTime(timezone=True), nullable=True),
    constraints=(
        ForeignKeyConstraint(["owner_id"], ["subject.subject_id"]),
        CheckConstraint("status IN ('pending','confirmed','cancelled')"),
    ),
)

wallets = business_table(
    "wallet_account",
    col("account_id", primary_key=True),
    col("owner_id"),
    col("currency", String(3), default="CNY"),
    col("balance_minor", BigInteger, default=0),
    col("ledger_sequence", BigInteger, default=0),
    col("status", String(16), default="active"),
    constraints=(
        UniqueConstraint("owner_id", "store_id"),
        ForeignKeyConstraint(["owner_id"], ["subject.subject_id"]),
        CheckConstraint("currency='CNY' AND balance_minor>=0 AND ledger_sequence>=0"),
        CheckConstraint("status IN ('active','frozen')"),
    ),
)

wallet_entries = business_table(
    "wallet_entry",
    col("entry_id", primary_key=True),
    col("account_id"),
    col("owner_id"),
    col("operation_id", nullable=True),
    col("sequence", BigInteger),
    col("kind", String(24)),
    col("currency", String(3), default="CNY"),
    col("amount_minor", BigInteger),
    col("balance_before_minor", BigInteger),
    col("balance_after_minor", BigInteger),
    col("business_ref"),
    col("occurred_at", DateTime(timezone=True)),
    col("recorded_at", DateTime(timezone=True)),
    constraints=(
        UniqueConstraint("operation_id"),
        UniqueConstraint("account_id", "sequence"),
        ForeignKeyConstraint(["account_id"], ["wallet_account.account_id"]),
        ForeignKeyConstraint(["operation_id"], ["business_operation.operation_id"]),
        CheckConstraint("kind IN ('initialization','topup','payment','refund') AND currency='CNY'"),
        CheckConstraint("balance_before_minor>=0 AND balance_after_minor>=0"),
        CheckConstraint(
            "(kind='initialization' AND amount_minor=0 AND sequence=0 AND balance_before_minor=0 AND balance_after_minor=0) OR (kind<>'initialization' AND amount_minor>0 AND sequence>0)"
        ),
        CheckConstraint(
            "(kind='payment' AND balance_after_minor=balance_before_minor-amount_minor) OR (kind<>'payment' AND balance_after_minor=balance_before_minor+amount_minor)"
        ),
    ),
)

outbox = business_table(
    "business_outbox",
    col("event_id", primary_key=True),
    col("event_type", String(64)),
    col("aggregate_id"),
    col("aggregate_version", Integer),
    col("business_ref"),
    col("dedupe_key", String(160)),
    col("contract_version", String(32)),
    col("payload", JSONB),
    col("occurred_at", DateTime(timezone=True)),
    col("recorded_at", DateTime(timezone=True)),
    col("status", String(16), default="pending"),
    constraints=(
        UniqueConstraint("dedupe_key"),
        CheckConstraint("status IN ('pending','delivered')"),
    ),
)

products = business_table(
    "product",
    col("product_id", primary_key=True),
    col("name", String(160)),
    col("category", String(80)),
    col("description", String(2000)),
    col("aliases", JSONB, default=list),
    col("image_slug", String(80)),
    col("status", String(16), default="active"),
    col("source_revision", String(64)),
    col("source_fingerprint", String(64)),
    col("external_id", nullable=True),
    col("supplier_ref", nullable=True),
    constraints=(CheckConstraint("status IN ('active','inactive','draft')"),),
)

skus = business_table(
    "sku",
    col("sku_id", primary_key=True),
    col("product_id"),
    col("specifications", JSONB),
    col("price_minor", BigInteger),
    col("currency", String(3), default="CNY"),
    col("cost_minor", BigInteger, nullable=True),
    col("price_version", Integer, default=1),
    col("status", String(16), default="active"),
    constraints=(
        ForeignKeyConstraint(["product_id"], ["product.product_id"]),
        CheckConstraint(
            "currency='CNY' AND price_minor>0 AND (cost_minor IS NULL OR cost_minor>=0)"
        ),
        CheckConstraint("price_version>0 AND status IN ('active','inactive')"),
    ),
)

inventory = business_table(
    "inventory",
    col("sku_id", primary_key=True),
    col("stock_qty", Integer),
    col("reserved_qty", Integer, default=0),
    constraints=(
        ForeignKeyConstraint(["sku_id"], ["sku.sku_id"]),
        CheckConstraint("stock_qty>=reserved_qty AND reserved_qty>=0"),
    ),
)

stock_movements = business_table(
    "stock_movement",
    col("movement_id", primary_key=True),
    col("sku_id"),
    col("operation_id", nullable=True),
    col("business_ref"),
    col("kind", String(32)),
    col("stock_delta", Integer),
    col("reserved_delta", Integer),
    col("stock_before", Integer),
    col("stock_after", Integer),
    col("reserved_before", Integer),
    col("reserved_after", Integer),
    col("reason", String(160)),
    col("occurred_at", DateTime(timezone=True)),
    col("recorded_at", DateTime(timezone=True)),
    constraints=(
        ForeignKeyConstraint(["sku_id"], ["sku.sku_id"]),
        ForeignKeyConstraint(["operation_id"], ["business_operation.operation_id"]),
        UniqueConstraint("operation_id", "sku_id", "kind"),
        CheckConstraint(
            "stock_after=stock_before+stock_delta AND reserved_after=reserved_before+reserved_delta"
        ),
        CheckConstraint("stock_after>=reserved_after AND reserved_after>=0"),
    ),
)

carts = business_table(
    "cart",
    col("cart_id", primary_key=True),
    col("owner_id"),
    constraints=(
        UniqueConstraint("owner_id", "store_id"),
        ForeignKeyConstraint(["owner_id"], ["subject.subject_id"]),
    ),
)
cart_items = business_table(
    "cart_item",
    col("item_id", primary_key=True),
    col("cart_id"),
    col("sku_id"),
    col("qty", Integer),
    col("selected", Boolean, default=True),
    constraints=(
        ForeignKeyConstraint(["cart_id"], ["cart.cart_id"]),
        ForeignKeyConstraint(["sku_id"], ["sku.sku_id"]),
        UniqueConstraint("cart_id", "sku_id"),
        CheckConstraint("qty>0"),
    ),
)

quotes = business_table(
    "checkout_quote",
    col("quote_id", primary_key=True),
    col("owner_id"),
    col("shipping_input_id"),
    col("lines", JSONB),
    col("cart_bindings", JSONB),
    col("currency", String(3), default="CNY"),
    col("goods_minor", BigInteger),
    col("discount_minor", BigInteger),
    col("freight_minor", BigInteger),
    col("payable_minor", BigInteger),
    col("policy_snapshot", JSONB),
    col("expires_at", DateTime(timezone=True)),
    col("status", String(16), default="active"),
    constraints=(
        ForeignKeyConstraint(["owner_id"], ["subject.subject_id"]),
        ForeignKeyConstraint(["shipping_input_id"], ["shipping_input.input_id"]),
        CheckConstraint(
            "currency='CNY' AND goods_minor>0 AND discount_minor>=0 AND freight_minor>=0 AND payable_minor=goods_minor-discount_minor+freight_minor"
        ),
        CheckConstraint("status IN ('active','consumed','expired')"),
    ),
)

orders = business_table(
    "customer_order",
    col("order_id", primary_key=True),
    col("owner_id"),
    col("display_no", String(40)),
    col("quote_id"),
    col("trade_status", String(32)),
    col("fulfillment_status", String(32)),
    col("currency", String(3), default="CNY"),
    col("goods_minor", BigInteger),
    col("discount_minor", BigInteger),
    col("freight_minor", BigInteger),
    col("payable_minor", BigInteger),
    col("paid_minor", BigInteger, default=0),
    col("refunded_minor", BigInteger, default=0),
    col("refunded_goods_minor", BigInteger, default=0),
    col("refunded_freight_minor", BigInteger, default=0),
    col("policy_snapshot", JSONB),
    col("current_shipping_id"),
    col("change_count", Integer, default=0),
    col("payment_expires_at", DateTime(timezone=True)),
    col("paid_at", DateTime(timezone=True), nullable=True),
    col("shipped_at", DateTime(timezone=True), nullable=True),
    col("received_at", DateTime(timezone=True), nullable=True),
    col("cancelled_at", DateTime(timezone=True), nullable=True),
    col("cancellation_reason", String(80), nullable=True),
    constraints=(
        UniqueConstraint("display_no"),
        UniqueConstraint("quote_id"),
        ForeignKeyConstraint(["owner_id"], ["subject.subject_id"]),
        ForeignKeyConstraint(["quote_id"], ["checkout_quote.quote_id"]),
        CheckConstraint(
            "currency='CNY' AND payable_minor=goods_minor-discount_minor+freight_minor AND goods_minor>0 AND discount_minor>=0 AND freight_minor>=0"
        ),
        CheckConstraint(
            "paid_minor>=0 AND paid_minor<=payable_minor AND refunded_minor=refunded_goods_minor+refunded_freight_minor AND refunded_minor>=0 AND refunded_minor<=paid_minor"
        ),
        CheckConstraint("change_count IN (0,1)"),
        CheckConstraint(
            "trade_status IN ('awaiting_payment','paid','completed','cancelled_unpaid','cancelled_refunded','closed_refunded')"
        ),
        CheckConstraint("fulfillment_status IN ('unshipped','in_transit','received_by_customer')"),
    ),
)

order_items = business_table(
    "order_item",
    col("order_item_id", primary_key=True),
    col("order_id"),
    col("sku_id"),
    col("product_name", String(160)),
    col("specifications", JSONB),
    col("image_slug", String(80)),
    col("unit_price_minor", BigInteger),
    col("ordered_qty", Integer),
    col("goods_minor", BigInteger),
    col("discount_minor", BigInteger),
    col("allocated_minor", BigInteger),
    col("paid_goods_minor", BigInteger),
    col("unit_allocations", JSONB),
    col("allocation_revision", String(40)),
    col("refunded_qty", Integer, default=0),
    col("reserved_refund_qty", Integer, default=0),
    col("refunded_goods_minor", BigInteger, default=0),
    col("reserved_refund_minor", BigInteger, default=0),
    constraints=(
        ForeignKeyConstraint(["order_id"], ["customer_order.order_id"]),
        ForeignKeyConstraint(["sku_id"], ["sku.sku_id"]),
        CheckConstraint(
            "ordered_qty>0 AND unit_price_minor>0 AND goods_minor=unit_price_minor*ordered_qty AND allocated_minor=goods_minor-discount_minor AND paid_goods_minor>=0 AND paid_goods_minor<=allocated_minor"
        ),
        CheckConstraint(
            "refunded_qty>=0 AND reserved_refund_qty>=0 AND refunded_qty+reserved_refund_qty<=ordered_qty"
        ),
        CheckConstraint(
            "refunded_goods_minor>=0 AND reserved_refund_minor>=0 AND refunded_goods_minor+reserved_refund_minor<=paid_goods_minor"
        ),
    ),
)

shipping_snapshots = business_table(
    "order_shipping_snapshot",
    col("snapshot_id", primary_key=True),
    col("order_id"),
    col("owner_id"),
    col("input_id"),
    col("recipient", String(100)),
    col("phone", String(32)),
    col("province", String(80)),
    col("city", String(80)),
    col("district", String(80)),
    col("street", String(300)),
    col("postal_code", String(16), nullable=True),
    constraints=(
        ForeignKeyConstraint(["order_id"], ["customer_order.order_id"]),
        ForeignKeyConstraint(["input_id"], ["shipping_input.input_id"]),
        UniqueConstraint("order_id", "version"),
    ),
)

reservations = business_table(
    "stock_reservation",
    col("reservation_id", primary_key=True),
    col("order_id"),
    col("sku_id"),
    col("qty", Integer),
    col("status", String(16)),
    col("expires_at", DateTime(timezone=True)),
    constraints=(
        ForeignKeyConstraint(["order_id"], ["customer_order.order_id"]),
        ForeignKeyConstraint(["sku_id"], ["sku.sku_id"]),
        UniqueConstraint("order_id", "sku_id"),
        CheckConstraint("qty>0 AND status IN ('reserved','consumed','released')"),
    ),
)

payments = business_table(
    "order_payment",
    col("payment_id", primary_key=True),
    col("order_id"),
    col("operation_id"),
    col("entry_id"),
    col("amount_minor", BigInteger),
    col("currency", String(3), default="CNY"),
    col("status", String(16)),
    col("paid_at", DateTime(timezone=True)),
    constraints=(
        ForeignKeyConstraint(["order_id"], ["customer_order.order_id"]),
        ForeignKeyConstraint(["operation_id"], ["business_operation.operation_id"]),
        ForeignKeyConstraint(["entry_id"], ["wallet_entry.entry_id"]),
        UniqueConstraint("order_id"),
        UniqueConstraint("operation_id"),
        CheckConstraint("amount_minor>0 AND currency='CNY' AND status='succeeded'"),
    ),
)

shipments = business_table(
    "order_shipment",
    col("shipment_id", primary_key=True),
    col("order_id"),
    col("operation_id"),
    col("shipping_snapshot_id"),
    col("carrier", String(60)),
    col("tracking_no", String(80)),
    col("status", String(24)),
    col("shipped_at", DateTime(timezone=True)),
    col("carrier_signed_at", DateTime(timezone=True), nullable=True),
    col("tracking_updated_at", DateTime(timezone=True)),
    col("tracking_events", JSONB),
    constraints=(
        ForeignKeyConstraint(["order_id"], ["customer_order.order_id"]),
        ForeignKeyConstraint(["operation_id"], ["business_operation.operation_id"]),
        ForeignKeyConstraint(["shipping_snapshot_id"], ["order_shipping_snapshot.snapshot_id"]),
        UniqueConstraint("order_id"),
        UniqueConstraint("operation_id"),
        CheckConstraint("status IN ('in_transit','carrier_signed')"),
    ),
)
shipment_items = business_table(
    "shipment_item",
    col("shipment_item_id", primary_key=True),
    col("shipment_id"),
    col("order_item_id"),
    col("qty", Integer),
    constraints=(
        ForeignKeyConstraint(["shipment_id"], ["order_shipment.shipment_id"]),
        ForeignKeyConstraint(["order_item_id"], ["order_item.order_item_id"]),
        UniqueConstraint("shipment_id", "order_item_id"),
        CheckConstraint("qty>0"),
    ),
)

address_changes = business_table(
    "order_address_change",
    col("change_id", primary_key=True),
    col("order_id"),
    col("operation_id"),
    col("confirmation_id"),
    col("old_snapshot_id"),
    col("new_snapshot_id"),
    col("changed_at", DateTime(timezone=True)),
    constraints=(
        ForeignKeyConstraint(["order_id"], ["customer_order.order_id"]),
        ForeignKeyConstraint(["operation_id"], ["business_operation.operation_id"]),
        ForeignKeyConstraint(["confirmation_id"], ["action_confirmation.confirmation_id"]),
        ForeignKeyConstraint(["old_snapshot_id"], ["order_shipping_snapshot.snapshot_id"]),
        ForeignKeyConstraint(["new_snapshot_id"], ["order_shipping_snapshot.snapshot_id"]),
        UniqueConstraint("order_id"),
        UniqueConstraint("operation_id"),
    ),
)
receipts = business_table(
    "customer_receipt",
    col("receipt_id", primary_key=True),
    col("order_id"),
    col("owner_id"),
    col("operation_id"),
    col("confirmed_at", DateTime(timezone=True)),
    constraints=(
        ForeignKeyConstraint(["order_id"], ["customer_order.order_id"]),
        ForeignKeyConstraint(["operation_id"], ["business_operation.operation_id"]),
        UniqueConstraint("order_id"),
        UniqueConstraint("operation_id"),
    ),
)

# 当前地址必须属于同一订单；建单与地址快照在一个事务内写入，提交时复核。
shipping_snapshots.append_constraint(
    UniqueConstraint("snapshot_id", "order_id", name="uq_shipping_snapshot_order")
)
orders.append_constraint(
    ForeignKeyConstraint(
        ["current_shipping_id", "order_id"],
        ["order_shipping_snapshot.snapshot_id", "order_shipping_snapshot.order_id"],
        name="fk_order_current_shipping",
        use_alter=True,
        deferrable=True,
        initially="DEFERRED",
    )
)
operations.append_constraint(
    ForeignKeyConstraint(
        ["confirmation_id"],
        ["action_confirmation.confirmation_id"],
        name="fk_operation_confirmation",
    )
)
Index("ix_order_owner_created", orders.c.owner_id, orders.c.store_id, orders.c.created_at)
Index(
    "ix_order_unpaid_deadline",
    orders.c.payment_expires_at,
    postgresql_where=text("trade_status='awaiting_payment'"),
)
Index("ix_order_item_order", order_items.c.order_id)
Index("ix_sku_product", skus.c.product_id)

# 注册顺序保留已验导入路径，活动表依赖本文件的基础表，不能上移到模块头。
from cs_agent.db import aftersales_tables as _aftersales_tables  # noqa: E402,F401,I001
from cs_agent.db import agent_action_tables as _agent_action_tables  # noqa: E402,F401
from cs_agent.db import cancellation_tables as _cancellation_tables  # noqa: E402,F401
from cs_agent.db import experience_tables as _experience_tables  # noqa: E402,F401
from cs_agent.db import knowledge_tables as _knowledge_tables  # noqa: E402,F401
from cs_agent.db import runtime_tables as _runtime_tables  # noqa: E402,F401
from cs_agent.db import support_tables as _support_tables  # noqa: E402,F401
from cs_agent.db import activity_tables as _activity_tables  # noqa: E402,F401
from cs_agent.db import operations_identity_tables as _operations_identity_tables  # noqa: E402,F401
from cs_agent.operations_snapshot import tables as _operations_snapshot_tables  # noqa: E402,F401
