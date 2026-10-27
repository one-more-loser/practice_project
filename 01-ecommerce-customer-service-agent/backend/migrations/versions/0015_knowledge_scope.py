"""知识适用范围、来源绑定、有效期和索引生成版本。"""

import sqlalchemy as sa
from alembic import op

revision = "0015_knowledge_scope"
down_revision = "0014_runtime_knowledge"


def upgrade():
    for table in ("knowledge_unit", "faq_revision"):
        op.add_column(table, sa.Column("product_id", sa.String(80), nullable=True))
        op.create_foreign_key(
            f"fk_{table}_product", table, "product", ["product_id"], ["product_id"]
        )
    op.add_column(
        "knowledge_chunk", sa.Column("embedding_service_revision", sa.String(160), nullable=True)
    )
    op.add_column(
        "knowledge_chunk",
        sa.Column(
            "tokenizer_revision", sa.String(80), nullable=False, server_default="jieba-search-cs-v1"
        ),
    )
    op.add_column(
        "faq_revision",
        sa.Column(
            "valid_from", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.add_column(
        "faq_revision", sa.Column("valid_until", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("faq_revision", sa.Column("knowledge_revision_id", sa.String(80), nullable=True))
    op.create_foreign_key(
        "fk_faq_source_revision",
        "faq_revision",
        "knowledge_revision",
        ["knowledge_revision_id"],
        ["revision_id"],
    )
    op.create_check_constraint(
        "ck_faq_validity", "faq_revision", "valid_until IS NULL OR valid_until > valid_from"
    )


def downgrade():
    raise RuntimeError("知识治理历史不自动破坏性回退")
