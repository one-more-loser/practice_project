from sqlalchemy import select, update

from cs_agent.actions.repository import execute_once, fingerprint, new_id, provenance
from cs_agent.aftersales.refunds import RefundService
from cs_agent.aftersales.rules import (
    clean_text,
    conflict,
    evidence_refs,
    original_freight,
    policy_for,
)
from cs_agent.aftersales.service import (
    AftersaleService,
    calculate,
    case_row,
    changed,
    items_for,
    order_row,
    reserve_claim,
    scope,
)
from cs_agent.commerce.checkout import check_version
from cs_agent.core.context import Role
from cs_agent.core.errors import AppError, not_found
from cs_agent.db.aftersales_tables import claims, incidents, reviews
from cs_agent.db.business_tables import order_items, receipts, shipments
from cs_agent.identity.policies import require_role
from cs_agent.wallet.service import event


def incident_row(session, ctx, ref, *, lock=False):
    statement = select(incidents).where(incidents.c.incident_id == ref, *scope(incidents, ctx))
    if ctx.role == Role.CUSTOMER:
        statement = statement.where(incidents.c.owner_id == ctx.subject_id)
    row = session.execute(statement).mappings().first()
    if row is None:
        raise not_found()
    if ctx.role in {Role.SUPPORT, Role.SUPERVISOR}:
        from cs_agent.support.service import require_ticket_scope

        require_ticket_scope(session, ctx, row["ticket_id"], lock=lock)
    elif ctx.role not in {Role.CUSTOMER, Role.SHOP_ADMIN}:
        raise not_found()
    if lock:
        order_row(session, ctx, row["order_id"], lock=True)
        row = session.execute(statement.with_for_update()).mappings().one()
    return row


def unreceived_order(session, ctx, order_id, *, lock=False):
    order = order_row(session, ctx, order_id, lock=lock)
    if (
        order["trade_status"] != "paid"
        or order["fulfillment_status"] != "in_transit"
        or session.execute(
            select(receipts.c.receipt_id).where(receipts.c.order_id == order_id)
        ).first()
    ):
        conflict("运输异常仅处理运输中且无本人收货记录的订单")
    return order


