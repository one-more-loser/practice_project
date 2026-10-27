"""稳定消息次序与受控知识/FAQ版本表。"""

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision = "0014_runtime_knowledge"
down_revision = "0013_conversation_runtime"


def upgrade():
    op.add_column(
        "conversation",
        sa.Column("next_message_seq", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column("conversation_message", sa.Column("message_seq", sa.Integer(), nullable=True))
    op.add_column("conversation_message", sa.Column("citations", postgresql.JSONB(), nullable=True))
    op.execute("UPDATE conversation_message SET citations='[]'::jsonb WHERE citations IS NULL")
    op.alter_column("conversation_message", "citations", nullable=False)
    op.execute("""
        WITH ordered AS (
            SELECT message_id, row_number() OVER (
                PARTITION BY conversation_id ORDER BY created_at, message_id
            ) AS seq
            FROM conversation_message
        )
        UPDATE conversation_message m SET message_seq = ordered.seq
        FROM ordered WHERE ordered.message_id = m.message_id
    """)
    op.execute("""
        UPDATE conversation c SET next_message_seq = COALESCE((
            SELECT max(m.message_seq) + 1 FROM conversation_message m
            WHERE m.conversation_id = c.conversation_id
        ), 1)
    """)
    op.alter_column("conversation_message", "message_seq", nullable=False)
    op.create_unique_constraint(
        "uq_message_conversation_seq", "conversation_message", ["conversation_id", "message_seq"]
    )
    op.create_index(
        "ix_message_history_seq", "conversation_message", ["conversation_id", "message_seq"]
    )

    op.create_table(
        "knowledge_unit",
        sa.Column("unit_id", sa.String(80), primary_key=True),
        sa.Column("title", sa.String(160), nullable=False),
        sa.Column("topic", sa.String(80), nullable=False),
        sa.Column("visibility", sa.String(24), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("active_revision", sa.Integer(), nullable=True),
        sa.Column("store_id", sa.String(80), sa.ForeignKey("store.store_id"), nullable=False),
        sa.Column("source_kind", sa.String(32), nullable=False),
        sa.Column("data_batch_id", sa.String(80), nullable=False),
        sa.Column("batch_generation", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["data_batch_id", "batch_generation"],
            ["source_batch.batch_id", "source_batch.generation"],
        ),
        sa.CheckConstraint("version > 0"),
        sa.CheckConstraint("source_kind IN ('synthetic','derived','business_import')"),
        sa.CheckConstraint("visibility IN ('public','customer')"),
        sa.CheckConstraint("status IN ('draft','published','retired')"),
    )
    op.create_index(
        "ix_knowledge_unit_scope", "knowledge_unit", ["store_id", "status", "visibility"]
    )

    op.create_table(
        "knowledge_revision",
        sa.Column("revision_id", sa.String(80), primary_key=True),
        sa.Column("unit_id", sa.String(80), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("source_ref", sa.String(160), nullable=False),
        sa.Column("source_revision", sa.String(80), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("synthetic", sa.Boolean(), nullable=False),
        sa.Column("created_by", sa.String(80), nullable=False),
        sa.Column("reviewed_by", sa.String(80), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("store_id", sa.String(80), sa.ForeignKey("store.store_id"), nullable=False),
        sa.Column("source_kind", sa.String(32), nullable=False),
        sa.Column("data_batch_id", sa.String(80), nullable=False),
        sa.Column("batch_generation", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["unit_id"], ["knowledge_unit.unit_id"]),
        sa.ForeignKeyConstraint(
            ["data_batch_id", "batch_generation"],
            ["source_batch.batch_id", "source_batch.generation"],
        ),
        sa.UniqueConstraint("unit_id", "revision"),
        sa.CheckConstraint("version > 0"),
        sa.CheckConstraint("source_kind IN ('synthetic','derived','business_import')"),
        sa.CheckConstraint("revision > 0"),
        sa.CheckConstraint("status IN ('draft','approved','published','retired')"),
        sa.CheckConstraint("valid_until IS NULL OR valid_until > valid_from"),
    )
    op.create_index(
        "ix_knowledge_revision_valid",
        "knowledge_revision",
        ["unit_id", "status", "valid_from", "valid_until"],
    )

    op.create_table(
        "knowledge_chunk",
        sa.Column("chunk_id", sa.String(80), primary_key=True),
        sa.Column("revision_id", sa.String(80), nullable=False),
        sa.Column("unit_id", sa.String(80), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("keyword_text", sa.Text(), nullable=False),
        sa.Column("embedding", Vector(1024), nullable=True),
        sa.Column("embedding_model", sa.String(100), nullable=True),
        sa.Column("index_revision", sa.String(100), nullable=True),
        sa.Column("test_only", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("store_id", sa.String(80), sa.ForeignKey("store.store_id"), nullable=False),
        sa.Column("source_kind", sa.String(32), nullable=False),
        sa.Column("data_batch_id", sa.String(80), nullable=False),
        sa.Column("batch_generation", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["revision_id"], ["knowledge_revision.revision_id"]),
        sa.ForeignKeyConstraint(["unit_id"], ["knowledge_unit.unit_id"]),
        sa.ForeignKeyConstraint(
            ["data_batch_id", "batch_generation"],
            ["source_batch.batch_id", "source_batch.generation"],
        ),
        sa.UniqueConstraint("revision_id", "ordinal"),
        sa.CheckConstraint("version > 0"),
        sa.CheckConstraint("source_kind IN ('synthetic','derived','business_import')"),
        sa.CheckConstraint("ordinal >= 0"),
    )
    op.create_index("ix_knowledge_chunk_revision", "knowledge_chunk", ["revision_id", "ordinal"])
    op.execute(
        "CREATE INDEX ix_knowledge_chunk_keyword ON knowledge_chunk USING gin (to_tsvector('simple', keyword_text))"
    )

    op.create_table(
        "faq_revision",
        sa.Column("faq_revision_id", sa.String(80), primary_key=True),
        sa.Column("faq_id", sa.String(80), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("question", sa.String(600), nullable=False),
        sa.Column("variants", postgresql.JSONB(), nullable=False),
        sa.Column("answer", sa.Text(), nullable=False),
        sa.Column("source_ref", sa.String(160), nullable=False),
        sa.Column("source_revision", sa.String(80), nullable=False),
        sa.Column("topic", sa.String(80), nullable=False),
        sa.Column("visibility", sa.String(24), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("synthetic", sa.Boolean(), nullable=False),
        sa.Column("created_by", sa.String(80), nullable=False),
        sa.Column("reviewed_by", sa.String(80), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("store_id", sa.String(80), sa.ForeignKey("store.store_id"), nullable=False),
        sa.Column("source_kind", sa.String(32), nullable=False),
        sa.Column("data_batch_id", sa.String(80), nullable=False),
        sa.Column("batch_generation", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["data_batch_id", "batch_generation"],
            ["source_batch.batch_id", "source_batch.generation"],
        ),
        sa.UniqueConstraint("faq_id", "revision"),
        sa.CheckConstraint("version > 0"),
        sa.CheckConstraint("source_kind IN ('synthetic','derived','business_import')"),
        sa.CheckConstraint("revision > 0"),
        sa.CheckConstraint("visibility IN ('public','customer')"),
        sa.CheckConstraint("status IN ('draft','approved','published','retired')"),
    )
    op.create_index("ix_faq_status", "faq_revision", ["faq_id", "status", "visibility"])
    for table in ("knowledge_unit", "knowledge_revision", "knowledge_chunk", "faq_revision"):
        op.execute(f'GRANT SELECT,INSERT,UPDATE,DELETE ON "{table}" TO cs_agent_app_b0')


def downgrade():
    raise RuntimeError("会话/知识历史不自动破坏性回退")
