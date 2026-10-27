from sqlalchemy import select, update

from cs_agent.actions.repository import execute_once, new_id, provenance
from cs_agent.audit.service import record
from cs_agent.commerce.checkout import check_version
from cs_agent.core.context import Role
from cs_agent.core.errors import AppError, not_found
from cs_agent.db.aftersales_tables import claim_items, claims, inspections, returns, reviews
from cs_agent.db.business_tables import order_items, orders, receipts
from cs_agent.identity.policies import require_role
from cs_agent.wallet.service import event

from .rules import (
    allocate_units,
    clean_text,
    conflict,
    evidence_refs,
    original_freight,
    policy_for,
    within_window,
)

RELEASED = ("rejected", "withdrawn")


def scope(table, ctx):
    return (
        table.c.store_id == ctx.store_id,
        table.c.data_batch_id == ctx.data_batch_id,
        table.c.batch_generation == ctx.batch_generation,
    )


def order_row(session, ctx, order_id, *, lock=False):
    statement = select(orders).where(orders.c.order_id == order_id, *scope(orders, ctx))
    if ctx.role == Role.CUSTOMER:
        statement = statement.where(orders.c.owner_id == ctx.subject_id)
    row = session.execute(statement.with_for_update() if lock else statement).mappings().first()
    if row is None:
        raise not_found()
    return row


def case_row(session, ctx, ref, *, lock=False, kind=None):
    statement = select(claims).where(claims.c.claim_id == ref, *scope(claims, ctx))
    if ctx.role == Role.CUSTOMER:
        statement = statement.where(claims.c.owner_id == ctx.subject_id)
    if kind:
        statement = statement.where(claims.c.case_kind == kind)
    row = session.execute(statement).mappings().first()
    if row is None:
        raise not_found()
    if lock:
        # 员工遵循工单域锁序：工单→订单→申请→库存→本人钱包。
        # 顾客只锁订单/申请，不会在持有订单锁时等待工单锁。
        if ctx.role in {Role.SUPPORT, Role.SUPERVISOR}:
            from cs_agent.support.service import require_ticket_scope

            require_ticket_scope(session, ctx, row["ticket_id"], lock=True)
        order_row(session, ctx, row["order_id"], lock=True)
        row = session.execute(statement.with_for_update()).mappings().one()
    return row


def items_for(session, ref):
    return (
        session.execute(
            select(claim_items)
            .where(claim_items.c.claim_id == ref)
            .order_by(claim_items.c.order_item_id)
        )
        .mappings()
        .all()
    )


def require_assignment(session, ctx, row, *, exception=False):
    from cs_agent.support.service import require_ticket_scope

    if exception:
        require_role(ctx, Role.SUPERVISOR)
    else:
        require_role(ctx, Role.SUPPORT)
    return require_ticket_scope(
        session, ctx, row["ticket_id"], lock=True, allow_supervisor=exception
    )


def changed(session, ctx, row, now, action, **values):
    values.update(version=row["version"] + 1, updated_at=now)
    session.execute(update(claims).where(claims.c.claim_id == row["claim_id"]).values(**values))
    event(
        session,
        ctx,
        action,
        row["claim_id"],
        values["version"],
        f"{row['claim_id']}:{values['version']}",
        {
            "claim_ref": row["claim_id"],
            "order_ref": row["order_id"],
            "status": values.get("status", row["status"]),
        },
        now,
    )
    record(
        session,
        request_id=ctx.request_id,
        action=action,
        outcome="succeeded",
        ctx=ctx,
        resource_ref=row["claim_id"],
        summary={"version": values["version"]},
    )
    return {
        "business_ref": row["claim_id"],
        "version": values["version"],
        "status_name": values.get("status", row["status"]),
    }


