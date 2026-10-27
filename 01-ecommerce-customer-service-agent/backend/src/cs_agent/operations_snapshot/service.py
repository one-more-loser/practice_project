from sqlalchemy import and_, case, func, literal, select, text, union_all

from cs_agent.core.errors import AppError, not_found
from cs_agent.db.aftersales_tables import refunds as formal_refunds
from cs_agent.db.business_tables import inventory, order_items, orders, products, skus
from cs_agent.db.cancellation_tables import refunds as cancellation_refunds
from cs_agent.db.models import SourceBatch, Store
from cs_agent.operations_snapshot.projection import build_projection, digest, read_inputs
from cs_agent.operations_snapshot.tables import snapshot_states


def source_statement(store_id):
    """一条SQL读取全部事实，使用PG单语句MVCC视图，避免多次READ COMMITTED漂移。"""
    scope = select(Store.store_id, Store.data_batch_id, Store.batch_generation,
                   SourceBatch.source_kind).join(SourceBatch, and_(
        SourceBatch.batch_id == Store.data_batch_id,
        SourceBatch.generation == Store.batch_generation,
    )).where(Store.store_id == store_id, Store.status == "active",
             SourceBatch.status == "active").cte("snapshot_scope")

    def scoped(table):
        return and_(table.c.store_id == scope.c.store_id,
                    table.c.data_batch_id == scope.c.data_batch_id,
                    table.c.batch_generation == scope.c.batch_generation)

    def row(kind, values):
        pairs = []
        for key, value in values.items():
            pairs.extend((literal(key), value))
        return select(literal(kind).label("kind"), func.jsonb_build_object(*pairs).label("body"))

    scope_row = row("scope", {name: scope.c[name] for name in (
        "store_id", "data_batch_id", "batch_generation", "source_kind")}).select_from(scope)
    sku_row = row("skus", {
        "sku_id": skus.c.sku_id, "title": products.c.name, "category": products.c.category,
        "base_price_minor": skus.c.price_minor, "cost_minor": skus.c.cost_minor,
        "price_version": skus.c.price_version,
        "available_qty": case((and_(skus.c.status == "active", products.c.status == "active"),
                                inventory.c.stock_qty - inventory.c.reserved_qty), else_=0),
    }).select_from(skus.join(scope, scoped(skus)).join(products, and_(
        products.c.product_id == skus.c.product_id, scoped(products),
    )).outerjoin(inventory, and_(inventory.c.sku_id == skus.c.sku_id, scoped(inventory))))
    order_row = row("orders", {
        "order_id": orders.c.order_id, "paid_at": orders.c.paid_at,
        "trade_status": orders.c.trade_status, "paid_minor": orders.c.paid_minor,
    }).select_from(orders.join(scope, scoped(orders)))
    item_row = row("items", {
        "order_item_id": order_items.c.order_item_id, "order_id": order_items.c.order_id,
        "sku_id": order_items.c.sku_id, "qty": order_items.c.ordered_qty,
        "paid_minor": order_items.c.paid_goods_minor,
    }).select_from(order_items.join(scope, scoped(order_items)))
    formal_row = row("refunds", {
        "refund_id": literal("aftersale:") + formal_refunds.c.refund_id,
        "order_id": formal_refunds.c.order_id, "occurred_at": formal_refunds.c.refunded_at,
        "amount_minor": formal_refunds.c.goods_minor + formal_refunds.c.freight_minor,
    }).select_from(formal_refunds.join(scope, scoped(formal_refunds)))
    cancel_row = row("refunds", {
        "refund_id": literal("cancellation:") + cancellation_refunds.c.refund_id,
        "order_id": cancellation_refunds.c.order_id,
        "occurred_at": cancellation_refunds.c.refunded_at,
        "amount_minor": cancellation_refunds.c.amount_minor,
    }).select_from(cancellation_refunds.join(scope, scoped(cancellation_refunds)))
    return union_all(scope_row, sku_row, order_row, item_row, formal_row, cancel_row)


def produce_snapshot(session, store_id, now, *, inputs_file=None):
    # 同店串行快照修订；源读取在锁后单语句完成，HTTP/模型不进入事务。
    session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:scope,0))"),
                    {"scope": "cs01:operations-snapshot:" + store_id})
    rows = list(session.execute(source_statement(store_id)).mappings())
    scopes = [row["body"] for row in rows if row["kind"] == "scope"]
    if len(scopes) != 1:
        raise not_found()
    scope = scopes[0]
    inputs = read_inputs(inputs_file, store_id, scope["data_batch_id"], scope["batch_generation"])
    payload = build_projection(scope, [row for row in rows if row["kind"] != "scope"], now, inputs)
    identity = payload["snapshot_id"]
    previous = session.execute(select(snapshot_states).where(
        snapshot_states.c.snapshot_id == identity)).mappings().one_or_none()
    if previous and now < previous["observed_at"]:
        raise AppError("snapshot_clock_conflict", 409, "快照时钟不能倒退")
    content_digest = digest(payload)
    if previous and content_digest == previous["source_digest"]:
        return previous["payload"]
    if previous:
        payload["revision"] = previous["revision"] + 1
        session.execute(snapshot_states.update().where(
            snapshot_states.c.snapshot_id == identity).values(
                revision=payload["revision"], source_digest=content_digest, observed_at=now,
                payload=payload, version=previous["version"] + 1,
            ))
    else:
        session.execute(snapshot_states.insert().values(
            snapshot_id=identity, revision=1, source_digest=content_digest, observed_at=now,
            payload=payload, store_id=store_id, source_kind="derived",
            data_batch_id=scope["data_batch_id"], batch_generation=scope["batch_generation"],
        ))
    return payload
