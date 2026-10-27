"""收退及运输异常共用退款额度，业务入口仍保持分离。"""

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB

from cs_agent.db.business_tables import business_table, col

claims = business_table(
    "refund_claim",
    col("claim_id", primary_key=True),
    col("case_kind", String(24)),
    col("order_id"),
    col("owner_id"),
    col("ticket_id"),
    col("receipt_id", nullable=True),
    col("confirmation_id"),
    col("operation_id"),
    col("reason", String(24)),
    col("description", String(1000)),
    col("policy_revision", String(160)),
    col("policy_snapshot", JSONB),
    col("goods_minor", BigInteger),
    col("freight_minor", BigInteger),
    col("return_freight_responsibility", String(24)),
    col("status", String(32)),
    col("needs_supervisor", Integer, default=0),
    col("reviewer_id", nullable=True),
    col("approved_at", DateTime(timezone=True), nullable=True),
    col("verified_kind", String(24), nullable=True),
    col("released_at", DateTime(timezone=True), nullable=True),
    constraints=(
        ForeignKeyConstraint(["order_id"], ["customer_order.order_id"]),
        ForeignKeyConstraint(["owner_id"], ["subject.subject_id"]),
        ForeignKeyConstraint(["ticket_id"], ["support_ticket.ticket_id"]),
        ForeignKeyConstraint(["receipt_id"], ["customer_receipt.receipt_id"]),
        ForeignKeyConstraint(["confirmation_id"], ["action_confirmation.confirmation_id"]),
        ForeignKeyConstraint(["operation_id"], ["business_operation.operation_id"]),
        UniqueConstraint("confirmation_id"),
        UniqueConstraint("operation_id"),
        CheckConstraint("case_kind IN ('formal_return','transport_refund')"),
        CheckConstraint("goods_minor>=0 AND freight_minor>=0 AND goods_minor+freight_minor>0"),
        CheckConstraint("needs_supervisor IN (0,1)"),
        CheckConstraint("return_freight_responsibility IN ('customer','shop','pending','none')"),
        CheckConstraint(
            "status IN ('reviewing','awaiting_return','return_in_transit','needs_dispute','refund_reviewed','refunded','rejected','withdrawn')"
        ),
        CheckConstraint(
            "(case_kind='formal_return' AND receipt_id IS NOT NULL) OR (case_kind='transport_refund' AND receipt_id IS NULL)"
        ),
    ),
)
claim_items = business_table(
    "refund_claim_item",
    col("claim_item_id", primary_key=True),
    col("claim_id"),
    col("order_item_id"),
    col("qty", Integer),
    col("goods_minor", BigInteger),
    col("unit_indices", JSONB),
    col("allocation_revision", String(40)),
    constraints=(
        ForeignKeyConstraint(["claim_id"], ["refund_claim.claim_id"]),
        ForeignKeyConstraint(["order_item_id"], ["order_item.order_item_id"]),
        UniqueConstraint("claim_id", "order_item_id"),
        CheckConstraint("qty>0 AND goods_minor>=0"),
    ),
)
reviews = business_table(
    "refund_case_review",
    col("review_id", primary_key=True),
    col("claim_id"),
    col("actor_id"),
    col("decision", String(32)),
    col("basis", String(1000)),
    col("evidence_refs", JSONB),
    col("operation_id"),
    constraints=(
        ForeignKeyConstraint(["claim_id"], ["refund_claim.claim_id"]),
        ForeignKeyConstraint(["actor_id"], ["subject.subject_id"]),
        ForeignKeyConstraint(["operation_id"], ["business_operation.operation_id"]),
        UniqueConstraint("operation_id"),
    ),
)
returns = business_table(
    "aftersale_return_shipment",
    col("return_id", primary_key=True),
    col("claim_id"),
    col("carrier", String(60)),
    col("tracking_no", String(80)),
    col("operation_id"),
    constraints=(
        ForeignKeyConstraint(["claim_id"], ["refund_claim.claim_id"]),
        ForeignKeyConstraint(["operation_id"], ["business_operation.operation_id"]),
        UniqueConstraint("claim_id"),
        UniqueConstraint("operation_id"),
    ),
)
inspections = business_table(
    "return_inspection",
    col("inspection_id", primary_key=True),
    col("claim_id"),
    col("inspector_id"),
    col("lines", JSONB),
    col("evidence_refs", JSONB),
    col("operation_id"),
    col("matched", Integer),
    constraints=(
        ForeignKeyConstraint(["claim_id"], ["refund_claim.claim_id"]),
        ForeignKeyConstraint(["inspector_id"], ["subject.subject_id"]),
        ForeignKeyConstraint(["operation_id"], ["business_operation.operation_id"]),
        UniqueConstraint("claim_id"),
        UniqueConstraint("operation_id"),
        CheckConstraint("matched IN (0,1)"),
    ),
)
refunds = business_table(
    "refund_execution",
    col("refund_id", primary_key=True),
    col("claim_id"),
    col("order_id"),
    col("owner_id"),
    col("entry_id"),
    col("operation_id"),
    col("goods_minor", BigInteger),
    col("freight_minor", BigInteger),
    col("refunded_at", DateTime(timezone=True)),
    constraints=(
        ForeignKeyConstraint(["claim_id"], ["refund_claim.claim_id"]),
        ForeignKeyConstraint(["order_id"], ["customer_order.order_id"]),
        ForeignKeyConstraint(["owner_id"], ["subject.subject_id"]),
        ForeignKeyConstraint(["entry_id"], ["wallet_entry.entry_id"]),
        ForeignKeyConstraint(["operation_id"], ["business_operation.operation_id"]),
        UniqueConstraint("claim_id"),
        UniqueConstraint("operation_id"),
        UniqueConstraint("entry_id"),
        CheckConstraint("goods_minor>=0 AND freight_minor>=0 AND goods_minor+freight_minor>0"),
    ),
)
incidents = business_table(
    "transport_incident",
    col("incident_id", primary_key=True),
    col("order_id"),
    col("owner_id"),
    col("shipment_id"),
    col("ticket_id"),
    col("kind", String(24)),
    col("description", String(1000)),
    col("status", String(32)),
    col("confirmation_id"),
    col("operation_id"),
    col("claim_id", nullable=True),
    col("facts", JSONB, default=list),
    constraints=(
        ForeignKeyConstraint(["order_id"], ["customer_order.order_id"]),
        ForeignKeyConstraint(["owner_id"], ["subject.subject_id"]),
        ForeignKeyConstraint(["shipment_id"], ["order_shipment.shipment_id"]),
        ForeignKeyConstraint(["ticket_id"], ["support_ticket.ticket_id"]),
        ForeignKeyConstraint(["confirmation_id"], ["action_confirmation.confirmation_id"]),
        ForeignKeyConstraint(["operation_id"], ["business_operation.operation_id"]),
        ForeignKeyConstraint(["claim_id"], ["refund_claim.claim_id"]),
        UniqueConstraint("confirmation_id"),
        UniqueConstraint("operation_id"),
        UniqueConstraint("claim_id"),
        CheckConstraint("kind IN ('inquiry','expedite','intercept','lost')"),
        CheckConstraint(
            "status IN ('open','verifying','awaiting_intercept','intercept_failed','awaiting_liability','verified_return','verified_loss','needs_dispute','refunded','closed')"
        ),
    ),
)
Index("ix_refund_claim_scope", claims.c.store_id, claims.c.owner_id, claims.c.created_at)
Index("ix_refund_claim_order", claims.c.order_id, claims.c.status)
Index("ix_refund_item_order", claim_items.c.order_item_id)
Index("ix_incident_scope", incidents.c.store_id, incidents.c.owner_id, incidents.c.created_at)
