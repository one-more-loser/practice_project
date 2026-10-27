from dataclasses import dataclass

from cs_agent.core.context import Role, TrustedContext
from cs_agent.core.errors import AppError, not_found


@dataclass(frozen=True)
class ResourceScope:
    # 只接受业务仓储加载的事实；B0分派事实的测试替身不是生产工单实现。
    store_id: str
    owner_id: str
    assigned_staff_id: str | None = None
    assignment_active: bool = False
    fulfillment_required: bool = False


def require_owner(ctx: TrustedContext, scope: ResourceScope):
    if (
        ctx.role != Role.CUSTOMER
        or ctx.store_id != scope.store_id
        or ctx.subject_id != scope.owner_id
    ):
        raise not_found()


def require_support(ctx: TrustedContext, scope: ResourceScope):
    if ctx.store_id != scope.store_id:
        raise not_found()
    if ctx.role == Role.SUPERVISOR:
        return
    if (
        ctx.role == Role.SUPPORT
        and scope.assignment_active
        and ctx.subject_id == scope.assigned_staff_id
    ):
        return
    raise not_found()


def require_fulfillment_contact(ctx: TrustedContext, scope: ResourceScope):
    if (
        ctx.role != Role.SHOP_ADMIN
        or ctx.store_id != scope.store_id
        or not scope.fulfillment_required
    ):
        raise not_found()


def require_role(ctx: TrustedContext, role: Role):
    if ctx.role != role:
        raise AppError("forbidden", 403, "当前角色不允许此操作")
