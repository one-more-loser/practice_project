"""区分应付分摊与实际支付；不重写已执行历史迁移。"""

from alembic import op
from sqlalchemy import text

revision = "0008_payment_allocation"
down_revision = "0007_fulfillment"


def upgrade():
    connection = op.get_bind()
    constraints = (
        connection.execute(
            text(
                "SELECT conname FROM pg_constraint WHERE conrelid='order_item'::regclass AND contype='c' AND pg_get_constraintdef(oid) LIKE '%paid_goods_minor = (goods_minor - discount_minor)%'"
            )
        )
        .scalars()
        .all()
    )
    for name in constraints:
        op.drop_constraint(name, "order_item", type_="check")
    op.execute("ALTER TABLE order_item ADD COLUMN allocated_minor BIGINT")
    op.execute("UPDATE order_item SET allocated_minor=goods_minor-discount_minor")
    op.execute(
        "UPDATE order_item i SET paid_goods_minor=0 FROM customer_order o WHERE i.order_id=o.order_id AND o.paid_minor=0"
    )
    op.execute("ALTER TABLE order_item ALTER COLUMN allocated_minor SET NOT NULL")
    op.execute(
        "ALTER TABLE order_item ADD CHECK (ordered_qty>0 AND unit_price_minor>0 AND goods_minor=unit_price_minor*ordered_qty AND allocated_minor=goods_minor-discount_minor AND paid_goods_minor>=0 AND paid_goods_minor<=allocated_minor)"
    )
    op.execute("GRANT UPDATE ON order_item TO cs_agent_app_b0")


def downgrade():
    raise RuntimeError("业务历史禁止自动破坏性回退")
