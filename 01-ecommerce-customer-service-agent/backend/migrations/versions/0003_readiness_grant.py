"""应用仅需读取迁移版本；补齐最小就绪探测权限。"""

from alembic import op

revision = "0003_readiness_grant"
down_revision = "0002_identity"


def upgrade():
    op.execute("GRANT SELECT ON alembic_version TO cs_agent_app_b0")


def downgrade():
    op.execute("REVOKE SELECT ON alembic_version FROM cs_agent_app_b0")
