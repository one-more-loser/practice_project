from sqlalchemy import select

from cs_agent.actions.repository import execute_once, new_id, provenance
from cs_agent.catalog.service import sku_row
from cs_agent.commerce.checkout import check_version
from cs_agent.core.context import Role
from cs_agent.db.aftersales_tables import inspections
from cs_agent.db.business_tables import order_items
from cs_agent.identity.policies import require_role
from cs_agent.inventory.service import move_stock

from .refunds import RefundService
from .rules import conflict, evidence_refs, inspection_lines
from .service import case_row, changed, items_for


class InspectionService:
    def inspect(self, session, ctx, ref, lines, evidence, expected_version, key, now):
        require_role(ctx, Role.SHOP_ADMIN)
        evidence = evidence_refs(evidence)

        def execute(op_id):
            row = case_row(session, ctx, ref, lock=True)
            check_version(row, expected_version)
            allowed = (
                {"return_in_transit"}
                if row["case_kind"] == "formal_return"
                else {"refund_reviewed"}
            )
            if row["status"] not in allowed or not row["approved_at"]:
                conflict("请先完成分派审核与退货流程")
            if row["reviewer_id"] == ctx.subject_id:
                conflict("审核客服不能同时作为店铺收退核验人")
            if session.execute(
                select(inspections.c.inspection_id).where(inspections.c.claim_id == ref)
            ).first():
                conflict("收退已核验，请核对原结果，不能重复增加库存或退款")
            items = items_for(session, ref)
            matched = inspection_lines(items, lines)
            session.execute(
                inspections.insert().values(
                    inspection_id=new_id("inspection"),
                    claim_id=ref,
                    inspector_id=ctx.subject_id,
                    lines=lines,
                    evidence_refs=evidence,
                    operation_id=op_id,
                    matched=int(matched),
                    **provenance(ctx, now),
                )
            )
            # 入库按SKU排序，避免相同订单含重复SKU时死锁；只计实际可售量。
            sku_quantities = {}
            for item in items:
                sku_id = session.execute(
                    select(order_items.c.sku_id).where(
                        order_items.c.order_item_id == item["order_item_id"]
                    )
                ).scalar_one()
                sku_quantities[sku_id] = (
                    sku_quantities.get(sku_id, 0) + lines[item["order_item_id"]]["saleable_qty"]
                )
            for sku_id, quantity in sorted(sku_quantities.items()):
                if quantity:
                    stock = sku_row(session, sku_id, ctx.store_id, lock=True)
                    move_stock(
                        session,
                        ctx,
                        stock,
                        quantity,
                        0,
                        "return_received",
                        ref,
                        op_id,
                        now,
                        "verified_saleable_return",
                    )
            result = changed(
                session,
                ctx,
                row,
                now,
                "aftersale.return.inspected",
                status="refund_reviewed" if matched else "needs_dispute",
                verified_kind="return",
                needs_supervisor=0 if matched else 1,
            )
            if matched:
                result.update(
                    RefundService().execute(session, ctx, case_row(session, ctx, ref), op_id, now)
                )
            return result

        return execute_once(
            session,
            ctx,
            "refund.return.inspect",
            key,
            {
                "claim_ref": ref,
                "lines": lines,
                "evidence": evidence,
                "expected_version": expected_version,
            },
            now,
            execute,
            resource_ref=ref,
        )