class TransportService:
    def prepare_action(self, session, ctx, action, resource_ref, parameters, expected_version, now):
        require_role(ctx, Role.CUSTOMER)
        if action == "transport.incident.create":
            if set(parameters) - {"kind", "description"} or parameters.get("kind") not in {
                "inquiry",
                "expedite",
                "intercept",
                "lost",
            }:
                raise AppError("invalid_input", 422, "请选择查询、催件、拦截或丢件诉求")
            order = unreceived_order(session, ctx, resource_ref)
            check_version(order, expected_version)
            shipment = (
                session.execute(select(shipments).where(shipments.c.order_id == resource_ref))
                .mappings()
                .first()
            )
            if not shipment:
                conflict("缺少真实发货记录，请人工核对")
            normalized = {
                "order_ref": resource_ref,
                "shipment_ref": shipment["shipment_id"],
                "kind": parameters["kind"],
                "description": clean_text(parameters.get("description", "")),
            }
            summary = {
                "action": action,
                "resource_ref": resource_ref,
                "display_no": order["display_no"],
                "expected_version": expected_version,
                "kind": normalized["kind"],
                "notice": "建立异常工单不产生退款；退款诉求须另行确认",
            }
            return normalized, summary
        if action != "transport.refund.request" or set(parameters) - {
            "description",
            "request_original_freight",
        }:
            raise AppError("invalid_input", 422, "运输退款只接受原因与运费诉求，不接受自填金额")
        incident = incident_row(session, ctx, resource_ref)
        check_version(incident, expected_version)
        if incident["status"] in {"refunded", "closed"} or incident["claim_id"]:
            conflict("已有退款申请，请核对原申请")
        order = unreceived_order(session, ctx, incident["order_id"])
        policy = policy_for(order)
        requested = parameters.get("request_original_freight", False)
        if type(requested) is not bool:
            raise AppError("invalid_input", 422, "运费选项须为布尔值")
        freight = original_freight(order, "transport", requested)
        quantities = {
            row["order_item_id"]: row["ordered_qty"]
            - row["refunded_qty"]
            - row["reserved_refund_qty"]
            for row in session.execute(
                select(order_items).where(order_items.c.order_id == order["order_id"])
            ).mappings()
        }
        quantities = {ref: qty for ref, qty in quantities.items() if qty > 0}
        lines, goods = calculate(session, ctx, order, quantities, freight)
        normalized = {
            "order_ref": order["order_id"],
            "incident_ref": resource_ref,
            "quantities": quantities,
            "reason": "transport",
            "description": clean_text(parameters.get("description", "")),
            "request_original_freight": requested,
            "policy_revision": policy["aftersale_revision"],
            "policy_snapshot": policy,
            "lines": lines,
            "goods_minor": str(goods),
            "freight_minor": freight,
            "needs_supervisor": False,
        }
        return normalized, {
            "action": action,
            "resource_ref": resource_ref,
            "expected_version": expected_version,
            "display_no": order["display_no"],
            "items": lines,
            "goods_minor": str(goods),
            "original_freight_minor": str(freight),
            "currency": "CNY",
            "notice": "分派客服审核后，仍须核实实际退回或丢件责任；拦截申请及失败不退款",
        }

    def apply_confirmed_action(self, session, ctx, confirmation_row, op_id, now):
        parameters, action = confirmation_row["parameters"], confirmation_row["action"]
        if action == "transport.incident.create":
            order = unreceived_order(session, ctx, confirmation_row["resource_ref"], lock=True)
            normalized, _ = self.prepare_action(
                session,
                ctx,
                action,
                order["order_id"],
                {key: parameters[key] for key in ("kind", "description")},
                confirmation_row["expected_version"],
                now,
            )
            if normalized != parameters:
                conflict("运输事实已变化，请重新确认")
            ref = new_id("incident")
            from cs_agent.support.service import create_case_ticket

            ticket_id = create_case_ticket(
                session,
                ctx,
                "运输异常",
                "transport",
                now,
                order_id=order["order_id"],
                case_kind="transport",
                case_ref=ref,
            )
            session.execute(
                incidents.insert().values(
                    incident_id=ref,
                    order_id=order["order_id"],
                    owner_id=order["owner_id"],
                    shipment_id=parameters["shipment_ref"],
                    ticket_id=ticket_id,
                    kind=parameters["kind"],
                    description=parameters["description"],
                    status="open",
                    confirmation_id=confirmation_row["confirmation_id"],
                    operation_id=op_id,
                    facts=[],
                    **provenance(ctx, now),
                )
            )
            event(
                session,
                ctx,
                "transport.incident.created",
                ref,
                1,
                ref,
                {"incident_ref": ref, "order_ref": order["order_id"], "ticket_ref": ticket_id},
                now,
            )
            return {
                "business_ref": ref,
                "incident_id": ref,
                "ticket_id": ticket_id,
                "version": 1,
                "status_name": "open",
            }
        if action != "transport.refund.request":
            raise AppError("invalid_input", 422, "运输动作不符合契约")
        incident = incident_row(session, ctx, confirmation_row["resource_ref"], lock=True)
        normalized, _ = self.prepare_action(
            session,
            ctx,
            action,
            incident["incident_id"],
            {key: parameters[key] for key in ("description", "request_original_freight")},
            confirmation_row["expected_version"],
            now,
        )
        if normalized != parameters:
            conflict("可退款事实已变化，请重新确认")
        order = unreceived_order(session, ctx, incident["order_id"], lock=True)
        result = reserve_claim(
            session,
            ctx,
            order,
            normalized,
            confirmation_row,
            op_id,
            now,
            ticket_id=incident["ticket_id"],
            kind="transport_refund",
        )
        session.execute(
            update(incidents)
            .where(incidents.c.incident_id == incident["incident_id"])
            .values(claim_id=result["claim_id"], version=incident["version"] + 1, updated_at=now)
        )
        return {
            **result,
            "incident_id": incident["incident_id"],
            "incident_version": incident["version"] + 1,
        }

    def read(self, session, ctx, ref):
        row = incident_row(session, ctx, ref)
        result = {
            key: row[key]
            for key in (
                "incident_id",
                "order_id",
                "shipment_id",
                "ticket_id",
                "kind",
                "description",
                "status",
                "version",
                "claim_id",
                "facts",
            )
        }
        if row["claim_id"]:
            claim = case_row(session, ctx, row["claim_id"])
            result["refund_claim"] = {
                key: claim[key]
                for key in (
                    "claim_id",
                    "status",
                    "version",
                    "verified_kind",
                    "return_freight_responsibility",
                )
            }
            result["refund_claim"].update(
                goods_minor=str(claim["goods_minor"]),
                original_freight_minor=str(claim["freight_minor"]),
                items=[
                    {
                        "order_item_id": item["order_item_id"],
                        "qty": item["qty"],
                        "goods_minor": str(item["goods_minor"]),
                    }
                    for item in items_for(session, claim["claim_id"])
                ],
            )
        return result

    def list(self, session, ctx, *, limit=20, offset=0):
        fields = ("incident_id", "order_id", "ticket_id", "status", "version", "kind", "claim_id")
        statement = select(*(incidents.c[key] for key in fields)).where(*scope(incidents, ctx))
        if ctx.role == Role.CUSTOMER:
            statement = statement.where(incidents.c.owner_id == ctx.subject_id)
        elif ctx.role == Role.SUPPORT:
            from cs_agent.support.service import assigned_ticket_ids

            statement = statement.where(incidents.c.ticket_id.in_(assigned_ticket_ids(ctx)))
        elif ctx.role not in {Role.SUPERVISOR, Role.SHOP_ADMIN}:
            raise not_found()
        rows = (
            session.execute(
                statement.order_by(incidents.c.created_at.desc(), incidents.c.incident_id)
                .limit(limit)
                .offset(offset)
            )
            .mappings()
            .all()
        )
        ids = [row["claim_id"] for row in rows if row["claim_id"]]
        related = (
            {
                row["claim_id"]: {
                    "claim_id": row["claim_id"],
                    "status": row["status"],
                    "version": row["version"],
                    "goods_minor": str(row["goods_minor"]),
                    "original_freight_minor": str(row["freight_minor"]),
                }
                for row in session.execute(
                    select(claims).where(claims.c.claim_id.in_(ids), *scope(claims, ctx))
                ).mappings()
            }
            if ids
            else {}
        )
        return [{**dict(row), "refund_claim": related.get(row["claim_id"])} for row in rows]

    def review(self, session, ctx, ref, decision, basis, evidence, expected_version, key, now):
        basis, evidence = clean_text(basis), evidence_refs(evidence)

        def execute(op_id):
            incident = incident_row(session, ctx, ref, lock=True)
            check_version(incident, expected_version)
            unreceived_order(session, ctx, incident["order_id"], lock=True)
            from cs_agent.support.service import require_ticket_scope

            exception = incident["status"] == "needs_dispute"
            require_role(ctx, Role.SUPERVISOR if exception else Role.SUPPORT)
            require_ticket_scope(
                session, ctx, incident["ticket_id"], lock=True, allow_supervisor=exception
            )
            if decision in {"approve", "reject"}:
                if not incident["claim_id"]:
                    conflict("尚无顾客确认的退款诉求")
                claim = case_row(session, ctx, incident["claim_id"])
                # 已经持有同一工单/订单锁；嵌套幂等保存各自不可变结果。
                result, status = AftersaleService().review(
                    session,
                    ctx,
                    claim["claim_id"],
                    decision,
                    basis,
                    evidence,
                    claim["version"],
                    "nested_" + fingerprint({"key": key, "claim": claim["claim_id"]}),
                    now,
                )
                if status >= 400:
                    raise AppError(result["error"]["code"], status, result["error"]["message"])
                state = "verifying" if decision == "approve" else "closed"
            elif decision in {
                "expedited",
                "intercept_requested",
                "intercept_failed",
                "loss_pending",
                "loss_verified",
                "dispute",
                "close",
            }:
                state = {
                    "expedited": "verifying",
                    "intercept_requested": "awaiting_intercept",
                    "intercept_failed": "intercept_failed",
                    "loss_pending": "awaiting_liability",
                    "loss_verified": "verified_loss",
                    "dispute": "needs_dispute",
                    "close": "closed",
                }[decision]
                result = {"business_ref": ref}
                if incident["status"] in {"closed", "refunded"}:
                    conflict("异常已经结清")
                if decision == "intercept_failed" and incident["status"] != "awaiting_intercept":
                    conflict("尚未申请拦截，不能登记拦截失败")
                if decision == "close" and incident["claim_id"]:
                    conflict("存在退款申请，请核对申请后结清")
                if decision == "dispute" and incident["claim_id"]:
                    claim = case_row(session, ctx, incident["claim_id"])
                    if claim["status"] in {"refunded", "rejected", "withdrawn"}:
                        conflict("退款申请已结清，不能回到审核状态")
                    changed(
                        session,
                        ctx,
                        claim,
                        now,
                        "transport.dispute",
                        status="needs_dispute",
                        needs_supervisor=1,
                    )
                if decision == "loss_verified":
                    if not incident["claim_id"]:
                        conflict("须先取得顾客明确退款确认及人工审批")
                    claim = case_row(session, ctx, incident["claim_id"])
                    if claim["status"] != "refund_reviewed" or not claim["approved_at"]:
                        conflict("退款申请尚未完成分派审核")
                    if not any(
                        value.startswith("shipment:") or value.startswith("evidence:")
                        for value in evidence
                    ):
                        conflict("丢件责任须有运输核验依据")
                    session.execute(
                        reviews.insert().values(
                            review_id=new_id("liability"),
                            claim_id=claim["claim_id"],
                            actor_id=ctx.subject_id,
                            decision="loss_verified",
                            basis=basis,
                            evidence_refs=evidence,
                            operation_id=op_id,
                            **provenance(ctx, now),
                        )
                    )
                    changed(
                        session, ctx, claim, now, "transport.loss.verified", verified_kind="loss"
                    )
                    result.update(
                        RefundService().execute(
                            session, ctx, case_row(session, ctx, claim["claim_id"]), op_id, now
                        )
                    )
                    state = "refunded"
            else:
                raise AppError("invalid_input", 422, "运输核验结论不符合契约")
            facts = [
                *incident["facts"],
                {
                    "decision": decision,
                    "basis": basis,
                    "evidence_refs": evidence,
                    "actor_ref": ctx.subject_id,
                    "occurred_at": now.isoformat(),
                },
            ]
            session.execute(
                update(incidents)
                .where(incidents.c.incident_id == ref)
                .values(status=state, facts=facts, version=incident["version"] + 1, updated_at=now)
            )
            event(
                session,
                ctx,
                "transport.incident.reviewed",
                ref,
                incident["version"] + 1,
                f"{ref}:{incident['version'] + 1}",
                {"incident_ref": ref, "status": state},
                now,
            )
            return {
                **result,
                "incident_id": ref,
                "incident_version": incident["version"] + 1,
                "incident_status": state,
            }

        return execute_once(
            session,
            ctx,
            "transport.incident.review",
            key,
            {
                "incident_ref": ref,
                "decision": decision,
                "basis": basis,
                "evidence": evidence,
                "expected_version": expected_version,
            },
            now,
            execute,
            resource_ref=ref,
        )
