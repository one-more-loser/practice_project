from datetime import datetime

from sqlalchemy import and_, func, select

from cs_agent.core.context import Role
from cs_agent.core.errors import AppError
from cs_agent.db.business_tables import (
    order_items,
    orders,
    shipments,
    wallet_entries,
    wallets,
)
from cs_agent.identity.policies import require_role
from cs_agent.query.metrics import METRICS, METRICS_REVISION
from cs_agent.query.validator import date_range, rejected


def _money(value):
    return str(value or 0)


class QueryService:
    """Fixed, identity-scoped reads for common intents; model SQL stays on SQLExecutor."""

    def __init__(self, sql_executor=None):
        self.sql_executor = sql_executor

    def read(self, session, ctx, operation, params, now):
        require_role(ctx, Role.CUSTOMER)
        if not isinstance(params, dict):
            raise rejected("invalid_parameters")
        if operation == "orders.list":
            return self._orders(session, ctx, params)
        if operation == "orders.detail":
            return self._order_detail(session, ctx, params)
        if operation == "orders.summary":
            return self._summary(session, ctx, params, now)
        if operation == "shipments.list":
            return self._shipments(session, ctx, params)
        if operation == "wallet.summary":
            return self._wallet(session, ctx, params)
        raise AppError("unsupported_query", 422, "暂不支持此类业务查询")

    def execute_candidate(self, ctx, sql, parameters, now, *, schema_revision, schema_digest):
        if self.sql_executor is None:
            raise AppError("query_unavailable", 503, "受控查询尚未配置")
        return self.sql_executor.query(
            ctx,
            sql,
            parameters,
            now,
            schema_revision=schema_revision,
            schema_digest=schema_digest,
        )

    @staticmethod
    def _scope(ctx):
        return (
            orders.c.owner_id == ctx.subject_id,
            orders.c.store_id == ctx.store_id,
            orders.c.data_batch_id == ctx.data_batch_id,
            orders.c.batch_generation == ctx.batch_generation,
        )

    def _orders(self, session, ctx, params):
        if set(params) - {"limit", "offset"}:
            raise rejected("invalid_parameters")
        limit, offset = params.get("limit", 20), params.get("offset", 0)
        if (
            type(limit) is not int
            or not 1 <= limit <= 50
            or type(offset) is not int
            or not 0 <= offset <= 10000
        ):
            raise rejected("invalid_parameters")
        rows = (
            session.execute(
                select(
                    orders.c.order_id,
                    orders.c.display_no,
                    orders.c.trade_status,
                    orders.c.fulfillment_status,
                    orders.c.currency,
                    orders.c.payable_minor,
                    orders.c.paid_minor,
                    orders.c.refunded_minor,
                    orders.c.created_at,
                    orders.c.paid_at,
                    orders.c.received_at,
                )
                .where(*self._scope(ctx))
                .order_by(orders.c.created_at.desc(), orders.c.order_id)
                .offset(offset)
                .limit(limit + 1)
            )
            .mappings()
            .all()
        )
        return {
            "items": [
                {
                    **{
                        key: row[key]
                        for key in (
                            "order_id",
                            "display_no",
                            "trade_status",
                            "fulfillment_status",
                            "currency",
                        )
                    },
                    "payable_minor": _money(row["payable_minor"]),
                    "paid_minor": _money(row["paid_minor"]),
                    "refunded_minor": _money(row["refunded_minor"]),
                    "created_at": row["created_at"].isoformat(),
                    "paid_at": row["paid_at"].isoformat() if row["paid_at"] else None,
                    "received_at": row["received_at"].isoformat() if row["received_at"] else None,
                }
                for row in rows[:limit]
            ],
            "has_more": len(rows) > limit,
            "limit": limit,
            "offset": offset,
        }

    def _order_detail(self, session, ctx, params):
        if (
            set(params) != {"order_id"}
            or not isinstance(params["order_id"], str)
            or len(params["order_id"]) > 80
        ):
            raise rejected("invalid_parameters")
        order = (
            session.execute(
                select(orders).where(*self._scope(ctx), orders.c.order_id == params["order_id"])
            )
            .mappings()
            .first()
        )
        if not order:
            raise AppError("not_found", 404, "资源不存在或不可访问")
        items = (
            session.execute(
                select(
                    order_items.c.order_item_id,
                    order_items.c.sku_id,
                    order_items.c.product_name,
                    order_items.c.ordered_qty,
                    order_items.c.paid_goods_minor,
                    order_items.c.refunded_qty,
                    order_items.c.refunded_goods_minor,
                    order_items.c.reserved_refund_qty,
                )
                .where(
                    order_items.c.order_id == order["order_id"],
                    order_items.c.store_id == ctx.store_id,
                    order_items.c.data_batch_id == ctx.data_batch_id,
                    order_items.c.batch_generation == ctx.batch_generation,
                )
                .order_by(order_items.c.order_item_id)
            )
            .mappings()
            .all()
        )
        return {
            "order_id": order["order_id"],
            "display_no": order["display_no"],
            "trade_status": order["trade_status"],
            "fulfillment_status": order["fulfillment_status"],
            "currency": order["currency"],
            "created_at": order["created_at"].isoformat(),
            "paid_minor": _money(order["paid_minor"]),
            "refunded_minor": _money(order["refunded_minor"]),
            "net_paid_minor": _money(order["paid_minor"] - order["refunded_minor"]),
            "items": [
                {
                    "order_item_id": row["order_item_id"],
                    "sku_id": row["sku_id"],
                    "product_name": row["product_name"],
                    "ordered_qty": row["ordered_qty"],
                    "paid_goods_minor": _money(row["paid_goods_minor"]),
                    "refunded_qty": row["refunded_qty"],
                    "remaining_qty": row["ordered_qty"]
                    - row["refunded_qty"]
                    - row["reserved_refund_qty"],
                    "refunded_goods_minor": _money(row["refunded_goods_minor"]),
                }
                for row in items
            ],
        }

    def _summary(self, session, ctx, params, now):
        if set(params) - {"start_at", "end_at"}:
            raise rejected("invalid_parameters")
        if any(
            key in params and (not isinstance(params[key], datetime) or params[key].tzinfo is None)
            for key in ("start_at", "end_at")
        ):
            raise rejected("invalid_range")
        window = date_range(now, params.get("start_at"), params.get("end_at"))
        start, end = window["start_at"], window["end_at"]
        if (end - start).total_seconds() > 366 * 86400:
            raise rejected("invalid_range")
        paid = and_(
            orders.c.trade_status.in_(
                ["paid", "completed", "cancelled_refunded", "closed_refunded"]
            ),
            orders.c.paid_minor > 0,
        )
        row = (
            session.execute(
                select(
                    func.count().label("order_count"),
                    func.coalesce(func.sum(orders.c.paid_minor), 0).label("paid_minor"),
                    func.coalesce(func.sum(orders.c.refunded_minor), 0).label("refunded_minor"),
                ).where(
                    *self._scope(ctx), orders.c.created_at >= start, orders.c.created_at < end, paid
                )
            )
            .mappings()
            .one()
        )
        paid_minor, refunded_minor = row["paid_minor"], row["refunded_minor"]
        return {
            "metrics_revision": METRICS_REVISION,
            "definitions": METRICS,
            "window": {
                "start_at": start.isoformat(),
                "end_at": end.isoformat(),
                "end_inclusive": False,
            },
            "order_count": row["order_count"],
            "paid_minor": _money(paid_minor),
            "refunded_minor": _money(refunded_minor),
            "net_paid_minor": _money(paid_minor - refunded_minor),
            "currency": "CNY",
        }

    def _shipments(self, session, ctx, params):
        if set(params) != {"order_id"} or not isinstance(params["order_id"], str):
            raise rejected("invalid_parameters")
        order_id = params["order_id"]
        owns = session.execute(
            select(orders.c.order_id).where(*self._scope(ctx), orders.c.order_id == order_id)
        ).first()
        if not owns:
            raise AppError("not_found", 404, "资源不存在或不可访问")
        rows = (
            session.execute(
                select(
                    shipments.c.shipment_id,
                    shipments.c.carrier,
                    shipments.c.status,
                    shipments.c.shipped_at,
                    shipments.c.carrier_signed_at,
                    shipments.c.tracking_updated_at,
                )
                .where(
                    shipments.c.order_id == order_id,
                    shipments.c.store_id == ctx.store_id,
                    shipments.c.data_batch_id == ctx.data_batch_id,
                    shipments.c.batch_generation == ctx.batch_generation,
                )
                .order_by(shipments.c.shipped_at)
            )
            .mappings()
            .all()
        )
        return {
            "items": [
                {
                    "shipment_id": row["shipment_id"],
                    "carrier": row["carrier"],
                    "status": row["status"],
                    "shipped_at": row["shipped_at"].isoformat(),
                    "carrier_signed_at": row["carrier_signed_at"].isoformat()
                    if row["carrier_signed_at"]
                    else None,
                    "tracking_updated_at": row["tracking_updated_at"].isoformat(),
                }
                for row in rows
            ],
            "customer_receipt_required": True,
        }

    def _wallet(self, session, ctx, params):
        if set(params) - {"limit"}:
            raise rejected("invalid_parameters")
        limit = params.get("limit", 20)
        if type(limit) is not int or not 1 <= limit <= 50:
            raise rejected("invalid_parameters")
        account = (
            session.execute(
                select(
                    wallets.c.account_id,
                    wallets.c.balance_minor,
                    wallets.c.currency,
                    wallets.c.status,
                ).where(
                    wallets.c.owner_id == ctx.subject_id,
                    wallets.c.store_id == ctx.store_id,
                    wallets.c.data_batch_id == ctx.data_batch_id,
                    wallets.c.batch_generation == ctx.batch_generation,
                )
            )
            .mappings()
            .first()
        )
        if not account:
            return {"account": None, "entries": []}
        rows = (
            session.execute(
                select(
                    wallet_entries.c.entry_id,
                    wallet_entries.c.kind,
                    wallet_entries.c.amount_minor,
                    wallet_entries.c.sequence,
                    wallet_entries.c.balance_before_minor,
                    wallet_entries.c.balance_after_minor,
                    wallet_entries.c.occurred_at,
                    wallet_entries.c.currency,
                )
                .where(
                    wallet_entries.c.account_id == account["account_id"],
                    wallet_entries.c.owner_id == ctx.subject_id,
                    wallet_entries.c.store_id == ctx.store_id,
                    wallet_entries.c.data_batch_id == ctx.data_batch_id,
                    wallet_entries.c.batch_generation == ctx.batch_generation,
                )
                .order_by(wallet_entries.c.sequence.desc())
                .limit(limit)
            )
            .mappings()
            .all()
        )
        return {
            "account": {
                "balance_minor": _money(account["balance_minor"]),
                "currency": account["currency"],
                "status": account["status"],
            },
            "entries": [
                {
                    "entry_id": row["entry_id"],
                    "kind": row["kind"],
                    "amount_minor": _money(row["amount_minor"]),
                    "sequence": row["sequence"],
                    "balance_before_minor": _money(row["balance_before_minor"]),
                    "balance_after_minor": _money(row["balance_after_minor"]),
                    "occurred_at": row["occurred_at"].isoformat(),
                    "currency": row["currency"],
                }
                for row in reversed(rows)
            ],
        }
