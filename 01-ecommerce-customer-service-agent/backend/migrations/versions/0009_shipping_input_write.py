"""个人表单允许新增，不允许运行角色改写已有资料。"""

from alembic import op

revision = "0009_shipping_input_write"
down_revision = "0008_payment_allocation"


def upgrade():
    op.execute("GRANT INSERT ON shipping_input TO cs_agent_app_b0")


def downgrade():
    raise RuntimeError("业务历史禁止自动破坏性回退")
