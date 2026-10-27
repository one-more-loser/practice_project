from sqlalchemy import select, update

from cs_agent.actions.repository import execute_once, new_id, provenance
from cs_agent.audit.service import record
from cs_agent.commerce.checkout import check_version, owned_order, shipping_input_row, snapshot
from cs_agent.core.context import Role
from cs_agent.core.errors import AppError, not_found
from cs_agent.db.business_tables import (
    address_changes,
    order_items,
    orders,
    receipts,
    shipment_items,
    shipments,
)
from cs_agent.identity.policies import require_role
from cs_agent.wallet.service import event


class FulfillmentService:
    def update_tracking(
        self, session, ctx, order_id, shipment_id, status, expected_version, key, now
    ):
        require_role(ctx, Role.SHOP_ADMIN)

        def execute(op_id):
            owned_order(session, ctx, order_id, lock=True, shop=True)
            row = (
                session.execute(
                    select(shipments)
                    .where(shipments.c.order_id == order_id, shipments.c.shipment_id == shipment_id)
                    .with_for_update()
                )
                .mappings()
                .first()
            )
            if row is None:
                raise not_found()
            check_version(row, expected_version)
            if status != "carrier_signed" or row["status"] != "in_transit":
                raise AppError("state_conflict", 409, "当前模拟轨迹只允许运输中转为承运签收")
            events = [
                *row["tracking_events"],
                {
                    "status": "carrier_signed",
                    "description": "承运方记录签收，待顾客本人确认收货",
                    "occurred_at": now.isoformat(),
                },
            ]
            session.execute(
                update(shipments)
                .where(shipments.c.shipment_id == shipment_id)
                .values(
                    status=status,
                    carrier_signed_at=now,
                    tracking_updated_at=now,
                    tracking_events=events,
                    version=row["version"] + 1,
                    updated_at=now,
                )
            )
            event(
                session,
                ctx,
                "shipment.updated",
                shipment_id,
                row["version"] + 1,
                op_id,
                {"order_ref": order_id, "shipment_ref": shipment_id, "status": status},
                now,
            )
            record(
                session,
                request_id=ctx.request_id,
                action="shipment.updated",
                outcome="succeeded",
                ctx=ctx,
                resource_ref=shipment_id,
                summary={"version": row["version"] + 1},
            )
            return {
                "business_ref": shipment_id,
                "version": row["version"] + 1,
                "status_name": status,
            }

        return execute_once(
            session,
            ctx,
            "shipment.tracking.update",
            key,
            {
                "order_id": order_id,
                "shipment_id": shipment_id,
                "status": status,
                "expected_version": expected_version,
            },
            now,
            execute,
            resource_ref=shipment_id,
        )

    def ship(self, session, ctx, order_id, quantities, expected_version, key, now):
        require_role(ctx, Role.SHOP_ADMIN)

        def execute(op_id):
            row = owned_order(session, ctx, order_id, lock=True, shop=True)
            check_version(row, expected_version)
            if row["trade_status"] != "paid" or row["fulfillment_status"] != "unshipped":
                raise AppError("state_conflict", 409, "仅已付款未发货订单可发货")
            lines = (
                session.execute(select(order_items).where(order_items.c.order_id == order_id))
                .mappings()
                .all()
            )
            if any(type(value) is not int for value in quantities.values()) or quantities != {
                line["order_item_id"]: line["ordered_qty"] for line in lines
            }:
                raise AppError("invalid_input", 422, "当前版本须整单发货，数量应与全部订单明细一致")
            shipment_id = new_id("shipment")
            session.execute(
                shipments.insert().values(
                    shipment_id=shipment_id,
                    order_id=order_id,
                    operation_id=op_id,
                    shipping_snapshot_id=row["current_shipping_id"],
                    carrier="模拟物流",
                    tracking_no="SIM" + shipment_id[-16:].upper(),
                    status="in_transit",
                    shipped_at=now,
                    tracking_updated_at=now,
                    tracking_events=[
                        {
                            "status": "in_transit",
                            "description": "店铺已发货，包裹运输中",
                            "occurred_at": now.isoformat(),
                        }
                    ],
                    **provenance(ctx, now),
                )
            )
            for line in lines:
                session.execute(
                    shipment_items.insert().values(
                        shipment_item_id=new_id("shipitem"),
                        shipment_id=shipment_id,
                        order_item_id=line["order_item_id"],
                        qty=line["ordered_qty"],
                        **provenance(ctx, now),
                    )
                )
            session.execute(
                update(orders)
                .where(orders.c.order_id == order_id)
                .values(
                    fulfillment_status="in_transit",
                    shipped_at=now,
                    version=row["version"] + 1,
                    updated_at=now,
                )
            )
            event(
                session,
                ctx,
                "order.shipped",
                order_id,
                row["version"] + 1,
                order_id,
                {"order_ref": order_id, "shipment_ref": shipment_id},
                now,
            )
            record(
                session,
                request_id=ctx.request_id,
                action="order.shipped",
                outcome="succeeded",
                ctx=ctx,
                resource_ref=order_id,
                summary={"version": row["version"] + 1},
            )
            return {
                "business_ref": order_id,
                "shipment_ref": shipment_id,
                "version": row["version"] + 1,
            }

        return execute_once(
            session,
            ctx,
            "order.ship",
            key,
            {"order_id": order_id, "quantities": quantities, "expected_version": expected_version},
            now,
            execute,
            resource_ref=order_id,
        )

    def change_address(
        self, session, ctx, parameters, expected_version, op_id, confirmation_id, now
    ):
        row = owned_order(session, ctx, parameters["order_ref"], lock=True)
        check_version(row, expected_version)
        self.check_change(row, now)
        input_row = shipping_input_row(
            session, ctx, parameters["shipping_input_ref"], "address_change"
        )
        new_snapshot = new_id("snapshot")
        snapshot(session, ctx, row["order_id"], input_row, new_snapshot, 2, now)
        session.execute(
            address_changes.insert().values(
                change_id=new_id("addresschange"),
                order_id=row["order_id"],
                operation_id=op_id,
                confirmation_id=confirmation_id,
                old_snapshot_id=row["current_shipping_id"],
                new_snapshot_id=new_snapshot,
                changed_at=now,
                **provenance(ctx, now),
            )
        )
        session.execute(
            update(orders)
            .where(orders.c.order_id == row["order_id"])
            .values(
                current_shipping_id=new_snapshot,
                change_count=1,
                version=row["version"] + 1,
                updated_at=now,
            )
        )
        event(
            session,
            ctx,
            "order.address_changed",
            row["order_id"],
            row["version"] + 1,
            row["order_id"],
            {"order_ref": row["order_id"], "change_count": 1},
            now,
        )
        record(
            session,
            request_id=ctx.request_id,
            action="order.address_changed",
            outcome="succeeded",
            ctx=ctx,
            resource_ref=row["order_id"],
            summary={"version": row["version"] + 1},
        )
        return {"business_ref": row["order_id"], "version": row["version"] + 1, "change_count": 1}

    @staticmethod
    def check_change(row, now):
        if (
            row["trade_status"] not in {"awaiting_payment", "paid"}
            or row["fulfillment_status"] != "unshipped"
            or row["change_count"] != 0
            or (row["trade_status"] == "awaiting_payment" and row["payment_expires_at"] <= now)
        ):
            raise AppError("state_conflict", 409, "订单已发货、关闭或已使用一次改址机会")

    def confirm_receipt(self, session, ctx, order_id, expected_version, op_id, now):
        row = owned_order(session, ctx, order_id, lock=True)
        check_version(row, expected_version)
        from cs_agent.db.aftersales_tables import claims, incidents

        active_refund = session.scalar(
            select(claims.c.claim_id)
            .where(
                claims.c.order_id == order_id,
                claims.c.case_kind == "transport_refund",
                claims.c.status.not_in(["rejected", "withdrawn"]),
            )
            .limit(1)
        )
        dispute = session.scalar(
            select(incidents.c.incident_id)
            .where(incidents.c.order_id == order_id, incidents.c.status == "needs_dispute")
            .limit(1)
        )
        if active_refund or dispute:
            raise AppError("state_conflict", 409, "运输退款或争议尚未结清，请先核对处理结果")
        if row["trade_status"] != "paid" or row["fulfillment_status"] != "in_transit":
            raise AppError("state_conflict", 409, "仅运输中订单可由本人确认收货")
        session.execute(
            receipts.insert().values(
                receipt_id=new_id("receipt"),
                order_id=order_id,
                owner_id=ctx.subject_id,
                operation_id=op_id,
                confirmed_at=now,
                **provenance(ctx, now),
            )
        )
        session.execute(
            update(orders)
            .where(orders.c.order_id == order_id)
            .values(
                trade_status="completed",
                fulfillment_status="received_by_customer",
                received_at=now,
                version=row["version"] + 1,
                updated_at=now,
            )
        )
        event(
            session,
            ctx,
            "order.received",
            order_id,
            row["version"] + 1,
            order_id,
            {"order_ref": order_id},
            now,
        )
        record(
            session,
            request_id=ctx.request_id,
            action="order.received",
            outcome="succeeded",
            ctx=ctx,
            resource_ref=order_id,
            summary={"version": row["version"] + 1},
        )
        return {"business_ref": order_id, "version": row["version"] + 1}
