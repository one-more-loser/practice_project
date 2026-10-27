from datetime import UTC, datetime, timedelta

import pytest

from cs_agent.aftersales.rules import allocate_units, within_window
from cs_agent.core.errors import AppError


def test_exact_168_hour_boundary_and_frozen_unit_tail():
    now = datetime(2026, 10, 23, tzinfo=UTC)
    assert within_window(now, now + timedelta(hours=168), {"ordinary_return_hours": 168})
    assert within_window(
        now, now + timedelta(hours=168) - timedelta(seconds=1), {"ordinary_return_hours": 168}
    )
    assert not within_window(
        now, now + timedelta(hours=168, seconds=1), {"ordinary_return_hours": 168}
    )
    line = {
        "ordered_qty": 3,
        "unit_allocations": {"quantity": 3, "base_minor": "3333", "tail_minor": "3334"},
        "allocation_revision": "ALLOC-LAST-UNIT-v1",
        "paid_goods_minor": 10000,
        "reserved_refund_qty": 0,
        "refunded_qty": 0,
        "reserved_refund_minor": 0,
        "refunded_goods_minor": 0,
    }
    assert allocate_units(line, 1, set()) == ([0], 3333)
    line.update(refunded_qty=1, refunded_goods_minor=3333)
    assert allocate_units(line, 2, {0}) == ([1, 2], 6667)
    with pytest.raises(AppError):
        allocate_units(line, 3, {0})
    with pytest.raises(AppError):
        allocate_units(line, True, {0})
