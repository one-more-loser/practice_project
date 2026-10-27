"""经营应用服务；身份/指标/测算/不可变版本始终在后端执行。"""

import csv
import hashlib
import io
import json
import uuid
from copy import deepcopy
from datetime import datetime, timedelta

from sqlalchemy import select

from ops_agent import db
from ops_agent.domain import calculate_metrics, evaluate_plan, validate_snapshot
from ops_agent.errors import AppError


def uid(prefix):
    return prefix + "_" + uuid.uuid4().hex


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode()).hexdigest()


def require_version(value):
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 9223372036854775807:
        raise AppError("invalid_input", "expected_version须为正整数", 422)
    return value


def parse_time(value):
    try:
        result = datetime.fromisoformat(value)
        if result.tzinfo is None:
            raise ValueError()
        return result
    except (ValueError, TypeError) as exc:
        raise AppError("invalid_input", "查询时间须为带时区ISO时间", 422) from exc


class Operations:
    def __init__(self, database, clock, mode, bridge):
        self.database, self.clock, self.mode, self.bridge = database, clock, mode, bridge

    def _audit(self, c, principal, action, resource):
        c.execute(db.audit.insert().values(audit_id=uid("audit"), store_id=principal.store_id,
            actor_ref=principal.actor_ref, action=action, resource_ref=resource,
            created_at=self.clock().isoformat()))

    def _idempotent(self, c, p, action, key, body, operation):
        if not isinstance(key, str) or not 1 <= len(key) <= 100:
            raise AppError("invalid_input", "此操作须提供1～100字符Idempotency-Key", 422)
        scope = digest([p.store_id, p.actor_ref, action, key])
        fingerprint = digest(body)
        previous = c.execute(select(db.operations).where(db.operations.c.scope == scope)).mappings().first()
        if previous:
            if previous["digest"] != fingerprint:
                raise AppError("idempotency_conflict", "此幂等键已用于不同请求")
            return previous["result"]
        result = operation()
        c.execute(db.operations.insert().values(scope=scope, digest=fingerprint, result=result))
        return result

    def _snapshot(self, c, store, identity=None):
        query = select(db.snapshots).where(db.snapshots.c.store_id == store)
        if identity:
            query = query.where(db.snapshots.c.snapshot_id == identity)
            query = query.order_by(db.snapshots.c.revision.desc())
        else:
            newer = db.snapshots.alias("newer")
            query = query.where(~select(newer.c.revision).where(
                newer.c.store_id == db.snapshots.c.store_id,
                newer.c.snapshot_id == db.snapshots.c.snapshot_id,
                newer.c.revision > db.snapshots.c.revision).exists())
            query = query.order_by(db.snapshots.c.created_at.desc(), db.snapshots.c.revision.desc())
        result = c.execute(query).mappings().first()
        if result is None:
            raise AppError("snapshot_not_ready", "店铺尚无可用经营快照，请导入获准数据", 409)
        return result["body"]

    def _bound_snapshot(self, c, p, draft):
        snapshot = self._snapshot(c, p.store_id, draft.get("snapshot_id"))
        if snapshot["revision"] != draft.get("expected_snapshot_revision"):
            raise AppError("snapshot_revision_conflict", "快照已修订，请基于新修订保存并重算")
        return snapshot

    def import_snapshot(self, p, body):
        p.require("data.import", "cost.read")
        snapshot = validate_snapshot(body)
        if snapshot["store_id"] != p.store_id:
            raise AppError("forbidden", "只能导入当前授权店铺数据", 403)
        if parse_time(snapshot["as_of"]) > self.clock():
            raise AppError("invalid_snapshot", "快照水位不可来自未来", 422)
        if len(snapshot["orders"]) > 20000 or len(snapshot["skus"]) > 2000:
            raise AppError("import_limit", "单快照上限2000个SKU、20000订单，请缩小数据窗", 422)
        with self.database.transaction("store:" + p.store_id) as c:
            existing = c.execute(select(db.snapshots).where(
                db.snapshots.c.store_id == p.store_id,
                db.snapshots.c.snapshot_id == snapshot["snapshot_id"],
                db.snapshots.c.revision == snapshot["revision"])).mappings().first()
            if existing:
                if existing["digest"] != digest(snapshot):
                    raise AppError("revision_conflict", "同一快照修订内容不同，须使用新修订")
                return snapshot, False
            for parent in snapshot.get("provenance", {}).get("parents", []):
                known = c.execute(select(db.snapshots.c.digest).where(
                    db.snapshots.c.store_id == p.store_id, db.snapshots.c.snapshot_id == parent["snapshot_id"],
                    db.snapshots.c.revision == parent["revision"])).scalar_one_or_none()
                if known != parent["digest"]:
                    raise AppError("lineage_unavailable", "父快照未在本店归档或摘要不符，不能回放", 422)
            c.execute(db.snapshots.insert().values(store_id=p.store_id,
                snapshot_id=snapshot["snapshot_id"], revision=snapshot["revision"],
                digest=digest(snapshot), body=snapshot, created_at=self.clock().isoformat()))
            self._audit(c, p, "snapshot.import", snapshot["snapshot_id"])
        return snapshot, True

    def list_snapshots(self, p):
        p.require("metrics.read")
        with self.database.transaction() as c:
            rows = c.execute(select(db.snapshots).where(db.snapshots.c.store_id == p.store_id)
                .order_by(db.snapshots.c.created_at.desc(), db.snapshots.c.revision.desc())).mappings().all()
        # 每个快照身份只投影最高修订，不把多次导入累加为事实。
        heads = {}
        for row in rows:
            identity = row["snapshot_id"]
            if identity not in heads or row["revision"] > heads[identity]["revision"]:
                heads[identity] = row["body"]
        values = list(heads.values())
        if "cost.read" not in p.capabilities:
            values = [self._public_snapshot(s) for s in values]
        return {"items": values}

    @staticmethod
    def _public_snapshot(snapshot):
        value = deepcopy(snapshot)
        value.pop("source_metadata", None)
        value.pop("provenance", None)
        for sku in value["skus"]:
            sku.pop("cost_minor", None)
            sku.pop("variable_fee_minor", None)
        return value

    def dashboard(self, p, snapshot_id=None, start_at=None, end_at=None, sku_id=None, channel=None):
        p.require("metrics.read")
        with self.database.transaction() as c:
            source = self._snapshot(c, p.store_id, snapshot_id)
        filters = {"sku_id": sku_id, "channel": channel}
        if sku_id and sku_id not in {row["sku_id"] for row in source["skus"]}:
            raise AppError("invalid_filter", "SKU不属于当前授权快照", 422)
        available_channels = {row["channel"].strip() or "unknown" for row in source["orders"]}
        if channel and channel not in available_channels:
            raise AppError("invalid_filter", "渠道不属于当前快照，请选择已有渠道", 422)
        source = deepcopy(source)
        if channel:
            source["orders"] = [row for row in source["orders"] if (row["channel"].strip() or "unknown") == channel]
        ambiguous = set()
        if sku_id:
            selected_orders = []
            for order in source["orders"]:
                items = [item for item in order["items"] if item["sku_id"] == sku_id]
                if items:
                    if len(items) != len(order["items"]):
                        ambiguous.add(order["order_id"])
                    order["items"] = items
                    order["paid_minor"] = str(sum(int(item["paid_minor"]) for item in items))
                    selected_orders.append(order)
            source["orders"] = selected_orders
            source["skus"] = [row for row in source["skus"] if row["sku_id"] == sku_id]
        selected_ids = {row["order_id"] for row in source["orders"]}
        now = self.clock()
        end = parse_time(end_at) if end_at else now
        start = parse_time(start_at) if start_at else end - timedelta(days=30)
        visible = min(now, parse_time(source["as_of"]))
        # 保留缺行分摊的成功退款，分别界定现金窗与支付批次的影响。
        uncertain = [row for row in source["refunds"] if row["order_id"] in ambiguous
                     and row["status"] == "succeeded" and int(row["amount_minor"]) > 0
                     and parse_time(row["occurred_at"]) <= visible]
        cash_unknown = [row for row in uncertain if start <= parse_time(row["occurred_at"]) < end]
        cohort_ids = {row["order_id"] for row in source["orders"] if row["status"] == "paid"
                      and start <= parse_time(row["paid_at"]) < end and parse_time(row["paid_at"]) <= visible}
        cohort_unknown = any(row["order_id"] in cohort_ids for row in uncertain)
        unknown_refunds = bool(cash_unknown or cohort_unknown)
        source["refunds"] = [row for row in source["refunds"] if row["order_id"] in selected_ids
                             and (row["order_id"] not in ambiguous or int(row["amount_minor"]) == 0)]
        metrics = calculate_metrics(source, start, end, now)
        if cash_unknown:
            for key in ("refund_minor", "net_paid_minor"):
                metrics[key] = None
        if cohort_unknown:
            for key in ("cohort_refund_order_rate", "cohort_refund_amount_rate"):
                metrics[key] = None
        # 退款未含退货/成本冲回、实付渠道费与固定费分摊，不从SKU当前成本虚造历史利润。
        result = {"snapshot": {k: source[k] for k in
                  ("snapshot_id", "revision", "source_kind", "as_of", "currency")},
                  "window": {"start_at": start.isoformat(), "end_at": end.isoformat(),
                             "semantics": "[start,end); cash events; cohort separate"},
                  "metrics": metrics, "skus": deepcopy(source["skus"]), "history": [],
                  "filters": filters, "query_ref": digest([p.store_id, source["snapshot_id"], source["revision"], start.isoformat(), end.isoformat(), filters]),
                  "order_count_semantics": "筛选内distinct订单数；不同SKU订单数不可相加",
                  "filter_missing": ["多SKU订单退款缺原生行分摊，相关SKU退款指标未知（现金期与支付批次分别判定）"] if unknown_refunds else []}
        # 每日图按上海自然日聚合；事件仍受所选半开窗及watermark约束。
        from zoneinfo import ZoneInfo
        zone = ZoneInfo("Asia/Shanghai")
        buckets = {}
        for kind, rows, timekey, amountkey in (
            ("payment_minor", source["orders"], "paid_at", "paid_minor"),
            ("refund_minor", source["refunds"], "occurred_at", "amount_minor")):
            for row in rows:
                if row["status"] != ("paid" if kind == "payment_minor" else "succeeded"):
                    continue
                when = parse_time(row[timekey])
                if not start <= when < end or when > visible:
                    continue
                key = when.astimezone(zone).date().isoformat()
                bucket = buckets.setdefault(key, {"date": key, "payment_minor": 0, "refund_minor": 0})
                bucket[kind] += int(row[amountkey])
        for row in cash_unknown:
            key = parse_time(row["occurred_at"]).astimezone(zone).date().isoformat()
            bucket = buckets.setdefault(key, {"date": key, "payment_minor": 0, "refund_minor": 0})
            bucket["refund_minor"] = None
            bucket.setdefault("refund_missing", []).append({
                "refund_id": row["refund_id"], "order_id": row["order_id"],
                "occurred_at": row["occurred_at"], "reason": "missing_sku_allocation"})
        result["history"] = [{k: str(v) if k.endswith("_minor") and v is not None else v for k, v in row.items()}
                             for _, row in sorted(buckets.items())]
        if "cost.read" in p.capabilities:
            result.update(cost_coverage={"known": sum(s["cost_minor"] is not None and
                s["variable_fee_minor"] is not None for s in source["skus"]), "total": len(source["skus"])},
                contribution_profit_minor=None,
                profit_missing_reason="实际退货成本冲回、渠道/履约实际费用与活动固定费分摊尚未提供；净支付不是利润")
        else:
            result["skus"] = self._public_snapshot(source)["skus"]
        return result

    def _head(self, c, store, plan_id):
        result = c.execute(select(db.plans).where(db.plans.c.plan_id == plan_id,
            db.plans.c.store_id == store)).mappings().first()
        if not result:
            raise AppError("not_found", "方案不存在或不可访问", 404)
        return dict(result)

    def _view(self, c, store, plan_id, version=None):
        head = self._head(c, store, plan_id)
        version = version or head["version"]
        revision = c.execute(select(db.revisions.c.body).where(
            db.revisions.c.plan_id == plan_id, db.revisions.c.store_id == store,
            db.revisions.c.version == version)).scalar_one_or_none()
        if revision is None:
            raise AppError("not_found", "方案版本不存在", 404)
        def latest(table):
            return c.execute(select(table.c.body).where(table.c.store_id == store,
                table.c.plan_id == plan_id, table.c.version == version)
                .order_by(table.c.created_at.desc())).scalars().first()
        approval = latest(db.approvals)
        evaluation = latest(db.evaluations)
        cancellation = latest(db.cancellations)
        # 历史版本不使用当前版本状态，输入与该版本评审证据仍可追溯。
        status = head["status"] if version == head["version"] else (
            "confirmed" if approval and approval.get("mode") != "demo" else
            "sandbox_confirmed" if approval else "evaluated" if evaluation else "draft")
        snapshot = c.execute(select(db.snapshots.c.body).where(db.snapshots.c.store_id == store,
            db.snapshots.c.snapshot_id == revision["snapshot_id"],
            db.snapshots.c.revision == revision["expected_snapshot_revision"])).scalar_one()
        latest_revision = c.execute(select(db.snapshots.c.revision).where(
            db.snapshots.c.store_id == store, db.snapshots.c.snapshot_id == revision["snapshot_id"])
            .order_by(db.snapshots.c.revision.desc())).scalars().first()
        age = self.clock() - parse_time(snapshot["as_of"])
        current = latest_revision == snapshot["revision"] and timedelta(0) <= age <= timedelta(hours=24)
        has_grant = c.execute(select(db.grants.c.grant_ref).where(db.grants.c.store_id == store,
            db.grants.c.plan_id == plan_id, db.grants.c.version == version)).first() is not None
        if not current and not has_grant and version == head["version"] and status != "cancelled":
            status = "invalidated"
        return {**head, "version": version, "status": status, "draft": revision,
                "title": revision["title"], "source_kind": snapshot["source_kind"],
                "evaluation": evaluation, "approval": approval, "cancellation": cancellation,
                "validity": {"snapshot_current": current, "latest_snapshot_revision": latest_revision,
                    "execution_frozen": has_grant,
                    "note": "批准经营假设已冻结；新数据待重评，不单方撤销在途执行" if has_grant and not current else
                            "快照已修订或超过24小时；历史值保留，须重新评估确认" if not current else "当前引用有效"}}

    def get_plan(self, p, plan_id, version=None):
        p.require("metrics.read", "cost.read")
        if version is not None:
            require_version(version)
        with self.database.transaction() as c:
            return self._view(c, p.store_id, plan_id, version)

    def list_plans(self, p):
        p.require("metrics.read")
        with self.database.transaction() as c:
            ids = c.execute(select(db.plans.c.plan_id).where(db.plans.c.store_id == p.store_id)
                .order_by(db.plans.c.updated_at.desc())).scalars().all()
            values = [self._view(c, p.store_id, identity) for identity in ids]
        if "cost.read" not in p.capabilities:
            values = [{key: value[key] for key in ("plan_id", "version", "title", "status",
                       "store_id", "source_kind", "created_at", "updated_at")} for value in values]
        return {"items": values}

    def save_plan(self, p, draft, key, plan_id=None, expected_version=None):
        p.require("plan.edit", "cost.read")
        if not isinstance(draft, dict):
            raise AppError("invalid_draft", "方案须为对象", 422)
        with self.database.transaction("store:" + p.store_id) as c:
            def save():
                source = self._bound_snapshot(c, p, draft)
                evaluate_plan(draft, source, self.clock())
                identity, version = plan_id or uid("plan"), 1
                now = self.clock().isoformat()
                if plan_id:
                    head = self._head(c, p.store_id, plan_id)
                    if head["version"] != require_version(expected_version):
                        raise AppError("version_conflict", "方案版本已变化，请刷新后保存")
                    frozen = c.execute(select(db.grants.c.grant_ref).where(
                        db.grants.c.plan_id == plan_id, db.grants.c.store_id == p.store_id)).first()
                    if frozen:
                        raise AppError("execution_frozen", "此方案已有执行授权；请核对活动与已占额度，另建方案")
                    version = head["version"] + 1
                    c.execute(db.plans.update().where(db.plans.c.plan_id == identity).values(
                        version=version, status="draft", updated_at=now))
                else:
                    c.execute(db.plans.insert().values(plan_id=identity, store_id=p.store_id,
                        version=1, status="draft", created_at=now, updated_at=now))
                c.execute(db.revisions.insert().values(plan_id=identity, version=version,
                    store_id=p.store_id, body=draft, digest=digest(draft), created_at=now))
                self._audit(c, p, "plan.save", identity)
                return {"plan_id": identity, "version": version}
            reference = self._idempotent(c, p, "plan.save:" + (plan_id or "new"), key,
                {"draft": draft, "expected_version": expected_version}, save)
            return self._view(c, p.store_id, reference["plan_id"], reference["version"])

    def evaluate(self, p, plan_id, expected_version):
        p.require("plan.edit", "cost.read")
        with self.database.transaction("store:" + p.store_id) as c:
            head = self._head(c, p.store_id, plan_id)
            if head["version"] != require_version(expected_version):
                raise AppError("version_conflict", "方案版本已变化")
            if head["status"] not in ("draft", "evaluated", "needs_data", "infeasible"):
                raise AppError("plan_locked", "已确认或执行中的方案不可覆写评估，请保存新版本")
            plan = self._view(c, p.store_id, plan_id)
            snapshot = self._bound_snapshot(c, p, plan["draft"])
            result = evaluate_plan(plan["draft"], snapshot, self.clock())
            c.execute(db.evaluations.insert().values(evaluation_id=uid("eval"), plan_id=plan_id,
                version=head["version"], store_id=p.store_id, body=result,
                created_at=self.clock().isoformat()))
            status = "evaluated" if result["feasibility"] == "checks_passed" else result["feasibility"]
            c.execute(db.plans.update().where(db.plans.c.plan_id == plan_id).values(
                status=status, updated_at=self.clock().isoformat()))
            self._audit(c, p, "plan.evaluate", plan_id)
            return self._view(c, p.store_id, plan_id)

    def confirm(self, p, plan_id, expected_version, reason, key, request):
        p.require("plan.confirm", "cost.read")
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
            raise AppError("invalid_input", "须填写不超过2000字的确认理由", 422)
        # 网络只读预检在数据库锁外；入库再查版本/快照/摘要，禁止长时间占交易锁。
        plan = self.get_plan(p, plan_id)
        if plan["version"] != require_version(expected_version):
            raise AppError("version_conflict", "方案版本已变化")
        preview = None
        if self.mode != "demo" and plan["status"] != "confirmed":
            preview = self.preview(p, plan_id, request)
        with self.database.transaction("store:" + p.store_id) as c:
            def approve():
                current = self._view(c, p.store_id, plan_id)
                if current["version"] != expected_version or current["status"] not in ("evaluated",):
                    raise AppError("plan_not_ready", "须先评估当前版本并解决全部必需缺口")
                source = self._bound_snapshot(c, p, current["draft"])
                evaluation = evaluate_plan(current["draft"], source, self.clock())
                if evaluation["feasibility"] != "checks_passed":
                    raise AppError("plan_not_ready", "；".join(evaluation["issues"]))
                if self.mode != "demo" and (not preview or preview.get("status") != "passed" or
                                            preview.get("evidence_kind") != "integration"):
                    raise AppError("execution_not_ready", "01真实执行预检尚未通过")
                if preview and parse_time(preview["expires_at"]) <= self.clock():
                    raise AppError("preview_expired", "01预检已过期，请重试")
                evaluation_id = c.execute(select(db.evaluations.c.evaluation_id).where(
                    db.evaluations.c.store_id == p.store_id,
                    db.evaluations.c.plan_id == plan_id, db.evaluations.c.version == expected_version)
                    .order_by(db.evaluations.c.created_at.desc())).scalars().first()
                approval = {"approval_ref": uid("approval"), "plan_ref": plan_id,
                    "plan_version": expected_version, "actor_ref": p.actor_ref,
                    "auth_version": p.auth_version, "capability_revision": p.capability_revision,
                    "mode": "demo" if self.mode == "demo" else "integration", "reason": reason,
                    "evaluation_id": evaluation_id,
                    "confirmed_at": self.clock().isoformat(), "preview": preview,
                    "source_kind": source["source_kind"],
                    "manifest_digest": preview["manifest_digest"] if preview else None,
                    "evidence_kind": preview["evidence_kind"] if preview else "sandbox"}
                c.execute(db.approvals.insert().values(approval_ref=approval["approval_ref"],
                    store_id=p.store_id, plan_id=plan_id, version=expected_version,
                    body=approval, created_at=self.clock().isoformat()))
                c.execute(db.plans.update().where(db.plans.c.plan_id == plan_id).values(
                    status="sandbox_confirmed" if self.mode == "demo" else "confirmed",
                    updated_at=self.clock().isoformat()))
                self._audit(c, p, "plan.confirm", plan_id)
                return {"plan_id": plan_id, "version": expected_version}
            ref = self._idempotent(c, p, "plan.confirm:" + plan_id, key,
                {"expected_version": expected_version, "reason": reason}, approve)
            return self._view(c, p.store_id, ref["plan_id"], ref["version"])

    def preview(self, p, plan_id, request):
        p.require("cost.read", "plan.edit")
        if self.mode == "demo":
            raise AppError("execution_unavailable", "演示模式不连接01真实活动执行器", 503)
        with self.database.transaction() as c:
            plan = self._view(c, p.store_id, plan_id)
            plan["snapshot"] = self._bound_snapshot(c, p, plan["draft"])
        return self.bridge.preview(plan, p, request)

    def cancel_approval(self, p, plan_id, expected_version, reason, key):
        p.require("plan.confirm", "cost.read")
        require_version(expected_version)
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 1000:
            raise AppError("invalid_input", "须填写不超过1000字的撤销理由", 422)
        with self.database.transaction("store:" + p.store_id) as c:
            def cancel():
                plan = self._view(c, p.store_id, plan_id)
                if plan["version"] != expected_version:
                    raise AppError("version_conflict", "方案版本已变化")
                if not plan["approval"] or plan["cancellation"]:
                    raise AppError("approval_not_active", "当前版本无可撤销审批或已有撤销请求")
                grant = c.execute(select(db.grants).where(db.grants.c.store_id == p.store_id,
                    db.grants.c.plan_id == plan_id, db.grants.c.version == expected_version)).mappings().first()
                value = {"cancel_ref": uid("cancel"), "plan_id": plan_id,
                    "version": expected_version, "actor_ref": p.actor_ref,
                    "auth_version": p.auth_version, "capability_revision": p.capability_revision,
                    "reason": reason, "requested_at": self.clock().isoformat(),
                    "intent_id": grant["intent_id"] if grant else None,
                    "meaning": "已向01申请核对；已提交活动需暂停/结束" if grant else "授权尚未签发，审批已在02撤销"}
                c.execute(db.cancellations.insert().values(cancel_ref=value["cancel_ref"],
                    plan_id=plan_id, version=expected_version, store_id=p.store_id,
                    body=value, created_at=self.clock().isoformat()))
                terminal = grant["body"].get("terminal") if grant else None
                status = (terminal["phase"] if terminal["status"] == "committed" else "closed_no_effect") if terminal else (
                    "cancel_pending" if grant else "cancelled")
                c.execute(db.plans.update().where(db.plans.c.plan_id == plan_id).values(
                    status=status, updated_at=self.clock().isoformat()))
                self._audit(c, p, "approval.cancel_request", plan_id)
                return value
            cancellation = self._idempotent(c, p, "approval.cancel:" + plan_id, key,
                {"expected_version": expected_version, "reason": reason}, cancel)
        if cancellation["intent_id"]:
            self.bridge.cancel(p.store_id, cancellation["intent_id"], {
                "actor_ref": p.actor_ref, "auth_version": p.auth_version,
                "capability_revision": p.capability_revision, "reason": cancellation["reason"]})
        return self.get_plan(p, plan_id, expected_version)

    def reconcile_execution(self, p, plan_id, expected_version):
        p.require("plan.confirm", "cost.read")
        require_version(expected_version)
        with self.database.transaction() as c:
            plan = self._view(c, p.store_id, plan_id)
            if plan["version"] != expected_version:
                raise AppError("version_conflict", "方案版本已变化")
            grant = c.execute(select(db.grants).where(db.grants.c.store_id == p.store_id,
                db.grants.c.plan_id == plan_id, db.grants.c.version == expected_version)).mappings().first()
            if not grant:
                raise AppError("execution_not_found", "此方案没有待核对的执行授权")
            intent = grant["intent_id"]
            expired = parse_time(grant["body"]["grant"]["expires_at"]) <= self.clock()
        if plan["status"] == "cancel_pending" and plan["cancellation"]:
            self.bridge.cancel(p.store_id, intent, {"actor_ref": p.actor_ref,
                "auth_version": p.auth_version, "capability_revision": p.capability_revision,
                "reason": plan["cancellation"]["reason"]})
        else:
            self.bridge.reconcile(p.store_id, intent, finalize=expired)
        return self.get_plan(p, plan_id)

    def activity_list(self, p):
        p.require("metrics.read")
        with self.database.transaction() as c:
            stored = c.execute(select(db.activities, db.plans.c.version, db.plans.c.updated_at)
                .join(db.plans, db.plans.c.plan_id == db.activities.c.plan_id)
                .where(db.activities.c.store_id == p.store_id)).mappings().all()
            rows = [{**row["body"], "plan_id": row["plan_id"], "plan_version": row["version"],
                     "updated_at": row["updated_at"]} for row in stored]
        return {"items": rows, "dependency_status": self.bridge.dependency_status}

    def monitor(self, p, plan_id):
        p.require("metrics.read", "cost.read")
        plan = self.get_plan(p, plan_id)
        with self.database.transaction() as c:
            activity_ids = list(c.execute(select(db.activities.c.activity_id).where(
                db.activities.c.store_id == p.store_id, db.activities.c.plan_id == plan_id)).scalars())
            rows = list(c.execute(select(db.actuals).where(db.actuals.c.store_id == p.store_id,
                db.actuals.c.activity_id.in_(activity_ids)).order_by(db.actuals.c.revision.desc(),
                db.actuals.c.created_at.desc())).mappings()) if activity_ids else []
        latest = max(rows, key=lambda r: (parse_time(r["body"]["watermark"]), r["revision"])) if rows else None
        actual = latest["body"] if latest else None
        # 实际聚合不同窗可能重叠；仅显示完整来源，绝不把多份快照累加。
        deviations = []
        if actual:
            actual = deepcopy(actual)
            actual["net_paid_minor"] = str(int(actual["payment_minor"]) - int(actual["refund_minor"]))
            actual["contribution_profit_minor"] = None
            actual["profit_missing_reason"] = "01未提供退货成本冲回及实际履约/渠道/固定费，不能以净支付替代利润"
            budget = int(plan["draft"]["goal"]["budget_minor"])
            allocated = (int(actual["consumed_discount_minor"]) + int(actual["reserved_discount_minor"]) +
                         int(plan["draft"]["fixed_fee_minor"]))
            deviations.append({"metric": "budget_available_minor", "value": str(budget - allocated),
                "status": "risk" if allocated >= budget else "within_limit",
                "meaning": "预算减已核销/未付预占让利及固定费预算分配；固定费分配不是现金已支付"})
            complete = (parse_time(actual["window_start"]) == parse_time(plan["draft"]["start_at"]) and
                        parse_time(actual["window_end"]) == parse_time(plan["draft"]["end_at"]) and
                        parse_time(actual["watermark"]) >= parse_time(plan["draft"]["end_at"]))
            if complete and plan["evaluation"]:
                candidate = next(row for row in plan["evaluation"]["candidates"]
                    if row["id"] == plan["draft"]["selected_candidate"])
                policy = plan["draft"]["goal"]["scenario_policy"]
                target = candidate["scenarios"]["base" if policy == "all" else policy]
                deviations.extend([
                    {"metric": "payment_vs_scenario_minor", "value": str(int(actual["payment_minor"]) -
                        int(target["revenue_minor"])), "status": "observed", "meaning": "原活动窗完整支付实收与选定情景差异；不是促销因果增量"},
                    {"metric": "units_vs_scenario", "value": actual["paid_units"] - target["units"],
                     "status": "observed", "meaning": "完整活动窗已付件数与选定情景差异"}])
            else:
                deviations.append({"metric": "period_comparison", "status": "partial",
                                   "meaning": "活动实际尚未覆盖完整计划窗，不与全期收入/销量目标作失败比较"})
        return {"plan": plan, "actual": actual, "deviations": deviations,
                "freshness": actual.get("watermark") if actual else None,
                "status": "actual_received" if actual else "awaiting_actual",
                "comparison_note": "实际窗未与目标窗完整对齐前不生成全期差异或收益归因"}

    def retrospective(self, p, body, key):
        p.require("plan.edit", "cost.read")
        if (not isinstance(body.get("summary"), str) or not body["summary"].strip() or
                not isinstance(body.get("actions"), list) or
                any(not isinstance(v, str) or not v.strip() for v in body["actions"])):
            raise AppError("invalid_input", "复盘须有摘要和文本行动项", 422)
        self.get_plan(p, body.get("plan_id"))
        with self.database.transaction("store:" + p.store_id) as c:
            def save():
                value = {**body, "retrospective_id": uid("retro"),
                         "evidence_note": "复盘为人工记录；实际事实以01回传快照为准"}
                c.execute(db.retrospectives.insert().values(retrospective_id=value["retrospective_id"],
                    store_id=p.store_id, plan_id=body["plan_id"], body=value,
                    created_at=self.clock().isoformat()))
                self._audit(c, p, "retrospective.save", value["retrospective_id"])
                return value
            return self._idempotent(c, p, "retrospective", key, body, save)

    def assistant(self, p, body):
        p.require("metrics.read")
        question = body.get("question")
        if (not isinstance(question, str) or not 1 <= len(question) <= 1000 or
                any(text in question for text in ("忽略权限", "其他店铺", "执行SQL", "发布活动", "采购", "投放广告"))):
            raise AppError("unsupported_question", "此版本支持授权店铺的经营指标与方案解释；执行请进入活动页", 422)
        if body.get("plan_id"):
            plan = self.get_plan(p, body["plan_id"])
            evaluation = plan["evaluation"]
            if not evaluation:
                return {"answer": "当前方案尚未测算，请先评估当前版本。", "mode": "deterministic",
                        "evidence": [{"title": "方案版本", "value": str(plan["version"])}],
                        "missing": ["当前版本评估"]}
            answer = "当前方案检查状态：" + evaluation["feasibility"] + "。" + "；".join(evaluation["issues"])
            return {"answer": answer, "mode": "deterministic", "evidence": [
                {"title": "经营快照", "value": evaluation["lineage"]["snapshot_id"]},
                {"title": "测算版本", "value": evaluation["lineage"]["calculation_revision"]}],
                "missing": evaluation["issues"]}
        if not any(text in question for text in ("销售", "支付", "退款", "收入", "库存", "经营", "利润", "收益", "渠道", "商品", "异常")):
            raise AppError("unsupported_question", "当前支持销售、退款、渠道、库存与方案解释，请说明指标", 422)
        if (("渠道" in question and not body.get("channel")) or
                (any(term in question for term in ("这个商品", "该商品", "这个SKU", "该SKU")) and not body.get("sku_id"))):
            return {"answer": "请选择本店SKU或渠道筛选，再继续查询。", "mode": "deterministic",
                    "evidence": [], "missing": ["sku_id或channel筛选条件"], "status": "clarify"}
        dashboard = self.dashboard(p, body.get("snapshot_id"), body.get("start_at"), body.get("end_at"),
                                   body.get("sku_id"), body.get("channel"))
        metrics = dashboard["metrics"]
        def yuan(minor):
            if minor is None:
                return "未知"
            value = int(minor)
            return ("-" if value < 0 else "") + f"{abs(value)//100}.{abs(value)%100:02d}"
        period = "所选时段" if body.get("start_at") or body.get("end_at") else "近30日"
        answer = f"{period}支付{yuan(metrics['payment_minor'])}元，退款{yuan(metrics['refund_minor'])}元，净支付{yuan(metrics['net_paid_minor'])}元，共{metrics['paid_order_count']}笔支付订单。"
        missing = dashboard["filter_missing"][:]
        if "利润" in question or "收益" in question:
            if "cost.read" not in p.capabilities:
                raise AppError("forbidden", "当前身份无利润数据权限", 403)
            missing = [dashboard["profit_missing_reason"]]
            answer += missing[0]
        if "库存" in question:
            answer += "库存：" + "；".join(s["title"] + "=" + str(s["available_qty"])
                                         for s in dashboard["skus"])
        return {"answer": answer, "mode": "deterministic",
                "evidence": [{"title": "快照及修订", "value": dashboard["snapshot"]["snapshot_id"] +
                    ":" + str(dashboard["snapshot"]["revision"])},
                    {"title": "统计窗口（左含右不含）", "value": {
                        "start_at": dashboard["window"]["start_at"],
                        "end_at": dashboard["window"]["end_at"]}},
                    {"title": "支付/退款口径", "value": "按各自发生时间；订单粒度去重；净支付不是利润"}],
                "missing": missing}

    def export(self, p, plan_id, format="markdown"):
        plan = self.get_plan(p, plan_id)
        draft = plan["draft"]
        with self.database.transaction() as c:
            snapshot = c.execute(select(db.snapshots.c.body).where(db.snapshots.c.store_id == p.store_id,
                db.snapshots.c.snapshot_id == draft["snapshot_id"],
                db.snapshots.c.revision == draft["expected_snapshot_revision"])).scalar_one()
            parents = []
            pending = list(snapshot.get("provenance", {}).get("parents", []))
            seen = set()
            while pending:
                parent = pending.pop(0)
                identity = (parent["snapshot_id"], parent["revision"])
                if identity in seen:
                    continue
                seen.add(identity)
                source = c.execute(select(db.snapshots.c.body).where(db.snapshots.c.store_id == p.store_id,
                    db.snapshots.c.snapshot_id == identity[0], db.snapshots.c.revision == identity[1])).scalar_one()
                parents.append({"reference": parent, "snapshot": source})
                pending.extend(source.get("provenance", {}).get("parents", []))
            activity_archive = list(c.execute(select(db.activities.c.body).where(
                db.activities.c.store_id == p.store_id, db.activities.c.plan_id == plan_id)).scalars())
            activity_ids = [row["activity_id"] for row in activity_archive]
            actual_archive = list(c.execute(select(db.actuals.c.body).where(
                db.actuals.c.store_id == p.store_id, db.actuals.c.activity_id.in_(activity_ids))).scalars()) if activity_ids else []
            grant_archive = list(c.execute(select(db.grants.c.body).where(
                db.grants.c.store_id == p.store_id, db.grants.c.plan_id == plan_id)).scalars())
            intents = [row["grant"]["intent_id"] for row in grant_archive]
            receipt_archive = list(c.execute(select(db.receipts.c.body).where(
                db.receipts.c.store_id == p.store_id, db.receipts.c.intent_id.in_(intents))).scalars()) if intents else []
            reviews = list(c.execute(select(db.retrospectives.c.body).where(
                db.retrospectives.c.store_id == p.store_id, db.retrospectives.c.plan_id == plan_id)).scalars())
        report = {"plan": plan, "source_snapshot": snapshot, "parent_archive": parents,
                  "grants": grant_archive, "receipts": receipt_archive, "activities": activity_archive,
                  "actual_revisions": actual_archive, "retrospectives": reviews,
                  "service_version": "OPS02-XOPS-20261023-01", "amount_unit": "CNY minor unit",
                  "effect_limit": "情景测算不是因果经营收益，净支付不是利润"}
        if format == "csv":
            out = io.StringIO(newline="")
            writer = csv.writer(out)
            writer.writerow(["plan_id", "version", "sku_id", "direct_price_minor", "low", "base", "high"])
            for line in draft["lines"]:
                writer.writerow([plan_id, plan["version"], "'" + line["sku_id"],
                    line["direct_price_minor"], *(line["quantities"][s] for s in ("low", "base", "high"))])
            writer.writerow([])
            writer.writerow(["section", "json_value"])
            for name, value in report.items():
                # 所有结构字段作为单元文本，前置单引号阻断表格公式解释。
                writer.writerow([name, "'" + json.dumps(value, ensure_ascii=False, sort_keys=True)])
            return "\ufeff" + out.getvalue(), "text/csv; charset=utf-8"
        if format != "markdown":
            raise AppError("invalid_input", "导出格式支持markdown/csv", 422)
        # 完整结构保留输入/依据/版本，导出并不赋予发布权。
        text = f"# {draft['title']}\n\n方案：{plan_id}，版本：{plan['version']}，状态：{plan['status']}\n\n来源：{plan['source_kind']}；本文件不代表真实收益保证。\n\n"
        text += "当前有效性：" + plan["validity"]["note"] + "\n\n"
        for title, value in (("目标、候选、资源、任务与监控", draft), ("确定性检查与依据", plan["evaluation"]),
                             ("人工确认", plan["approval"]), ("撤销请求与执行核对", plan["cancellation"])):
            text += "## " + title + "\n\n```json\n" + json.dumps(value, ensure_ascii=False, indent=2) + "\n```\n\n"
        text += "## 完整回放报告（金额单位：CNY整数分）\n\n```json\n" + json.dumps(report, ensure_ascii=False, indent=2) + "\n```\n"
        return text, "text/markdown; charset=utf-8"
