"""显式启用的02专用真实PG验收；不重置、删除或借用01数据库。"""
import os
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import select
from test_domain import NOW, draft, snapshot

from ops_agent import db
from ops_agent.auth import OWNER, Principal
from ops_agent.bridge import Bridge
from ops_agent.service import Operations

pytestmark = pytest.mark.skipif(os.environ.get("OPS_PG_TEST") != "1", reason="需父会话供给02专用PG及身份文件")


def test_postgres_advisory_lock_and_idempotency():
    database = db.Database(os.environ["OPS_DATABASE_URL"], "integration")
    database.verify_schema()
    assert database.identity_record["database"].startswith("ops02_")
    assert database.identity_record["pool_size"] == 5
    suffix = uuid.uuid4().hex
    principal = Principal("pg_test_" + suffix, "pg_test_store_" + suffix, OWNER, "csrf", mode="test")
    bridge = Bridge(database, lambda: NOW)
    operations = Operations(database, lambda: NOW, "demo", bridge)
    source = snapshot()
    source["store_id"] = principal.store_id
    source["snapshot_id"] += suffix
    spec = draft()
    spec["snapshot_id"] = source["snapshot_id"]
    try:
        with ThreadPoolExecutor(max_workers=4) as workers:
            results = list(workers.map(lambda _: operations.import_snapshot(principal, source), range(8)))
        assert sum(created for _, created in results) == 1
        with ThreadPoolExecutor(max_workers=4) as workers:
            plans = list(workers.map(lambda _: operations.save_plan(principal, spec, "same-plan"), range(8)))
        assert len({plan["plan_id"] for plan in plans}) == 1
        with database.transaction() as connection:
            assert len(connection.execute(select(db.plans.c.plan_id).where(db.plans.c.store_id == principal.store_id)).all()) == 1
        # 测试数据保留为本run证据，不DELETE、不修改别的store。
    finally:
        bridge.close()
        database.close()


def test_postgres_single_approval_concurrent_grants():
    from datetime import timedelta

    from ops_agent.domain import evaluate_plan
    from ops_agent.errors import AppError
    from ops_agent.service import digest

    database = db.Database(os.environ["OPS_DATABASE_URL"], "integration")
    database.verify_schema()
    suffix = uuid.uuid4().hex
    store, plan_id, approval_ref = "pg_grant_" + suffix, "pg_plan_" + suffix, "pg_approval_" + suffix
    bridge = Bridge(database, lambda: NOW)
    source, spec = snapshot(), draft()
    source.update(store_id=store, snapshot_id="pg_source_" + suffix)
    spec["snapshot_id"] = source["snapshot_id"]
    plan = dict(plan_id=plan_id, store_id=store, version=1, draft=spec, snapshot=source, status="confirmed")
    fingerprint = digest(bridge.manifest(plan))
    approval = dict(actor_ref="pg_manager", auth_version=1, capability_revision=2, mode="integration",
        manifest_digest=fingerprint, source_kind="synthetic", evaluation_id="pg_eval_" + suffix,
        evidence_kind="integration", confirmed_at=NOW.isoformat(), preview=dict(
            contract_version="XOPS-ACTIVITY-v0.1", store_id=store, plan_id=plan_id, plan_version=1,
            manifest_digest=fingerprint, preview_ref="pg_preview_" + suffix, status="passed",
            evidence_kind="integration", expires_at=(NOW + timedelta(minutes=5)).isoformat()))
    # 这是受控PG锁测试夹具，integration标记仅供覆盖门禁，不作为真实01身份/预检证据。
    with database.transaction("store:" + store) as connection:
        connection.execute(db.snapshots.insert().values(store_id=store, snapshot_id=source["snapshot_id"],
            revision=1, digest=digest(source), body=source, created_at=NOW.isoformat()))
        connection.execute(db.plans.insert().values(plan_id=plan_id, store_id=store, version=1,
            status="confirmed", created_at=NOW.isoformat(), updated_at=NOW.isoformat()))
        connection.execute(db.revisions.insert().values(plan_id=plan_id, store_id=store, version=1,
            body=spec, digest=digest(spec), created_at=NOW.isoformat()))
        connection.execute(db.evaluations.insert().values(evaluation_id=approval["evaluation_id"],
            plan_id=plan_id, store_id=store, version=1, body=evaluate_plan(spec, source, NOW), created_at=NOW.isoformat()))
        connection.execute(db.approvals.insert().values(approval_ref=approval_ref, plan_id=plan_id,
            store_id=store, version=1, body=approval, created_at=NOW.isoformat()))
    payload = dict(approval_ref=approval_ref, intent_id="pg_intent_" + suffix,
        manifest_digest=fingerprint, actor_ref="pg_manager", auth_version=1, capability_revision=2,
        preview_ref=approval["preview"]["preview_ref"])
    try:
        with ThreadPoolExecutor(max_workers=4) as workers:
            results = list(workers.map(lambda _: bridge.grant(store, plan_id, payload), range(8)))
        assert len({result["grant_ref"] for result in results}) == 1
        with pytest.raises(AppError):
            bridge.grant(store, plan_id, dict(payload, intent_id="pg_other_" + suffix))
        with database.transaction() as connection:
            assert len(connection.execute(select(db.grants.c.grant_ref).where(db.grants.c.store_id == store)).all()) == 1
    finally:
        bridge.close()
        database.close()
