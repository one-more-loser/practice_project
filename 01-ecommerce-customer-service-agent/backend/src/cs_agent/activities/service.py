"""发布：持久slot → 事务外grant → 短事务本地复验；不会将超时当作无效果。"""

from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert

from cs_agent.actions.repository import execute_once, new_id, provenance
from cs_agent.activities.contracts import (
    VERSION,
    instant,
    manifest_digest,
    reject,
    validate_grant,
    validate_manifest,
)
from cs_agent.activities.repository import receipt, refresh_phase, scope
from cs_agent.catalog.service import sku_row
from cs_agent.core.context import Role, TrustedContext
from cs_agent.core.errors import not_found
from cs_agent.db.activity_tables import activities, lines, previews, slots
from cs_agent.db.business_tables import products, skus
from cs_agent.db.models import LoginSession
from cs_agent.identity.operations import require_operations


def validate_local(session, ctx, manifest, now, *, lock=False, exclude=None):
    validate_manifest(manifest)
    if manifest["store_id"] != ctx.store_id:
        raise not_found()
    if instant(manifest["end_at"]) <= now:
        reject("activity_window_expired", 409)
    if lock:
        # 与常规商品改价、交易保持 product → SKU 的顺序，slot不被交易路径访问。
        ids = session.scalars(
            select(skus.c.product_id)
            .where(
                skus.c.store_id == ctx.store_id,
                skus.c.sku_id.in_([r["sku_id"] for r in manifest["lines"]]),
            )
            .distinct()
        ).all()
        for pid in sorted(ids):
            session.execute(
                select(products.c.product_id).where(products.c.product_id == pid).with_for_update()
            ).all()
    for line in manifest["lines"]:
        sku = sku_row(session, line["sku_id"], ctx.store_id, lock=lock)
        if (
            sku["data_batch_id"] != ctx.data_batch_id
            or sku["batch_generation"] != ctx.batch_generation
            or sku["status"] != "active"
            or sku["product_status"] != "active"
            or sku["price_minor"] != int(line["base_price_minor"])
            or sku["price_version"] != line["price_version"]
        ):
            reject("activity_price_version_conflict", 409)
        if sku["stock_qty"] - sku["reserved_qty"] < line["quantity_cap"]:
            reject("activity_inventory_insufficient", 409)
        stmt = (
            select(activities.c.activity_id)
            .join(lines, lines.c.activity_id == activities.c.activity_id)
            .where(
                *scope(activities, ctx),
                lines.c.sku_id == line["sku_id"],
                activities.c.phase != "ended",
                activities.c.start_at < instant(manifest["end_at"]),
                activities.c.end_at > instant(manifest["start_at"]),
            )
        )
        if exclude:
            stmt = stmt.where(activities.c.activity_id != exclude)
        if session.execute(stmt).first():
            reject("activity_overlap", 409)


def preview(session, ctx, body, now):
    auth = require_operations(session, ctx, "activity.publish", now)
    m = body["manifest"]
    validate_manifest(m)
    if (
        body["contract_version"] != VERSION
        or manifest_digest(m) != body["manifest_digest"]
        or any(body[k] != m[k] for k in ("store_id", "plan_id", "plan_version"))
    ):
        reject()
    validate_local(session, ctx, m, now)
    ref = new_id("apr")
    expires = now + timedelta(minutes=5)
    session.execute(
        previews.insert().values(
            preview_ref=ref,
            actor_ref=ctx.subject_id,
            auth_version=ctx.auth_version,
            capability_revision=auth.capability_revision,
            plan_id=m["plan_id"],
            plan_version=m["plan_version"],
            manifest_digest=body["manifest_digest"],
            manifest=m,
            expires_at=expires,
            **provenance(ctx, now),
        )
    )
    return dict(
        contract_version=VERSION,
        store_id=ctx.store_id,
        plan_id=m["plan_id"],
        plan_version=m["plan_version"],
        manifest_digest=body["manifest_digest"],
        preview_ref=ref,
        status="passed",
        expires_at=expires.isoformat(),
        evidence_kind="integration",
    )


def find_slot(session, ctx, body, *, lock=False):
    stmt = select(slots).where(
        *scope(slots, ctx),
        slots.c.plan_id == body["plan_id"],
        slots.c.plan_version == body["plan_version"],
        slots.c.approval_ref == body["approval_ref"],
    )
    row = session.execute(stmt.with_for_update() if lock else stmt).mappings().first()
    if row and (
        row["manifest_digest"] != body["manifest_digest"]
        or row["preview_ref"] != body["preview_ref"]
        or row["actor_ref"] != ctx.subject_id
    ):
        reject("activity_slot_binding_conflict", 409)
    return row