def calculate(session, ctx, order, quantities, freight):
    if not isinstance(quantities, dict) or not quantities or len(quantities) > 100:
        raise AppError("invalid_input", 422, "请选择订单明细及数量")
    lines = (
        session.execute(
            select(order_items)
            .where(order_items.c.order_id == order["order_id"])
            .order_by(order_items.c.order_item_id)
        )
        .mappings()
        .all()
    )
    by_ref = {line["order_item_id"]: line for line in lines}
    if set(quantities) - set(by_ref):
        raise not_found()
    # 被拒绝/撤销的单位释放；已退款单位永久占用原单位序号。
    occupied = {}
    rows = session.execute(
        select(claim_items.c.order_item_id, claim_items.c.unit_indices)
        .join(claims, claims.c.claim_id == claim_items.c.claim_id)
        .where(claims.c.order_id == order["order_id"], claims.c.status.not_in(RELEASED))
    ).mappings()
    for row in rows:
        occupied.setdefault(row["order_item_id"], set()).update(row["unit_indices"])
    result = []
    for ref in sorted(quantities):
        line = by_ref[ref]
        units, amount = allocate_units(line, quantities[ref], occupied.get(ref, set()))
        result.append(
            {
                "order_item_id": ref,
                "qty": quantities[ref],
                "goods_minor": str(amount),
                "unit_indices": units,
                "allocation_revision": line["allocation_revision"],
                "product_name": line["product_name"],
            }
        )
    existing = session.execute(
        select(claims.c.goods_minor, claims.c.freight_minor).where(
            claims.c.order_id == order["order_id"], claims.c.status.not_in((*RELEASED, "refunded"))
        )
    ).all()
    goods = sum(int(line["goods_minor"]) for line in result)
    if goods + freight <= 0:
        conflict("本次申请没有可退金额")
    if (
        freight + sum(row.freight_minor for row in existing) + order["refunded_freight_minor"]
        > order["freight_minor"]
    ):
        conflict("原订单运费已退或已被有效申请占用")
    if (
        goods
        + freight
        + sum(row.goods_minor + row.freight_minor for row in existing)
        + order["refunded_minor"]
        > order["paid_minor"]
    ):
        conflict("累计退款与有效申请占用超过原实付")
    return result, goods


def reserve_claim(
    session,
    ctx,
    order,
    normalized,
    confirmation,
    op_id,
    now,
    *,
    ticket_id=None,
    kind="formal_return",
):
    lines, goods = calculate(
        session, ctx, order, normalized["quantities"], normalized["freight_minor"]
    )
    # 提案只展示；确认时在订单锁内重算。尾差/被占用单位变化必须重新确认。
    if lines != normalized["lines"] or str(goods) != normalized["goods_minor"]:
        conflict("可退明细或分摊已变化，请重新确认")
    ref = new_id("aftersale" if kind == "formal_return" else "transportrefund")
    if ticket_id is None:
        from cs_agent.support.service import create_case_ticket

        ticket_id = create_case_ticket(
            session,
            ctx,
            "退货退款申请",
            "aftersale",
            now,
            order_id=order["order_id"],
            case_kind="aftersale",
            case_ref=ref,
        )
    session.execute(
        claims.insert().values(
            claim_id=ref,
            case_kind=kind,
            order_id=order["order_id"],
            owner_id=order["owner_id"],
            ticket_id=ticket_id,
            receipt_id=normalized.get("receipt_id"),
            confirmation_id=confirmation["confirmation_id"],
            operation_id=op_id,
            reason=normalized["reason"],
            description=normalized["description"],
            policy_revision=normalized["policy_revision"],
            policy_snapshot=normalized["policy_snapshot"],
            goods_minor=goods,
            freight_minor=normalized["freight_minor"],
            return_freight_responsibility="customer"
            if normalized["reason"] == "ordinary"
            else ("none" if kind == "transport_refund" else "pending"),
            status="needs_dispute" if normalized.get("needs_supervisor") else "reviewing",
            needs_supervisor=int(normalized.get("needs_supervisor", False)),
            **provenance(ctx, now),
        )
    )
    for line in lines:
        session.execute(
            claim_items.insert().values(
                claim_item_id=new_id("claimitem"),
                claim_id=ref,
                **{
                    key: line[key]
                    for key in ("order_item_id", "qty", "unit_indices", "allocation_revision")
                },
                goods_minor=int(line["goods_minor"]),
                **provenance(ctx, now),
            )
        )
        session.execute(
            update(order_items)
            .where(order_items.c.order_item_id == line["order_item_id"])
            .values(
                reserved_refund_qty=order_items.c.reserved_refund_qty + line["qty"],
                reserved_refund_minor=order_items.c.reserved_refund_minor
                + int(line["goods_minor"]),
                version=order_items.c.version + 1,
                updated_at=now,
            )
        )
    session.execute(
        update(orders)
        .where(orders.c.order_id == order["order_id"])
        .values(version=order["version"] + 1, updated_at=now)
    )
    event(
        session,
        ctx,
        "aftersale.requested" if kind == "formal_return" else "transport.refund.requested",
        ref,
        1,
        ref,
        {
            "claim_ref": ref,
            "order_ref": order["order_id"],
            "ticket_ref": ticket_id,
            "goods_minor": str(goods),
            "original_freight_minor": str(normalized["freight_minor"]),
        },
        now,
    )
    record(
        session,
        request_id=ctx.request_id,
        action="refund.claim.requested",
        outcome="succeeded",
        ctx=ctx,
        resource_ref=ref,
        summary={"version": 1},
    )
    return {
        "business_ref": ref,
        "claim_id": ref,
        "ticket_id": ticket_id,
        "version": 1,
        "goods_minor": str(goods),
        "original_freight_minor": str(normalized["freight_minor"]),
        "status_name": "needs_dispute" if normalized.get("needs_supervisor") else "reviewing",
    }


