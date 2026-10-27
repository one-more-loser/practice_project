"""固定test独占窗口运行；不由身份子模块自行启动。"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from threading import Event

import pytest
from sqlalchemy import func, select

from cs_agent.core.context import Role
from cs_agent.core.errors import AppError
from cs_agent.db.models import LoginSession
from cs_agent.db.operations_identity_tables import OperationsCapabilityAudit as Audit
from cs_agent.identity.operations import (
    grant_capability,
    operations_for,
    require_operations,
    revoke_capability,
)


def prepare(db, base, now):
    ctx = replace(base, subject_id="shop_admin", role=Role.SHOP_ADMIN,
                  session_ref="ses_ops_test")
    with db.transaction() as session:
        session.add(LoginSession(
            session_id=ctx.session_ref, subject_id=ctx.subject_id, token_digest="a" * 64,
            csrf_digest="b" * 64, active_role=ctx.role.value, auth_version=ctx.auth_version,
            status="active", expires_at=now + timedelta(hours=1), source_kind="derived",
            data_batch_id=ctx.data_batch_id, batch_generation=ctx.batch_generation,
        ))
    return ctx


def test_explicit_grant_revoke_and_expiry_revision(business):
    db, owner, base, now = business
    ctx = prepare(db, base, now)
    with db.transaction() as session:
        assert operations_for(session, ctx, now).capabilities == ()
        grant = grant_capability(session, subject_id=ctx.subject_id, store_id=ctx.store_id,
                                 role=ctx.role, capability="activity.publish", now=now,
                                 valid_to=now + timedelta(minutes=1), actor_ref="test:owner",
                                 reason="bounded synthetic grant")
        grant_id = grant.grant_id
        projection = require_operations(session, ctx, "activity.publish", now, lock=True)
        assert projection.capability_revision == 2
    with db.transaction() as session:
        with pytest.raises(AppError) as error:
            require_operations(session, ctx, "activity.publish", now, lock=True,
                               expected_capability_revision=1)
        assert error.value.code == "identity_version_conflict"
    with db.transaction() as session:
        after = operations_for(session, ctx, now + timedelta(minutes=1))
        assert after.capabilities == () and after.capability_revision == 3
        # repeated expiry/revoke does not produce a second transition audit
        revoke_capability(session, subject_id=ctx.subject_id, store_id=ctx.store_id,
                          grant_id=grant_id, now=now + timedelta(minutes=2),
                          actor_ref="test:owner", reason="replay")
        assert session.scalar(select(func.count()).select_from(Audit)) == 2


def test_revoked_login_is_rechecked_inside_publish_transaction(business):
    db, owner, base, now = business
    ctx = prepare(db, base, now)
    with db.transaction() as session:
        grant_capability(session, subject_id=ctx.subject_id, store_id=ctx.store_id,
                         role=ctx.role, capability="activity.publish", now=now,
                         actor_ref="test:owner", reason="explicit")
        current = session.get(LoginSession, ctx.session_ref)
        current.status, current.revoked_at = "revoked", now
    with db.transaction() as session, pytest.raises(AppError) as error:
        require_operations(session, ctx, "activity.publish", now, lock=True)
    assert error.value.status == 401


def test_publish_and_revocation_share_subject_transaction_lock(business):
    db, owner, base, now = business
    ctx = prepare(db, base, now)
    with db.transaction() as session:
        grant_id = grant_capability(
            session, subject_id=ctx.subject_id, store_id=ctx.store_id, role=ctx.role,
            capability="activity.publish", now=now, actor_ref="test:owner", reason="explicit",
        ).grant_id
    entered, completed = Event(), Event()

    def revoke():
        entered.set()
        with db.transaction() as session:
            revoke_capability(session, subject_id=ctx.subject_id, store_id=ctx.store_id,
                              grant_id=grant_id, now=now, actor_ref="test:owner", reason="revoke")
        completed.set()

    with ThreadPoolExecutor(max_workers=1) as pool:
        with db.transaction() as session:
            require_operations(session, ctx, "activity.publish", now, lock=True,
                               expected_auth_version=1, expected_capability_revision=2)
            pending = pool.submit(revoke)
            assert entered.wait(1)
            assert not completed.wait(0.1)
        pending.result(timeout=3)
    assert completed.is_set()
    with db.transaction() as session, pytest.raises(AppError) as error:
        require_operations(session, ctx, "activity.publish", now, lock=True)
    assert error.value.status == 403
