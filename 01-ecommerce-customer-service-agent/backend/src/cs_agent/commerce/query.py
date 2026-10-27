from sqlalchemy import select

from cs_agent.commerce.checkout import owned_order
from cs_agent.core.context import Role
from cs_agent.db.business_tables import order_items, orders, shipments, shipping_snapshots
from cs_agent.identity.policies import require_role
from cs_agent.privacy.projections import PrivateContact, personal_contact, support_contact


class OrderQuery:
    def list(self, session, ctx, now, shop=False, limit=20, offset=0):
        require_role(ctx, Role.SHOP_ADMIN if shop else Role.CUSTOMER)
        stmt = select(orders.c.order_id).where(
            orders.c.store_id == ctx.store_id,
            orders.c.data_batch_id == ctx.data_batch_id,
            orders.c.batch_generation == ctx.batch_generation,
        )
        if not shop:
            stmt = stmt.where(orders.c.owner_id == ctx.subject_id)
        refs = session.scalars(
            stmt.order_by(orders.c.created_at.desc(), orders.c.order_id).offset(offset).limit(limit)
        ).all()
        return [self.get(session, ctx, ref, now, shop, include_private=False) for ref in refs]

    def get(self, session, ctx, order_id, now, shop=False, include_private=True):
        row = owned_order(session, ctx, order_id, shop=shop)
        result = {
            key: row[key]
            for key in (
                "order_id",
                "display_no",
                "trade_status",
                "fulfillment_status",
                "currency",
                "change_count",
                "version",
            )
        }
        for key in (
            "goods_minor",
            "discount_minor",
            "freight_minor",
            "payable_minor",
            "paid_minor",
            "refunded_minor",
            "refunded_goods_minor",
            "refunded_freight_minor",
        ):
            result[key] = str(row[key])
        for key in (
            "created_at",
            "payment_expires_at",
            "paid_at",
            "shipped_at",
            "received_at",
            "cancelled_at",
        ):
            result[key] = row[key].isoformat() if row[key] else None
        result["policy_snapshot"] = row["policy_snapshot"]
        result["items"] = []
        for item in session.execute(
            select(order_items)
            .where(order_items.c.order_id == order_id)
            .order_by(order_items.c.sku_id)
        ).mappings():
            projected = {
                key: item[key]
                for key in (
                    "order_item_id",
                    "sku_id",
                    "product_name",
                    "specifications",
                    "image_slug",
                    "ordered_qty",
                    "refunded_qty",
                    "reserved_refund_qty",
                )
            }
            projected.update(
                {
                    key: str(item[key])
                    for key in (
                        "unit_price_minor",
                        "goods_minor",
                        "discount_minor",
                        "allocated_minor",
                        "paid_goods_minor",
                        "refunded_goods_minor",
                        "reserved_refund_minor",
                    )
                }
            )
            result["items"].append(projected)
        contact = (
            session.execute(
                select(shipping_snapshots).where(
                    shipping_snapshots.c.snapshot_id == row["current_shipping_id"],
                    shipping_snapshots.c.order_id == order_id,
                )
            )
            .mappings()
            .one()
        )
        private = PrivateContact(
            contact["snapshot_id"],
            **{
                key: contact[key]
                for key in (
                    "recipient",
                    "phone",
                    "province",
                    "city",
                    "district",
                    "street",
                    "postal_code",
                )
            },
        )
        result["shipping"] = (
            support_contact(private) if shop or not include_private else personal_contact(private)
        )
        result["shipments"] = [
            {
                "shipment_id": shipment["shipment_id"],
                "carrier": shipment["carrier"],
                "tracking_no": shipment["tracking_no"],
                "status": shipment["status"],
                "events": shipment["tracking_events"],
                "shipped_at": shipment["shipped_at"].isoformat(),
                "carrier_signed_at": shipment["carrier_signed_at"].isoformat()
                if shipment["carrier_signed_at"]
                else None,
                "version": shipment["version"],
            }
            for shipment in session.execute(
                select(shipments).where(shipments.c.order_id == order_id)
            ).mappings()
        ]
        unpaid = row["trade_status"] == "awaiting_payment" and row["payment_expires_at"] > now
        paid_unshipped = row["trade_status"] == "paid" and row["fulfillment_status"] == "unshipped"
        from cs_agent.db.aftersales_tables import claims, incidents

        blocked_receipt = session.scalar(
            select(claims.c.claim_id)
            .where(
                claims.c.order_id == order_id,
                claims.c.case_kind == "transport_refund",
                claims.c.status.not_in(["rejected", "withdrawn"]),
            )
            .limit(1)
        ) or session.scalar(
            select(incidents.c.incident_id)
            .where(incidents.c.order_id == order_id, incidents.c.status == "needs_dispute")
            .limit(1)
        )
        result["allowed_actions"] = {
            "pay": unpaid,
            "change_address": (unpaid or paid_unshipped) and row["change_count"] == 0,
            "cancel": unpaid or paid_unshipped,
            "confirm_receipt": row["trade_status"] == "paid"
            and row["fulfillment_status"] == "in_transit"
            and not blocked_receipt,
            "ship": paid_unshipped,
            "aftersale": row["fulfillment_status"] == "received_by_customer"
            and any(
                item["ordered_qty"] > item["refunded_qty"] + item["reserved_refund_qty"]
                for item in result["items"]
            ),
        }
        result["cancellation_requires_eligibility_review"] = paid_unshipped
        return result
