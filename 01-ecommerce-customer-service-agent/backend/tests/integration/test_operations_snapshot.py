from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import pytest
from sqlalchemy import func, select

from cs_agent.core.errors import AppError
from cs_agent.operations_snapshot.service import produce_snapshot
from cs_agent.operations_snapshot.tables import snapshot_states


def test_snapshot_revision_persists_and_never_reverses_clock(business):
    db, owner, ctx, now = business
    with db.transaction() as session:
        first = produce_snapshot(session, ctx.store_id, now)
    with db.transaction() as session:
        replay = produce_snapshot(session, ctx.store_id, now)
        assert replay == first
    with db.transaction() as session:
        refreshed = produce_snapshot(session, ctx.store_id, now + timedelta(seconds=1))
        assert refreshed["snapshot_id"] == first["snapshot_id"]
        assert refreshed["revision"] == first["revision"] + 1
        assert refreshed["source_metadata"]["source_revision"] == first["source_metadata"]["source_revision"]
        assert session.scalar(select(func.count()).select_from(snapshot_states)) == 1
    with db.transaction() as session, pytest.raises(AppError) as error:
        produce_snapshot(session, ctx.store_id, now)
    assert error.value.code == "snapshot_clock_conflict"


def test_concurrent_same_observation_is_one_persistent_revision(business):
    db, owner, ctx, now = business
    barrier = Barrier(2)

    def observe():
        with db.transaction() as session:
            barrier.wait(timeout=3)
            return produce_snapshot(session, ctx.store_id, now)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = list(pool.map(lambda _: observe(), range(2)))
    assert first == second
    assert first["revision"] == 1
    with db.transaction() as session:
        assert session.scalar(select(func.count()).select_from(snapshot_states)) == 1
