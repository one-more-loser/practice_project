import asyncio
import logging
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import and_, or_, select, update

from cs_agent.actions.repository import fingerprint, new_id
from cs_agent.audit.service import record
from cs_agent.cancellation.review import RULES, SKILL, parse_review, review_payload
from cs_agent.cancellation.service import (
    execute_refund,
    load_facts,
    rejected_result,
    scope,
    submission_parameters,
    validate_confirmation,
)
from cs_agent.core.context import Role, TrustedContext
from cs_agent.core.errors import AppError
from cs_agent.db.business_tables import confirmations, operations
from cs_agent.db.cancellation_tables import reviews
from cs_agent.db.models import LoginSession, Subject
from cs_agent.identity.service import check_subject
from cs_agent.models.gateway import ModelGateway

logger = logging.getLogger("cs_agent.cancellation.worker")


@dataclass(frozen=True)
class ReviewLease:
    review_id: str
    executor_id: str
    fence: int


def context_for(row):
    # 只由数据库内的已接收任务重建；金融执行前还需verify_binding再次鉴权。
    return TrustedContext(
        row["owner_id"],
        row["store_id"],
        Role.CUSTOMER,
        row["session_ref"],
        row["auth_version"],
        row["data_batch_id"],
        row["batch_generation"],
        row["request_id"],
    )


def lock_session_before_review(session, row):
    # HTTP鉴权先锁login_session，再锁confirmation；Worker必须沿用这个顺序。
    return session.scalar(
        select(LoginSession)
        .where(
            LoginSession.session_id == row["session_ref"],
        )
        .with_for_update()
    )


def verify_binding(session, row, current, now):
    ctx = context_for(row)
    subject = session.scalar(
        select(Subject).where(
            Subject.subject_id == ctx.subject_id,
        )
    )
    check_subject(session, subject)
    if (
        current is None
        or current.status != "active"
        or current.expires_at <= now
        or current.active_role != Role.CUSTOMER.value
        or current.subject_id != ctx.subject_id
        or current.auth_version != ctx.auth_version
        or subject.auth_version != ctx.auth_version
        or subject.subject_type != "customer"
        or subject.store_id != ctx.store_id
        or subject.data_batch_id != ctx.data_batch_id
        or subject.batch_generation != ctx.batch_generation
        or current.data_batch_id != ctx.data_batch_id
        or current.batch_generation != ctx.batch_generation
    ):
        raise AppError("unauthenticated", 401, "授权已变化，请重新登录并核对原操作")
    confirmation = (
        session.execute(
            select(confirmations)
            .where(
                confirmations.c.confirmation_id == row["confirmation_id"],
                *scope(confirmations, ctx),
            )
            .with_for_update()
        )
        .mappings()
        .first()
    )
    if not confirmation:
        raise AppError("cancellation_confirmation_invalid", 409, "原确认已失效")
    validate_confirmation(confirmation, ctx)
    from cs_agent.actions.origin import verify_origin

    verify_origin(session, confirmation["confirmation_id"], ctx)
    if (
        confirmation["status"] != "confirmed"
        or confirmation["resource_ref"] != row["order_id"]
        or confirmation["expected_version"] != row["expected_order_version"]
        or confirmation["confirmed_at"] != row["confirmed_at"]
        or not confirmation["created_at"] <= row["confirmed_at"] < confirmation["expires_at"]
        or row["confirmed_at"] - confirmation["created_at"] > timedelta(minutes=5)
    ):
        raise AppError("cancellation_confirmation_invalid", 409, "原确认与取消任务不一致")
    operation = (
        session.execute(
            select(operations)
            .where(
                operations.c.operation_id == row["operation_id"],
                *scope(operations, ctx),
            )
            .with_for_update()
        )
        .mappings()
        .first()
    )
    if (
        operation is None
        or operation["status"] != "accepted"
        or operation["actor_id"] != ctx.subject_id
        or operation["actor_role"] != Role.CUSTOMER.value
        or operation["action"] != "order.cancel"
        or operation["resource_ref"] != row["order_id"]
        or operation["confirmation_id"] != row["confirmation_id"]
        or operation["parameters_digest"]
        != fingerprint(submission_parameters(confirmation, "confirm"))
        or row["task_kind"] != "action"
        or row["rules_revision"] != RULES
        or row["skill_revision"] != SKILL
    ):
        raise AppError("cancellation_confirmation_invalid", 409, "原操作与取消任务不一致")
    return ctx


