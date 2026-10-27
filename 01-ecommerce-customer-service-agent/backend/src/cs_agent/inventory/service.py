from sqlalchemy import update

from cs_agent.actions.repository import execute_once, new_id, provenance
from cs_agent.audit.service import record
from cs_agent.catalog.service import sku_row
from cs_agent.core.context import Role
from cs_agent.core.errors import AppError
from cs_agent.db.business_tables import inventory, stock_movements
from cs_agent.identity.policies import require_role
from cs_agent.wallet.service import event


def move_stock(
    session, ctx, row, stock_delta, reserved_delta, kind, business_ref, operation_id, now, reason
):
    stock, reserved = row["stock_qty"] + stock_delta, row["reserved_qty"] + reserved_delta
    if not 0 <= reserved <= stock <= 2**31 - 1:
        raise AppError("insufficient_stock", 409, "库存不足或不能低于已预留数量")
    version = row["inventory_version"] + 1
    session.execute(
        update(inventory)
        .where(inventory.c.sku_id == row["sku_id"])
        .values(stock_qty=stock, reserved_qty=reserved, version=version, updated_at=now)
    )
    movement_id = new_id("movement")
    session.execute(
        stock_movements.insert().values(
            movement_id=movement_id,
            sku_id=row["sku_id"],
            operation_id=operation_id,
            business_ref=business_ref,
            kind=kind,
            stock_delta=stock_delta,
            reserved_delta=reserved_delta,
            stock_before=row["stock_qty"],
            stock_after=stock,
            reserved_before=row["reserved_qty"],
            reserved_after=reserved,
            reason=reason,
            occurred_at=now,
            recorded_at=now,
            **provenance(ctx, now),
        )
    )
    return {
        "business_ref": movement_id,
        "sku_id": row["sku_id"],
        "stock_qty": stock,
        "reserved_qty": reserved,
        "available_qty": stock - reserved,
        "version": version,
    }


class InventoryService:
    def adjust(
        self, session, ctx, sku_id, delta, expected_version, key, now, reason="manual_adjustment"
    ):
        require_role(ctx, Role.SHOP_ADMIN)
        if type(delta) is not int or delta == 0 or abs(delta) > 2**31 - 1:
            raise AppError("invalid_input", 422, "请输入合法的非零库存变更数")

        def execute(op_id):
            row = sku_row(session, sku_id, ctx.store_id, lock=True)
            if row["inventory_version"] != expected_version:
                raise AppError("version_conflict", 409, "库存已更新，请刷新后重试")
            result = move_stock(
                session, ctx, row, delta, 0, "adjustment", op_id, op_id, now, reason
            )
            event(
                session,
                ctx,
                "inventory.adjusted",
                sku_id,
                result["version"],
                op_id,
                {
                    "sku_id": sku_id,
                    "stock_qty": result["stock_qty"],
                    "reserved_qty": result["reserved_qty"],
                },
                now,
            )
            record(
                session,
                request_id=ctx.request_id,
                action="inventory.adjusted",
                outcome="succeeded",
                ctx=ctx,
                resource_ref=sku_id,
                summary={"version": result["version"]},
            )
            return result

        return execute_once(
            session,
            ctx,
            "inventory.adjust",
            key,
            {
                "sku_id": sku_id,
                "delta": delta,
                "expected_version": expected_version,
                "reason": reason,
            },
            now,
            execute,
            resource_ref=sku_id,
        )
