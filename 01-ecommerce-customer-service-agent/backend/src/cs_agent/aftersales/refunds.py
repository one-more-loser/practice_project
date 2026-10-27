"""唯一内部退款入口，无顾客、员工或Agent的任意金额接口。"""

from dataclasses import replace

from sqlalchemy import select, update

from cs_agent.actions.repository import new_id, provenance
from cs_agent.audit.service import record
from cs_agent.core.context import Role
from cs_agent.db.aftersales_tables import incidents, inspections, refunds
from cs_agent.db.business_tables import order_items, orders, receipts
from cs_agent.wallet.service import post_entry

from .rules import conflict, original_freight
from .service import case_row, changed, items_for, order_row


class RefundService:
    def execute(self, session, ctx, claim, operation_id, now):
        """调用者已在同一execute_once事务完成核验；拒绝后整次核验回滚。"""
        if ctx.role not in {Role.SUPPORT, Role.SUPERVISOR, Role.SHOP_ADMIN}:
            conflict("退款只能由获准人工核验流程触发")
        order = order_row(session, ctx, claim["order_id"], lock=True)
        claim = case_row(session, ctx, claim["claim_id"])
        existing = (
            session.execute(select(refunds).where(refunds.c.claim_id == claim["claim_id"]))
            .mappings()
            .first()
        )
        if existing:
            return {
                "refund_id": existing["refund_id"],
                "entry_id": existing["entry_id"],
                "status_name": "refunded",
            }
        if (
            not claim["approved_at"]
            or not claim["reviewer_id"]
            or claim["status"] != "refund_reviewed"
            or claim["verified_kind"] not in {"return", "loss", "return_dispute"}
        ):
            conflict("尚缺顾客确认、人工审核、实收或丢件责任核验")
        if claim["case_kind"] == "transport_refund":
            if (
                session.execute(
                    select(receipts.c.receipt_id).where(receipts.c.order_id == order["order_id"])
                ).first()
                or order["fulfillment_status"] != "in_transit"
            ):
                conflict("运输事实已变化，请重新核对，不能伪造收货退款")
        if claim["verified_kind"] in {"return", "return_dispute"}:
            inspection = (
                session.execute(
                    select(inspections).where(inspections.c.claim_id == claim["claim_id"])
                )
                .mappings()
                .first()
            )
            if not inspection or inspection["inspector_id"] == claim["reviewer_id"]:
                conflict("审核客服与店铺收退核验人员必须分离")
            if not inspection["matched"] and claim["verified_kind"] != "return_dispute":
                conflict("实收数量不符，须主管有据处理争议")
        elif claim["case_kind"] != "transport_refund":
            # 正式售后的退回丢件也须保留店铺实收事实和主管争议依据。
            conflict("正式售后不能以运输丢件状态替代退货核验")
        goods, freight = claim["goods_minor"], claim["freight_minor"]
        # 本次运费不能在申请之后被人工扩充；再核对原历史政策。
        if freight != original_freight(order, claim["reason"], freight > 0):
            conflict("原运费依据已不一致，请人工核对")
        if (
            order["refunded_minor"] + goods + freight > order["paid_minor"]
            or order["refunded_freight_minor"] + freight > order["freight_minor"]
        ):
            conflict("退款将超过原实付或原运费")
        for line in items_for(session, claim["claim_id"]):
            original = (
                session.execute(
                    select(order_items).where(
                        order_items.c.order_item_id == line["order_item_id"],
                        order_items.c.order_id == order["order_id"],
                    )
                )
                .mappings()
                .one()
            )
            if (
                original["reserved_refund_qty"] < line["qty"]
                or original["reserved_refund_minor"] < line["goods_minor"]
                or original["refunded_qty"] + line["qty"] > original["ordered_qty"]
                or original["refunded_goods_minor"] + line["goods_minor"]
                > original["paid_goods_minor"]
            ):
                conflict("申请占用或剩余可退额不符，须核对，不能释放后重建")
            session.execute(
                update(order_items)
                .where(order_items.c.order_item_id == line["order_item_id"])
                .values(
                    reserved_refund_qty=original["reserved_refund_qty"] - line["qty"],
                    reserved_refund_minor=original["reserved_refund_minor"] - line["goods_minor"],
                    refunded_qty=original["refunded_qty"] + line["qty"],
                    refunded_goods_minor=original["refunded_goods_minor"] + line["goods_minor"],
                    version=original["version"] + 1,
                    updated_at=now,
                )
            )
        refund_id = new_id("refund")
        # wallet.post_entry只按subject_id定位账户。这个内部上下文只决定收款人；
        # 原始工作人员身份仍由operation及下方审计保存，不接受客户端传owner。
        owner_ctx = replace(ctx, subject_id=order["owner_id"], role=Role.CUSTOMER)
        entry_id, _, _ = post_entry(
            session, owner_ctx, goods + freight, "refund", operation_id, refund_id, now
        )
        session.execute(
            refunds.insert().values(
                refund_id=refund_id,
                claim_id=claim["claim_id"],
                order_id=order["order_id"],
                owner_id=order["owner_id"],
                entry_id=entry_id,
                operation_id=operation_id,
                goods_minor=goods,
                freight_minor=freight,
                refunded_at=now,
                **provenance(ctx, now),
            )
        )
        total = order["refunded_minor"] + goods + freight
        from cs_agent.activities.pricing import record_refund

        allocations = []
        for line in items_for(session, claim["claim_id"]):
            sku_id = session.scalar(select(order_items.c.sku_id).where(
                order_items.c.order_item_id == line["order_item_id"]))
            allocations.append({"sku_id": sku_id, "amount_minor": str(line["goods_minor"])})
        record_refund(session, ctx, refund_id, order["order_id"], goods + freight, now,
                      allocations=allocations)
        session.execute(
            update(orders)
            .where(orders.c.order_id == order["order_id"])
            .values(
                refunded_minor=total,
                refunded_goods_minor=order["refunded_goods_minor"] + goods,
                refunded_freight_minor=order["refunded_freight_minor"] + freight,
                trade_status="closed_refunded"
                if total == order["paid_minor"]
                else order["trade_status"],
                version=order["version"] + 1,
                updated_at=now,
            )
        )
        result = changed(
            session, ctx, claim, now, "refund.succeeded", status="refunded", released_at=now
        )
        incident = (
            session.execute(select(incidents).where(incidents.c.claim_id == claim["claim_id"]))
            .mappings()
            .first()
        )
        if incident:
            session.execute(
                update(incidents)
                .where(incidents.c.incident_id == incident["incident_id"])
                .values(status="refunded", version=incident["version"] + 1, updated_at=now)
            )
        record(
            session,
            request_id=ctx.request_id,
            action="refund.executed",
            outcome="succeeded",
            ctx=ctx,
            resource_ref=refund_id,
            summary={"version": result["version"]},
        )
        return {
            **result,
            "refund_id": refund_id,
            "entry_id": entry_id,
            "goods_minor": str(goods),
            "original_freight_minor": str(freight),
            "net_paid_minor": str(order["paid_minor"] - total),
            "original_paid_fully_refunded": total == order["paid_minor"],
        }