class CancellationWorker:
    """每次只领取一个持久任务；不创建聊天run，不在模型等待期间持事务/连接。"""

    def __init__(self, db, settings, clock, *, gateway=None):
        self.db, self.settings, self.clock = db, settings, clock
        self.gateway = gateway if gateway is not None else ModelGateway.from_settings(settings)
        self.executor_id = new_id("cancelworker")
        self.closed = False

    def _claim(self):
        now = self.clock.now()
        with self.db.transaction() as session:
            row = (
                session.execute(
                    select(reviews)
                    .where(
                        or_(
                            reviews.c.status == "queued",
                            and_(reviews.c.status == "running", reviews.c.lease_until <= now),
                        ),
                    )
                    .order_by(reviews.c.created_at, reviews.c.review_id)
                    .with_for_update(skip_locked=True)
                    .limit(1)
                )
                .mappings()
                .first()
            )
            if row is None:
                return None
            if row["attempts"] >= 3:
                self._terminal(
                    session, row, "unavailable", "cancellation_review_attempts_exceeded", now
                )
                return None
            fence = row["fence"] + 1
            session.execute(
                update(reviews)
                .where(reviews.c.review_id == row["review_id"])
                .values(
                    status="running",
                    executor_id=self.executor_id,
                    fence=fence,
                    attempts=row["attempts"] + 1,
                    lease_until=now + timedelta(seconds=90),
                    started_at=now,
                    updated_at=now,
                    version=reviews.c.version + 1,
                )
            )
            return ReviewLease(row["review_id"], self.executor_id, fence)

    def _lock(self, session, lease, now):
        preliminary = (
            session.execute(
                select(reviews).where(
                    reviews.c.review_id == lease.review_id,
                )
            )
            .mappings()
            .first()
        )
        if preliminary is None:
            raise AppError("stale_execution", 409, "审核任务已变更")
        current = lock_session_before_review(session, preliminary)
        row = (
            session.execute(
                select(reviews)
                .where(
                    reviews.c.review_id == lease.review_id,
                )
                .with_for_update()
            )
            .mappings()
            .first()
        )
        if (
            not row
            or row["status"] != "running"
            or row["fence"] != lease.fence
            or row["executor_id"] != lease.executor_id
            or row["lease_until"] is None
            or row["lease_until"] <= now
        ):
            raise AppError("stale_execution", 409, "审核任务已变更")
        return dict(row), current

    def _prepare(self, lease):
        now = self.clock.now()
        with self.db.transaction() as session:
            row, current = self._lock(session, lease, now)
            ctx = verify_binding(session, row, current, now)
            facts, *_ = load_facts(session, ctx, row, lock=True)
            session.execute(
                update(reviews)
                .where(reviews.c.review_id == row["review_id"])
                .values(
                    facts=facts,
                    facts_digest=fingerprint(facts),
                    updated_at=now,
                )
            )
            return facts

    def process_once(self):
        if self.closed:
            return False
        lease = self._claim()
        if lease is None:
            return False
        try:
            facts = self._prepare(lease)
            # _prepare事务已提交并释放连接。模型输出无工具、无金融写权限。
            config = self.gateway._config("chat")
            message = self.gateway.invoke("chat", review_payload(facts), remaining=20)
            decision = parse_review(message, facts)
            self._complete(lease, decision, config.model, config.service_revision)
        except AppError as exc:
            if exc.code != "stale_execution":
                status = (
                    "unavailable"
                    if exc.code.startswith(("model_", "cancellation_review_protocol"))
                    else "execution_rejected"
                )
                self._fail(lease, status, exc.code)
        except Exception:
            # 数据库提交结果未知时不得改写成业务失败，也不另造operation退款。
            # 留下租约，后继先查持久状态：已成功不可领取，未提交才能重领。
            logger.error("cancellation_task_result_unknown review_id=%s", lease.review_id)
        return True

    def _complete(self, lease, decision, model, revision):
        now = self.clock.now()
        with self.db.transaction() as session:
            row, current = self._lock(session, lease, now)
            changes = {
                name: decision[name]
                for name in ("decision", "checks", "rule_refs", "reason_codes", "basis_summary")
            }
            changes.update(model_identifier=model, model_revision=revision, reviewed_at=now)
            session.execute(
                update(reviews).where(reviews.c.review_id == row["review_id"]).values(**changes)
            )
            row.update(changes)
            if decision["decision"] != "approved":
                status = decision["decision"]
                self._terminal(session, row, status, "cancellation_review_" + status, now)
                return
            try:
                with session.begin_nested():
                    ctx = verify_binding(session, row, current, now)
                    result = execute_refund(session, ctx, row, now)
            except AppError as exc:
                self._terminal(session, row, "execution_rejected", exc.code, now)
                return
            session.execute(
                update(reviews)
                .where(reviews.c.review_id == row["review_id"])
                .values(
                    status="succeeded",
                    refund_id=result["refund_id"],
                    entry_id=result["entry_id"],
                    executed_at=now,
                    lease_until=None,
                    executor_id=None,
                    error_code=None,
                    updated_at=now,
                    version=reviews.c.version + 1,
                )
            )
            session.execute(
                update(operations)
                .where(operations.c.operation_id == row["operation_id"])
                .values(
                    status="succeeded",
                    result=result,
                    http_status=200,
                    updated_at=now,
                    version=operations.c.version + 1,
                )
            )

    def _terminal(self, session, row, status, code, now):
        result, http_status = rejected_result(
            row["operation_id"],
            row["order_id"],
            code,
            "取消未执行；请核对审核结果，需协助时补齐事实后重新确认",
            503 if status == "unavailable" else 409,
            row["review_id"],
        )
        result["review_status"] = status
        session.execute(
            update(reviews)
            .where(reviews.c.review_id == row["review_id"])
            .values(
                status=status,
                error_code=code,
                lease_until=None,
                executor_id=None,
                updated_at=now,
                version=reviews.c.version + 1,
            )
        )
        session.execute(
            update(operations)
            .where(
                operations.c.operation_id == row["operation_id"],
                operations.c.status == "accepted",
            )
            .values(
                status="rejected",
                result=result,
                http_status=http_status,
                updated_at=now,
                version=operations.c.version + 1,
            )
        )
        record(
            session,
            request_id=row["request_id"],
            action="cancel.reviewed",
            outcome="denied",
            ctx=context_for(row),
            resource_ref=row["review_id"],
            summary={"version": row["version"]},
        )

    def _fail(self, lease, status, code):
        try:
            now = self.clock.now()
            with self.db.transaction() as session:
                row, _ = self._lock(session, lease, now)
                self._terminal(session, row, status, code, now)
        except AppError as exc:
            if exc.code != "stale_execution":
                logger.error("cancellation_failure_state_unavailable review_id=%s", lease.review_id)
        except Exception:
            logger.error("cancellation_failure_state_unavailable review_id=%s", lease.review_id)

    async def run(self, stop_event: asyncio.Event):
        while not stop_event.is_set():
            try:
                worked = await asyncio.to_thread(self.process_once)
            except Exception:
                logger.error("cancellation_worker_poll_failed")
                worked = False
            if not worked:
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=0.75)
                except TimeoutError:
                    pass

    def close(self):
        # Gateway为主应用共享资源，不能由本Worker关闭。
        self.closed = True