def prepare_slot(session, ctx, body, revision, now):
    auth = require_operations(session, ctx, "activity.publish", now, lock=True)
    existing = find_slot(session, ctx, body, lock=True)
    if existing:
        return dict(existing)
    if not isinstance(revision, dict):
        reject("activity_remote_contract", 503)
    m = revision.get("manifest")
    validate_manifest(m)
    if (
        revision.get("contract_version") != VERSION
        or revision.get("status") != "confirmed"
        or any(
            revision.get(k) != body[k]
            for k in ("plan_id", "plan_version", "approval_ref", "manifest_digest")
        )
        or revision.get("store_id") != ctx.store_id
        or manifest_digest(m) != body["manifest_digest"]
        or m["plan_id"] != body["plan_id"]
        or m["plan_version"] != body["plan_version"]
    ):
        reject("activity_revision_not_confirmed", 409)
    p = (
        session.execute(
            select(previews).where(
                *scope(previews, ctx), previews.c.preview_ref == body["preview_ref"]
            )
        )
        .mappings()
        .first()
    )
    if (
        not p
        or p["expires_at"] <= now
        or p["actor_ref"] != ctx.subject_id
        or p["auth_version"] != ctx.auth_version
        or p["capability_revision"] != auth.capability_revision
        or p["manifest_digest"] != body["manifest_digest"]
        or p["manifest"] != m
    ):
        reject("activity_preview_expired", 409)
    values = dict(
        intent_id=new_id("intent"),
        plan_id=body["plan_id"],
        plan_version=body["plan_version"],
        approval_ref=body["approval_ref"],
        manifest_digest=body["manifest_digest"],
        manifest=m,
        actor_ref=ctx.subject_id,
        actor_role=ctx.role.value,
        session_ref=ctx.session_ref,
        auth_version=ctx.auth_version,
        capability_revision=auth.capability_revision,
        preview_ref=body["preview_ref"],
        status="pending",
        grant_expires_at=p["expires_at"],
        **provenance(ctx, now),
    )
    # 同审批跨客户端/换键只创建一个slot，等待唯一约束后读取同一事实。
    session.execute(insert(slots).values(**values).on_conflict_do_nothing())
    found = find_slot(session, ctx, body, lock=True)
    if not found:
        reject("activity_approval_reused", 409)
    return dict(found)


def grant_request(slot):
    return {
        k: slot[k]
        for k in (
            "store_id",
            "plan_id",
            "approval_ref",
            "intent_id",
            "manifest_digest",
            "actor_ref",
            "auth_version",
            "capability_revision",
            "preview_ref",
        )
    }


def remember_grant(session, ctx, intent_id, grant, now):
    slot = (
        session.execute(
            select(slots)
            .where(*scope(slots, ctx), slots.c.intent_id == intent_id)
            .with_for_update()
        )
        .mappings()
        .one()
    )
    if slot["status"] != "pending":
        return
    validate_grant(grant, grant_request(slot), slot["manifest"], now)
    if slot["grant_ref"] and slot["grant_ref"] != grant["grant_ref"]:
        reject("activity_grant_conflict", 409)
    session.execute(
        update(slots)
        .where(slots.c.intent_id == intent_id)
        .values(
            grant_ref=grant["grant_ref"],
            grant_expires_at=min(slot["grant_expires_at"], instant(grant["expires_at"])),
            updated_at=now,
        )
    )


