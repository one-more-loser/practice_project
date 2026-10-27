import hashlib
import json
import re
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert

from cs_agent.core.errors import AppError
from cs_agent.db.business_tables import operations


def new_id(prefix):
    return prefix + "_" + uuid4().hex


def provenance(ctx, now):
    return {
        "store_id": ctx.store_id,
        "source_kind": "derived",
        "data_batch_id": ctx.data_batch_id,
        "batch_generation": ctx.batch_generation,
        "created_at": now,
        "updated_at": now,
        "version": 1,
    }


def fingerprint(parameters):
    return hashlib.sha256(
        json.dumps(parameters, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def execute_once(
    session, ctx, action, key, parameters, now, execute, *, confirmation_id=None, resource_ref=None
):
    """无外部等待的短事务：请求去重门槛→业务锁→原始结果。

    单一数据库事务内保存意图和效果。业务拒绝回滚savepoint，保留拒绝结果；
    整体提交失败则不返回成功。未来外部Agent审核另建持久接收任务，不持此事务等待模型。
    """
    if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{8,128}", key):
        raise AppError("invalid_input", 422, "请提供有效的操作编号")
    digest = fingerprint(parameters)
    scope = [
        operations.c.actor_id == ctx.subject_id,
        operations.c.actor_role == ctx.role.value,
        operations.c.store_id == ctx.store_id,
        operations.c.store_id == ctx.store_id,
        operations.c.data_batch_id == ctx.data_batch_id,
        operations.c.batch_generation == ctx.batch_generation,
        operations.c.action == action,
        operations.c.idempotency_key == key,
    ]
    op_id = new_id("op")
    row = session.execute(
        insert(operations)
        .values(
            operation_id=op_id,
            actor_id=ctx.subject_id,
            actor_role=ctx.role.value,
            action=action,
            idempotency_key=key,
            parameters_digest=digest,
            resource_ref=resource_ref,
            confirmation_id=confirmation_id,
            status="accepted",
            result={},
            http_status=202,
            **provenance(ctx, now),
        )
        .on_conflict_do_nothing()
        .returning(operations.c.operation_id)
    ).scalar_one_or_none()
    if row is None:
        original = session.execute(select(operations).where(*scope)).mappings().first()
        if original is None and confirmation_id:
            original = (
                session.execute(
                    select(operations).where(
                        operations.c.confirmation_id == confirmation_id,
                        operations.c.actor_id == ctx.subject_id,
                        operations.c.actor_role == ctx.role.value,
                        operations.c.data_batch_id == ctx.data_batch_id,
                        operations.c.batch_generation == ctx.batch_generation,
                    )
                )
                .mappings()
                .first()
            )
        if original is None or original["parameters_digest"] != digest:
            raise AppError("idempotency_conflict", 409, "操作编号已用于其他内容，请核对原操作")
        return original["result"], original["http_status"]
    try:
        with session.begin_nested():
            result = execute(op_id)
        result = {"operation_id": op_id, "status": "succeeded", **result}
        status = 200
    except AppError as exc:
        result = {
            "operation_id": op_id,
            "status": "rejected",
            "error": {"code": exc.code, "message": exc.message, "retryable": False},
        }
        status = exc.status
    session.execute(
        update(operations)
        .where(operations.c.operation_id == op_id)
        .values(status=result["status"], result=result, http_status=status, updated_at=now)
    )
    return result, status