def release_claim(session, ctx, row, status, now):
    if row["status"] in (*RELEASED, "refunded"):
        conflict("申请已结清，不能重复释放额度")
    for line in items_for(session, row["claim_id"]):
        session.execute(
            update(order_items)
            .where(order_items.c.order_item_id == line["order_item_id"])
            .values(
                reserved_refund_qty=order_items.c.reserved_refund_qty - line["qty"],
                reserved_refund_minor=order_items.c.reserved_refund_minor - line["goods_minor"],
                version=order_items.c.version + 1,
                updated_at=now,
            )
        )
    session.execute(
        update(orders)
        .where(orders.c.order_id == row["order_id"])
        .values(version=orders.c.version + 1, updated_at=now)
    )
    return changed(session, ctx, row, now, "refund.claim.released", status=status, released_at=now)


class AftersaleService:
    def prepare_action(self, session, ctx, action, resource_ref, parameters, expected_version, now):
        require_role(ctx, Role.CUSTOMER)
        if action != "aftersale.apply" or set(parameters) - {
            "quantities",
            "reason",
            "description",
            "request_original_freight",
        }:
            raise AppError("invalid_input", 422, "售后只接受订单明细、数量与原因，不接受自填金额")
        order = order_row(session, ctx, resource_ref)
        check_version(order, expected_version)
        receipt = (
            session.execute(
                select(receipts).where(
                    receipts.c.order_id == resource_ref,
                    receipts.c.owner_id == ctx.subject_id,
                    *scope(receipts, ctx),
                )
            )
            .mappings()
            .first()
        )
        if (
            not receipt
            or order["fulfillment_status"] != "received_by_customer"
            or order["trade_status"] != "completed"
        ):
            conflict("正式售后须先有本人真实确认收货记录；未收到货请走运输异常")
        reason = parameters.get("reason")
        if reason not in {"ordinary", "quality", "wrong_item"}:
            raise AppError("invalid_input", 422, "请选择普通退货、质量问题或错发")
        requested = parameters.get("request_original_freight", False)
        if type(requested) is not bool:
            raise AppError("invalid_input", 422, "运费选项须为布尔值")
        policy = policy_for(order)
        in_window = (
            within_window(receipt["confirmed_at"], now, policy) if reason == "ordinary" else True
        )
        freight = original_freight(order, reason, requested)
        lines, goods = calculate(session, ctx, order, parameters.get("quantities"), freight)
        normalized = {
            "order_ref": resource_ref,
            "quantities": {line["order_item_id"]: line["qty"] for line in lines},
            "reason": reason,
            "description": clean_text(parameters.get("description", "")),
            "request_original_freight": requested,
            "receipt_id": receipt["receipt_id"],
            "policy_revision": policy["aftersale_revision"],
            "policy_snapshot": policy,
            "lines": lines,
            "goods_minor": str(goods),
            "freight_minor": freight,
            "needs_supervisor": not in_window,
        }
        summary = {
            "action": action,
            "resource_ref": resource_ref,
            "display_no": order["display_no"],
            "expected_version": expected_version,
            "items": lines,
            "goods_minor": str(goods),
            "original_freight_minor": str(freight),
            "currency": "CNY",
            "reason": reason,
            "needs_supervisor": not in_window,
            "notice": "申请与审核不会到账；需退货及核验，超期需主管有据处理",
        }
        return normalized, summary

    def apply_confirmed_action(self, session, ctx, confirmation_row, op_id, now):
        parameters = confirmation_row["parameters"]
        order = order_row(session, ctx, confirmation_row["resource_ref"], lock=True)
        refreshed, _ = self.prepare_action(
            session,
            ctx,
            "aftersale.apply",
            order["order_id"],
            {
                key: parameters[key]
                for key in ("quantities", "reason", "description", "request_original_freight")
            },
            confirmation_row["expected_version"],
            now,
        )
        if refreshed != parameters:
            conflict("申请时限或金额事实已变化，请重新确认")
        return reserve_claim(session, ctx, order, parameters, confirmation_row, op_id, now)

    def read(self, session, ctx, ref):
        row = case_row(session, ctx, ref, kind="formal_return")
        if ctx.role == Role.SUPPORT:
            require_assignment(session, ctx, row)
        elif ctx.role not in {Role.CUSTOMER, Role.SUPERVISOR, Role.SHOP_ADMIN}:
            raise not_found()
        result = {
            key: row[key]
            for key in (
                "claim_id",
                "case_kind",
                "order_id",
                "ticket_id",
                "reason",
                "description",
                "policy_revision",
                "return_freight_responsibility",
                "status",
                "version",
                "needs_supervisor",
                "verified_kind",
            )
        }
        result.update(
            goods_minor=str(row["goods_minor"]),
            original_freight_minor=str(row["freight_minor"]),
            items=[
                {
                    **dict(line),
                    "goods_minor": str(line["goods_minor"]),
                    "created_at": line["created_at"].isoformat(),
                    "updated_at": line["updated_at"].isoformat(),
                }
                for line in items_for(session, ref)
            ],
        )
        result["reviews"] = [
            {
                "decision": value["decision"],
                "basis": value["basis"],
                "evidence_refs": value["evidence_refs"],
                "created_at": value["created_at"].isoformat(),
            }
            for value in session.execute(
                select(reviews).where(reviews.c.claim_id == ref).order_by(reviews.c.created_at)
            ).mappings()
        ]
        result["return_shipment"] = (
            session.execute(
                select(returns.c.carrier, returns.c.tracking_no).where(returns.c.claim_id == ref)
            )
            .mappings()
            .first()
        )
        result["inspection"] = (
            session.execute(
                select(
                    inspections.c.lines, inspections.c.evidence_refs, inspections.c.matched
                ).where(inspections.c.claim_id == ref)
            )
            .mappings()
            .first()
        )
        return result

    def list(self, session, ctx, *, limit=20, offset=0):
        fields = (
            "claim_id",
            "order_id",
            "ticket_id",
            "status",
            "version",
            "reason",
            "goods_minor",
            "freight_minor",
        )
        statement = select(*(claims.c[key] for key in fields)).where(
            claims.c.case_kind == "formal_return", *scope(claims, ctx)
        )
        if ctx.role == Role.CUSTOMER:
            statement = statement.where(claims.c.owner_id == ctx.subject_id)
        elif ctx.role == Role.SUPPORT:
            from cs_agent.support.service import assigned_ticket_ids

            statement = statement.where(claims.c.ticket_id.in_(assigned_ticket_ids(ctx)))
        elif ctx.role not in {Role.SHOP_ADMIN, Role.SUPERVISOR}:
            raise not_found()
        return [
            {
                **{key: row[key] for key in fields if key not in {"goods_minor", "freight_minor"}},
                "goods_minor": str(row["goods_minor"]),
                "original_freight_minor": str(row["freight_minor"]),
            }
            for row in session.execute(
                statement.order_by(claims.c.created_at.desc(), claims.c.claim_id)
                .limit(limit)
                .offset(offset)
            ).mappings()
        ]

    def withdraw(self, session, ctx, ref, expected_version, key, now):
        require_role(ctx, Role.CUSTOMER)

        def execute(op_id):
            row = case_row(session, ctx, ref, lock=True, kind="formal_return")
            check_version(row, expected_version)
            if (
                row["status"] not in {"reviewing", "awaiting_return", "needs_dispute"}
                or row["verified_kind"]
            ):
                conflict("已寄出、已核验或已结清的申请不能撤销")
            if session.execute(
                select(returns.c.return_id).where(returns.c.claim_id == ref)
            ).first():
                conflict("退货已经寄出，需人工核对，不能直接释放额度")
            return release_claim(session, ctx, row, "withdrawn", now)

        return execute_once(
            session,
            ctx,
            "aftersale.withdraw",
            key,
            {"claim_ref": ref, "expected_version": expected_version},
            now,
            execute,
            resource_ref=ref,
        )

    def review(self, session, ctx, ref, decision, basis, evidence, expected_version, key, now):
        basis, evidence = clean_text(basis), evidence_refs(evidence)

        def execute(op_id):
            row = case_row(session, ctx, ref, lock=True)
            check_version(row, expected_version)
            exception = row["status"] == "needs_dispute"
            require_assignment(session, ctx, row, exception=exception)
            if row["status"] not in {"reviewing", "needs_dispute"} or decision not in {
                "approve",
                "reject",
            }:
                conflict("当前申请不能重复审核")
            session.execute(
                reviews.insert().values(
                    review_id=new_id("review"),
                    claim_id=ref,
                    actor_id=ctx.subject_id,
                    decision=decision,
                    basis=basis,
                    evidence_refs=evidence,
                    operation_id=op_id,
                    **provenance(ctx, now),
                )
            )
            if decision == "reject":
                # 已有实收事实的争议拒绝不释放，必须保留核对责任。
                if (
                    row["verified_kind"]
                    or session.execute(
                        select(inspections.c.inspection_id).where(inspections.c.claim_id == ref)
                    ).first()
                ):
                    conflict("已核验实收的争议不能直接拒绝释放，请先核对处理依据")
                return release_claim(session, ctx, row, "rejected", now)
            responsibility = row["return_freight_responsibility"]
            if responsibility == "pending":
                responsibility = row["policy_snapshot"].get(
                    f"{row['reason']}_return_freight_responsibility"
                )
                if responsibility not in {"customer", "shop"}:
                    conflict("退回运费责任缺少历史适用政策，请核对后处理")
            status = "awaiting_return" if row["case_kind"] == "formal_return" else "refund_reviewed"
            if exception and row["verified_kind"]:
                status = "refund_reviewed"
            verification = (
                "return_dispute"
                if exception and row["verified_kind"] == "return"
                else row["verified_kind"]
            )
            result = changed(
                session,
                ctx,
                row,
                now,
                "aftersale.reviewed",
                status=status,
                reviewer_id=ctx.subject_id,
                approved_at=now,
                return_freight_responsibility=responsibility,
                verified_kind=verification,
            )
            if status == "refund_reviewed" and row["verified_kind"]:
                from .refunds import RefundService

                current = case_row(session, ctx, ref)
                result.update(RefundService().execute(session, ctx, current, op_id, now))
            return result

        return execute_once(
            session,
            ctx,
            "refund.claim.review",
            key,
            {
                "claim_ref": ref,
                "decision": decision,
                "basis": basis,
                "evidence": evidence,
                "expected_version": expected_version,
            },
            now,
            execute,
            resource_ref=ref,
        )

    def return_shipment(self, session, ctx, ref, carrier, tracking_no, expected_version, key, now):
        require_role(ctx, Role.CUSTOMER)
        carrier = clean_text(carrier, 60)
        import re

        if not isinstance(tracking_no, str) or not re.fullmatch(r"[A-Za-z0-9-]{6,80}", tracking_no):
            raise AppError("invalid_input", 422, "请输入合法的退货单号")

        def execute(op_id):
            row = case_row(session, ctx, ref, lock=True, kind="formal_return")
            check_version(row, expected_version)
            if row["status"] != "awaiting_return" or not row["approved_at"]:
                conflict("请先等待审核通过")
            session.execute(
                returns.insert().values(
                    return_id=new_id("return"),
                    claim_id=ref,
                    carrier=carrier,
                    tracking_no=tracking_no,
                    operation_id=op_id,
                    **provenance(ctx, now),
                )
            )
            return changed(
                session, ctx, row, now, "aftersale.return.shipped", status="return_in_transit"
            )

        return execute_once(
            session,
            ctx,
            "aftersale.return.shipment",
            key,
            {
                "claim_ref": ref,
                "carrier": carrier,
                "tracking_no": tracking_no,
                "expected_version": expected_version,
            },
            now,
            execute,
            resource_ref=ref,
        )
