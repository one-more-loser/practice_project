"""补齐当前地址的同订单约束、确认关联及业务查询索引。"""

from alembic import op

revision = "0010_business_integrity"
down_revision = "0009_shipping_input_write"


def upgrade():
    op.execute(
        "ALTER TABLE order_shipping_snapshot ADD CONSTRAINT uq_shipping_snapshot_order UNIQUE(snapshot_id,order_id)"
    )
    op.execute(
        "ALTER TABLE customer_order ADD CONSTRAINT fk_order_current_shipping FOREIGN KEY(current_shipping_id,order_id) REFERENCES order_shipping_snapshot(snapshot_id,order_id) DEFERRABLE INITIALLY DEFERRED"
    )
    op.execute(
        "ALTER TABLE business_operation ADD CONSTRAINT fk_operation_confirmation FOREIGN KEY(confirmation_id) REFERENCES action_confirmation(confirmation_id)"
    )
    op.execute(
        "CREATE INDEX ix_order_owner_created ON customer_order(owner_id,store_id,created_at)"
    )
    op.execute(
        "CREATE INDEX ix_order_unpaid_deadline ON customer_order(payment_expires_at) WHERE trade_status='awaiting_payment'"
    )
    op.execute("CREATE INDEX ix_order_item_order ON order_item(order_id)")
    op.execute("CREATE INDEX ix_sku_product ON sku(product_id)")


def downgrade():
    raise RuntimeError("业务历史禁止自动破坏性回退")
