from dataclasses import replace

import pytest

from cs_agent.core.context import Role, TrustedContext
from cs_agent.core.errors import AppError
from cs_agent.identity.policies import (
    ResourceScope,
    require_fulfillment_contact,
    require_owner,
    require_support,
)


def ctx(subject, role, store="store_sim"):
    return TrustedContext(subject, store, role, "session_sim", 1, "batch_sim", 1, "req_sim")


SCOPE = ResourceScope("store_sim", "customer_a", "staff_a", True, True)


@pytest.mark.parametrize(
    "subject,role,allowed",
    [
        ("staff_a", Role.SUPPORT, True),
        ("staff_b", Role.SUPPORT, False),
        ("boss", Role.SUPERVISOR, True),
        ("knowledge", Role.KNOWLEDGE_ADMIN, False),
        ("customer_a", Role.CUSTOMER, False),
        ("shop", Role.SHOP_ADMIN, False),
    ],
)
def test_support_scope_requires_current_role_and_facts(subject, role, allowed):
    if allowed:
        require_support(ctx(subject, role), SCOPE)
    else:
        with pytest.raises(AppError):
            require_support(ctx(subject, role), SCOPE)


def test_old_assignment_and_cross_store_never_grant_access():
    for scope in [replace(SCOPE, assignment_active=False), replace(SCOPE, store_id="other")]:
        with pytest.raises(AppError):
            require_support(ctx("staff_a", Role.SUPPORT), scope)
    with pytest.raises(AppError):
        require_support(ctx("boss", Role.SUPERVISOR, "other"), SCOPE)


def test_shop_admin_only_reads_fulfillment_related_scope():
    require_fulfillment_contact(ctx("shop", Role.SHOP_ADMIN), SCOPE)
    with pytest.raises(AppError):
        require_fulfillment_contact(
            ctx("shop", Role.SHOP_ADMIN), replace(SCOPE, fulfillment_required=False)
        )


def test_self_capability_does_not_transfer_to_staff_role():
    require_owner(ctx("customer_a", Role.CUSTOMER), SCOPE)
    with pytest.raises(AppError):
        require_owner(ctx("customer_b", Role.CUSTOMER), SCOPE)
    with pytest.raises(AppError):
        require_owner(ctx("customer_a", Role.SUPPORT), SCOPE)
