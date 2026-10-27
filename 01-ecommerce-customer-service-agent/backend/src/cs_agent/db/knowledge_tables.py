from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

from cs_agent.db.business_tables import business_table, col

units = business_table(
    "knowledge_unit",
    col("unit_id", primary_key=True),
    col("title", String(160)),
    col("topic", String(80)),
    col("product_id", nullable=True),
    col("visibility", String(24)),
    col("status", String(16), default="draft"),
    col("active_revision", Integer, nullable=True),
    constraints=(
        ForeignKeyConstraint(["product_id"], ["product.product_id"]),
        CheckConstraint("visibility IN ('public','customer')"),
        CheckConstraint("status IN ('draft','published','retired')"),
    ),
)

revisions = business_table(
    "knowledge_revision",
    col("revision_id", primary_key=True),
    col("unit_id"),
    col("revision", Integer),
    col("body", Text),
    col("source_ref", String(160)),
    col("source_revision", String(80)),
    col("valid_from", DateTime(timezone=True)),
    col("valid_until", DateTime(timezone=True), nullable=True),
    col("status", String(16), default="draft"),
    col("synthetic", Boolean, nullable=False),
    col("created_by", String(80)),
    col("reviewed_by", String(80), nullable=True),
    col("reviewed_at", DateTime(timezone=True), nullable=True),
    col("published_at", DateTime(timezone=True), nullable=True),
    constraints=(
        ForeignKeyConstraint(["unit_id"], ["knowledge_unit.unit_id"]),
        UniqueConstraint("unit_id", "revision"),
        CheckConstraint("revision > 0"),
        CheckConstraint("status IN ('draft','approved','published','retired')"),
        CheckConstraint("valid_until IS NULL OR valid_until > valid_from"),
    ),
)

chunks = business_table(
    "knowledge_chunk",
    col("chunk_id", primary_key=True),
    col("revision_id"),
    col("unit_id"),
    col("ordinal", Integer),
    col("text", Text),
    col("keyword_text", Text),
    col("embedding", Vector(1024), nullable=True),
    col("embedding_model", String(100), nullable=True),
    col("embedding_service_revision", String(160), nullable=True),
    col("tokenizer_revision", String(80), default="jieba-search-cs-v1"),
    col("index_revision", String(100), nullable=True),
    col("test_only", Boolean, nullable=False, default=False),
    constraints=(
        ForeignKeyConstraint(["revision_id"], ["knowledge_revision.revision_id"]),
        ForeignKeyConstraint(["unit_id"], ["knowledge_unit.unit_id"]),
        UniqueConstraint("revision_id", "ordinal"),
        CheckConstraint("ordinal >= 0"),
    ),
)

faqs = business_table(
    "faq_revision",
    col("faq_revision_id", primary_key=True),
    col("faq_id", String(80)),
    col("revision", Integer),
    col("question", String(600)),
    col("variants", JSONB),
    col("answer", Text),
    col("source_ref", String(160)),
    col("source_revision", String(80)),
    col("topic", String(80)),
    col("product_id", nullable=True),
    col("valid_from", DateTime(timezone=True)),
    col("valid_until", DateTime(timezone=True), nullable=True),
    col("knowledge_revision_id", nullable=True),
    col("visibility", String(24)),
    col("status", String(16), default="draft"),
    col("synthetic", Boolean, nullable=False),
    col("created_by", String(80)),
    col("reviewed_by", String(80), nullable=True),
    col("reviewed_at", DateTime(timezone=True), nullable=True),
    col("published_at", DateTime(timezone=True), nullable=True),
    col("retired_at", DateTime(timezone=True), nullable=True),
    constraints=(
        ForeignKeyConstraint(["product_id"], ["product.product_id"]),
        ForeignKeyConstraint(["knowledge_revision_id"], ["knowledge_revision.revision_id"]),
        UniqueConstraint("faq_id", "revision"),
        CheckConstraint("valid_until IS NULL OR valid_until > valid_from"),
        CheckConstraint("revision > 0"),
        CheckConstraint("visibility IN ('public','customer')"),
        CheckConstraint("status IN ('draft','approved','published','retired')"),
    ),
)

Index("ix_knowledge_unit_scope", units.c.store_id, units.c.status, units.c.visibility)
Index(
    "ix_knowledge_revision_valid",
    revisions.c.unit_id,
    revisions.c.status,
    revisions.c.valid_from,
    revisions.c.valid_until,
)
Index("ix_knowledge_chunk_revision", chunks.c.revision_id, chunks.c.ordinal)
Index(
    "ix_knowledge_chunk_keyword",
    func.to_tsvector(text("'simple'::regconfig"), chunks.c.keyword_text),
    postgresql_using="gin",
)
Index("ix_faq_status", faqs.c.faq_id, faqs.c.status, faqs.c.visibility)