def commit_grant(session, ctx, intent_id, grant, now, *, clock=None):
    # session行 → subject授权锁 → slot → product → SKU。取消使用相同前缀锁序。
    prior = (
        session.execute(select(slots).where(*scope(slots, ctx), slots.c.intent_id == intent_id))
        .mappings()
        .first()
    )
    if not prior:
        raise not_found()
    require_operations(
        session,
        ctx,
        "activity.publish",
        now,
        lock=True,
        expected_auth_version=prior["auth_version"],
        expected_capability_revision=prior["capability_revision"],
    )
    slot = (
        session.execute(
            select(slots)
            .where(*scope(slots, ctx), slots.c.intent_id == intent_id)
            .with_for_update()
        )
        .mappings()
        .one()
    )
    now = clock.now() if clock else now
    if slot["status"] != "pending":
        return receipt(session, slot, now)
    if slot["actor_ref"] != ctx.subject_id:
        reject("activity_actor_conflict", 403)
    validate_grant(grant, grant_request(slot), slot["manifest"], now)
    if slot["grant_expires_at"] and now >= slot["grant_expires_at"]:
        reject("activity_grant_expired", 409)
    if slot["grant_ref"] and slot["grant_ref"] != grant["grant_ref"]:
        reject("activity_grant_conflict", 409)
    validate_local(session, ctx, slot["manifest"], now, lock=True)
    now = clock.now() if clock else now
    validate_grant(grant, grant_request(slot), slot["manifest"], now)
    if instant(slot["manifest"]["end_at"]) <= now:
        reject("activity_window_expired", 409)
    if now >= slot["grant_expires_at"]:
        reject("activity_grant_expired", 409)
    require_operations(
        session,
        ctx,
        "activity.publish",
        now,
        lock=True,
        expected_auth_version=slot["auth_version"],
        expected_capability_revision=slot["capability_revision"],
    )
    aid = new_id("act")
    m = slot["manifest"]
    phase = "active" if instant(m["start_at"]) <= now else "scheduled"
    session.execute(
        activities.insert().values(
            activity_id=aid,
            intent_id=intent_id,
            manifest=m,
            phase=phase,
            revision=1,
            start_at=instant(m["start_at"]),
            end_at=instant(m["end_at"]),
            variable_cap_minor=int(m["variable_budget_cap_minor"]),
            reserved_discount_minor=0,
            consumed_discount_minor=0,
            actual_revision=0,
            snapshot_id=new_id("aas"),
            watermark=now,
            **dict(provenance(ctx, now), source_kind=m["source_kind"]),
        )
    )
    for line in m["lines"]:
        session.execute(
            lines.insert().values(
                line_id=new_id("al"),
                activity_id=aid,
                sku_id=line["sku_id"],
                base_price_minor=int(line["base_price_minor"]),
                effective_price_minor=int(line["activity_price_minor"]),
                price_version=line["price_version"],
                quantity_cap=line["quantity_cap"],
                reserved_qty=0,
                consumed_qty=0,
                **provenance(ctx, now),
            )
        )
    session.execute(
        update(slots)
        .where(slots.c.intent_id == intent_id)
        .values(
            status="committed",
            activity_id=aid,
            grant_ref=grant["grant_ref"],
            grant_expires_at=instant(grant["expires_at"]),
            updated_at=now,
        )
    )
    changed = dict(slot, status="committed", activity_id=aid)
    result = receipt(
        session, changed, now, dict(activity_id=aid, revision=1, phase=phase), enqueue_event=True
    )
    from cs_agent.activities.pricing import record_actuals

    record_actuals(session, ctx, aid, now)
    return result


def project(row, *, can_read_cost=False):
    """公开读取DTO；不改原Manifest，也不用于签名、grant或内部回执。"""
    m = row["manifest"]
    result = dict(
        activity_id=row["activity_id"],
        intent_id=row["intent_id"],
        plan_id=m["plan_id"],
        plan_version=m["plan_version"],
        phase=row["phase"],
        revision=row["revision"],
        start_at=m["start_at"],
        end_at=m["end_at"],
    )
    # 白名单使未来新增的嵌套预算字段也不能进入metrics-only响应。
    # 行级价格×数量上限、让利核销/预占同样不向无cost.read主体披露。
    if can_read_cost:
        result.update(
            manifest=m,
            reserved_discount_minor=str(row["reserved_discount_minor"]),
            consumed_discount_minor=str(row["consumed_discount_minor"]),
        )
    return result


def list_activities(session, ctx, now, activity_id=None):
    authorization = require_operations(session, ctx, "metrics.read", now)
    can_read_cost = "cost.read" in authorization.capabilities
    stmt = (
        select(activities)
        .where(*scope(activities, ctx))
        .order_by(activities.c.activity_id)
        .limit(200)
    )
    if activity_id:
        stmt = stmt.where(activities.c.activity_id == activity_id)
    rows = session.execute(stmt.with_for_update()).mappings().all()
    items = [project(refresh_phase(session, r, now), can_read_cost=can_read_cost) for r in rows]
    if activity_id:
        if not items:
            raise not_found()
        item = items[0]
        if not can_read_cost:
            return item
        item["lines"] = [
            dict(
                sku_id=r["sku_id"],
                base_price_minor=str(r["base_price_minor"]),
                effective_price_minor=str(r["effective_price_minor"]),
                quantity_cap=r["quantity_cap"],
                reserved_qty=r["reserved_qty"],
                consumed_qty=r["consumed_qty"],
            )
            for r in session.execute(
                select(lines).where(lines.c.activity_id == activity_id).order_by(lines.c.sku_id)
            ).mappings()
        ]
        return item
    return dict(items=items, total=len(items))


