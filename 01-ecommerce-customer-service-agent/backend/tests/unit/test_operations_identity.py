from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from cs_agent.core.context import Role, TrustedContext
from cs_agent.core.errors import AppError
from cs_agent.identity.operations import capability_projection, enforce_projection

NOW = datetime(2026, 10, 23, tzinfo=UTC)
CTX = TrustedContext("staff", "store", Role.SHOP_ADMIN, "ses", 1, "batch", 1, "req")


def grant(**changes):
    values = dict(subject_id="staff", store_id="store", role="shop_admin",
                  capability="activity.publish", grant_status="active", valid_from=NOW,
                  valid_to=None, data_batch_id="batch", batch_generation=1)
    return SimpleNamespace(**(values | changes))


def test_shop_admin_has_no_implicit_capabilities():
    assert capability_projection(CTX, [], 1, NOW).capabilities == ()


@pytest.mark.parametrize("changes", [
    {"role": "support"}, {"store_id": "other"}, {"subject_id": "other"},
    {"grant_status": "revoked"}, {"valid_to": NOW},
    {"valid_from": NOW + timedelta(seconds=1)}, {"data_batch_id": "old"},
    {"batch_generation": 2}, {"capability": "unknown"},
])
def test_capability_grants_fail_closed(changes):
    assert not capability_projection(CTX, [grant(**changes)], 2, NOW).capabilities


def test_capability_requires_exact_versions_and_permission():
    projection = capability_projection(CTX, [grant()], 3, NOW)
    enforce_projection(projection, "activity.publish", 1, 3)
    for capability, auth, revision in [("cost.read", 1, 3), ("activity.publish", 2, 3),
                                       ("activity.publish", 1, 2)]:
        with pytest.raises(AppError):
            enforce_projection(projection, capability, auth, revision)
