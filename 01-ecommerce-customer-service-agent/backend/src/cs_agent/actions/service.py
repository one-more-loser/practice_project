from datetime import timedelta

from sqlalchemy import select, update

from cs_agent.actions.repository import execute_once, fingerprint, new_id, provenance
from cs_agent.audit.service import record
from cs_agent.commerce.checkout import (
    CheckoutService,
    check_version,
    contact_from,
    owned_order,
    quote_row,
    shipping_input_row,
    validate_quote,
)
from cs_agent.commerce.expiration import release_unpaid
from cs_agent.commerce.fulfillment import FulfillmentService
from cs_agent.core.context import Role
from cs_agent.core.errors import AppError, not_found
from cs_agent.db.business_tables import confirmations, operations, shipping_snapshots
from cs_agent.identity.policies import require_role
from cs_agent.privacy.projections import PrivateContact, support_contact


class ActionService:
    def propose(self, session, ctx, action, resource_ref, parameters, expected_version, now):
        require_role(ctx, Role.CUSTOMER)
        normalized = {}
        summary = {
            "action": action,
            "resource_ref": resource_ref,
            "expected_version": expected_version,
        }
        from cs_agent.actions.extensions import provider

        extension = provider(action)
        if extension:
            normalized, summary = extension.prepare_action(
                session, ctx, action, resource_ref, parameters, expected_version, now
            )
        elif action == "order.create":
            if (
                set(parameters) - {"quote_ref"}
                or parameters.get("quote_ref", resource_ref) != resource_ref
            ):
                raise AppError("invalid_input", 422, "报价引用不一致")
            row = quote_row(session, ctx, resource_ref)
            check_version(row, expected_version)
            validate_quote(session, ctx, row, now)
            normalized = {"quote_ref": resource_ref}
            summary.update(payable_minor=str(row["payable_minor"]), currency="CNY")
            summary["items"] = [
                {"name": line["product_name"], "qty": line["qty"]} for line in row["lines"]
            ]
            summary["shipping"] = support_contact(
                contact_from(shipping_input_row(session, ctx, row["shipping_input_id"]))
            )
        else:
            row = owned_order(session, ctx, resource_ref)
            check_version(row, expected_version)
            normalized = {"order_ref": resource_ref}
            summary["display_no"] = row["display_no"]
            if action == "order.address.change":
                if set(parameters) != {"shipping_input_ref"} or not isinstance(
                    parameters["shipping_input_ref"], str
                ):
                    raise AppError("invalid_input", 422, "请使用本人填写的改址资料")
                FulfillmentService.check_change(row, now)
                new_input = shipping_input_row(
                    session, ctx, parameters["shipping_input_ref"], "address_change"
                )
                old_contact = (
                    session.execute(
                        select(shipping_snapshots).where(
                            shipping_snapshots.c.snapshot_id == row["current_shipping_id"],
                            shipping_snapshots.c.order_id == row["order_id"],
                        )
                    )
                    .mappings()
                    .one()
                )
                summary["previous_shipping"] = support_contact(
                    PrivateContact(
                        old_contact["snapshot_id"],
                        **{
                            name: old_contact[name]
                            for name in (
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
                )
                summary["shipping"] = support_contact(contact_from(new_input))
                normalized.update(parameters)
                # 不在确认记录/通用操作响应中复制完整地址。
                summary["notice"] = "本单仅可成功修改一次收货地址，请核对个人页面填写的内容"
            else:
                if parameters:
                    raise AppError("invalid_input", 422, "该操作不接受额外参数")
                if action == "order.pay":
                    if (
                        row["trade_status"] != "awaiting_payment"
                        or row["payment_expires_at"] <= now
                    ):
                        raise AppError("state_conflict", 409, "订单已关闭、已付款或付款时间已过")
                    summary.update(payable_minor=str(row["payable_minor"]), currency="CNY")
                elif action == "order.receipt.confirm":
                    if row["trade_status"] != "paid" or row["fulfillment_status"] != "in_transit":
                        raise AppError("state_conflict", 409, "仅运输中订单可确认收货")
                elif action == "order.cancel":
                    if row["trade_status"] == "paid" and row["fulfillment_status"] == "unshipped":
                        summary["requires_agent_review"] = True
                        summary["notice"] = (
                            "确认后提交资格审核；审核及业务状态复核通过才取消并退余额"
                        )
                    elif row["trade_status"] != "awaiting_payment":
                        raise AppError("state_conflict", 409, "当前订单不可普通取消")
                else:
                    raise AppError("invalid_input", 422, "操作类型不可用")
        ref = new_id("confirm")
        expires = now + timedelta(minutes=5)
        session.execute(
            confirmations.insert().values(
                confirmation_id=ref,
                owner_id=ctx.subject_id,
                actor_role=ctx.role.value,
                auth_version=ctx.auth_version,
                action=action,
                resource_ref=resource_ref,
                parameters=normalized,
                parameters_digest=fingerprint(normalized),
                summary=summary,
                expected_version=expected_version,
                expires_at=expires,
                status="pending",
                **provenance(ctx, now),
            )
        )
        record(
            session,
            request_id=ctx.request_id,
            action="action.proposed",
            outcome="succeeded",
            ctx=ctx,
            resource_ref=ref,
            summary={"version": expected_version},
        )
        return {"confirmation_id": ref, "summary": summary, "expires_at": expires.isoformat()}

    def submit(self, session, ctx, confirmation_id, decision, key, now):
        require_role(ctx, Role.CUSTOMER)
        row = (
            session.execute(
                select(confirmations)
                .where(
                    confirmations.c.confirmation_id == confirmation_id,
                    confirmations.c.owner_id == ctx.subject_id,
                    confirmations.c.actor_role == ctx.role.value,
                    confirmations.c.store_id == ctx.store_id,
                    confirmations.c.data_batch_id == ctx.data_batch_id,
                    confirmations.c.batch_generation == ctx.batch_generation,
                )
                .with_for_update()
            )
            .mappings()
            .first()
        )
        if row is None:
            raise not_found()
        if row["auth_version"] != ctx.auth_version:
            raise AppError("state_conflict", 409, "授权已变化，请重新确认")
        if row["action"] == "order.cancel" and row["summary"].get("requires_agent_review"):
            from cs_agent.cancellation import CancellationService

            return CancellationService().submit(session, ctx, row, decision, key, now)
        params = {
            "confirmation_id": confirmation_id,
            "decision": decision,
            "parameters_digest": row["parameters_digest"],
            "expected_version": row["expected_version"],
        }
        original = session.execute(
            select(operations.c.operation_id).where(operations.c.confirmation_id == confirmation_id)
        ).scalar_one_or_none()

        def execute(op_id):
            from cs_agent.actions.origin import verify_origin

            verify_origin(session, confirmation_id, ctx)
            if row["status"] != "pending" or row["expires_at"] <= now:
                raise AppError("confirmation_expired", 409, "确认已失效，请重新核对操作")
            if decision == "cancel":
                return {
                    "business_ref": row["resource_ref"],
                    "decision": "cancel",
                    "business_effect": "none",
                }
            if decision != "confirm":
                raise AppError("invalid_input", 422, "确认选项不符合契约")
            action, parameters, version = row["action"], row["parameters"], row["expected_version"]
            from cs_agent.actions.extensions import provider

            extension = provider(action)
            if extension:
                return extension.apply_confirmed_action(session, ctx, row, op_id, now)
            if action == "order.create":
                return CheckoutService().create_order(session, ctx, parameters, version, op_id, now)
            if action == "order.pay":
                return CheckoutService().pay(session, ctx, row["resource_ref"], version, op_id, now)
            if action == "order.address.change":
                return FulfillmentService().change_address(
                    session, ctx, parameters, version, op_id, confirmation_id, now
                )
            if action == "order.receipt.confirm":
                return FulfillmentService().confirm_receipt(
                    session, ctx, row["resource_ref"], version, op_id, now
                )
            if action == "order.cancel":
                order = owned_order(session, ctx, row["resource_ref"], lock=True)
                check_version(order, version)
                if order["trade_status"] != "awaiting_payment":
                    raise AppError(
                        "state_conflict", 409, "当前订单不可取消；已付款取消须经资格审核"
                    )
                release_unpaid(session, ctx, order, now, "customer_cancelled", op_id)
                return {"business_ref": order["order_id"], "version": order["version"] + 1}
            raise AppError("invalid_input", 422, "操作类型不可用")

        result, status = execute_once(
            session,
            ctx,
            row["action"],
            key,
            params,
            now,
            execute,
            confirmation_id=confirmation_id,
            resource_ref=row["resource_ref"],
        )
        if not original:
            session.execute(
                update(confirmations)
                .where(confirmations.c.confirmation_id == confirmation_id)
                .values(
                    status="cancelled" if decision == "cancel" else "confirmed",
                    confirmed_at=now,
                    version=row["version"] + 1,
                    updated_at=now,
                )
            )
            record(
                session,
                request_id=ctx.request_id,
                action="action.submitted",
                outcome="succeeded" if status < 400 else "denied",
                ctx=ctx,
                resource_ref=confirmation_id,
                summary={"reason_code": result.get("error", {}).get("code", "confirmed")},
            )
        return result, status