def manage(session, ctx, activity_id, action, body, key, now):
    require_operations(session, ctx, "activity.manage", now, lock=True)

    def execute(op_id):
        nonlocal now
        row = (
            session.execute(
                select(activities)
                .where(*scope(activities, ctx), activities.c.activity_id == activity_id)
                .with_for_update()
            )
            .mappings()
            .first()
        )
        if not row:
            raise not_found()
        clock = session.info.get("clock")
        now = clock.now() if clock else now
        require_operations(session, ctx, "activity.manage", now, lock=True)
        row = refresh_phase(session, row, now)
        if row["revision"] != body["expected_revision"]:
            reject("activity_revision_conflict", 409)
        if (
            row["phase"] == "ended"
            or (action == "pause" and row["phase"] == "paused")
            or (action == "resume" and row["phase"] != "paused")
        ):
            reject("activity_phase_conflict", 409)
        target = (
            "paused"
            if action == "pause"
            else (
                "ended"
                if action == "end"
                else ("active" if now >= row["start_at"] else "scheduled")
            )
        )
        changed = dict(row, phase=target, revision=row["revision"] + 1)
        session.execute(
            update(activities)
            .where(activities.c.activity_id == activity_id)
            .values(phase=target, revision=changed["revision"], updated_at=now)
        )
        slot = (
            session.execute(select(slots).where(slots.c.intent_id == row["intent_id"]))
            .mappings()
            .one()
        )
        result = receipt(session, slot, now, changed, enqueue_event=True)
        if target == "ended":
            from cs_agent.activities.pricing import record_actuals

            record_actuals(session, ctx, activity_id, now)
        return {"receipt": result}

    result, status = execute_once(
        session,
        ctx,
        "activity." + action,
        key,
        dict(activity_id=activity_id, **body),
        now,
        execute,
        resource_ref=activity_id,
    )
    return result.get("receipt", result), status


def internal_result(session, store_id, intent_id, now, action=None, body=None, *, clock=None):
    stmt = select(slots).where(slots.c.store_id == store_id, slots.c.intent_id == intent_id)
    # cancel重新核验原会话和最新主体能力，然后再取得slot锁；发布与之同序。
    initial = session.execute(stmt).mappings().first()
    if not initial:
        return dict(status="unknown")
    if action == "cancel-request":
        if any(
            body.get(k) != initial[k] for k in ("actor_ref", "auth_version", "capability_revision")
        ) or (initial["grant_ref"] and body["grant_ref"] != initial["grant_ref"]):
            reject("activity_cancel_binding", 409)
        current = session.scalar(
            select(LoginSession)
            .where(
                LoginSession.subject_id == initial["actor_ref"],
                LoginSession.status == "active",
                LoginSession.auth_version == initial["auth_version"],
                LoginSession.active_role == initial["actor_role"],
                LoginSession.expires_at > now,
                LoginSession.data_batch_id == initial["data_batch_id"],
                LoginSession.batch_generation == initial["batch_generation"],
            )
            .order_by(LoginSession.created_at.desc())
            .limit(1)
        )
        if current is None:
            reject("unauthenticated", 401)
        ctx = TrustedContext(
            subject_id=initial["actor_ref"],
            store_id=store_id,
            role=Role(current.active_role),
            session_ref=current.session_id,
            auth_version=initial["auth_version"],
            data_batch_id=initial["data_batch_id"],
            batch_generation=initial["batch_generation"],
            request_id="internal:cancel",
        )
        require_operations(
            session,
            ctx,
            "plan.confirm",
            now,
            lock=True,
            expected_auth_version=body["auth_version"],
            expected_capability_revision=body["capability_revision"],
        )
    slot = session.execute(stmt.with_for_update()).mappings().one()
    now = clock.now() if clock else now
    if action == "cancel-request":
        require_operations(
            session,
            ctx,
            "plan.confirm",
            now,
            lock=True,
            expected_auth_version=body["auth_version"],
            expected_capability_revision=body["capability_revision"],
        )
    if slot["status"] != "pending":
        return receipt(session, slot, now)
    if action == "cancel-request" or (
        action == "finalize-expired"
        and slot["grant_expires_at"]
        and now >= slot["grant_expires_at"]
    ):
        session.execute(
            update(slots)
            .where(slots.c.intent_id == intent_id)
            .values(status="closed_no_effect", updated_at=now)
        )
        return receipt(session, dict(slot, status="closed_no_effect"), now, enqueue_event=True)
    return dict(status="unknown")
