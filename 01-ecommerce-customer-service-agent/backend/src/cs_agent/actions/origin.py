from sqlalchemy import select

from cs_agent.core.errors import AppError
from cs_agent.db.agent_action_tables import bindings
from cs_agent.db.runtime_tables import conversations


def propose_for_run(session, ctx, lease, args, now):
    from cs_agent.actions.repository import fingerprint, new_id, provenance
    from cs_agent.actions.service import ActionService
    from cs_agent.db.business_tables import confirmations

    digest = fingerprint(args)
    existing = (
        session.execute(
            select(confirmations)
            .join(bindings, bindings.c.confirmation_id == confirmations.c.confirmation_id)
            .where(bindings.c.run_id == lease.run_id, bindings.c.request_digest == digest)
        )
        .mappings()
        .first()
    )
    if existing:
        return {
            "confirmation_id": existing["confirmation_id"],
            "summary": existing["summary"],
            "expires_at": existing["expires_at"].isoformat(),
            "business_effect": "none",
        }
    result = ActionService().propose(
        session,
        ctx,
        args["action"],
        args["resource_ref"],
        args["parameters"],
        args["expected_version"],
        now,
    )
    session.execute(
        bindings.insert().values(
            binding_id=new_id("binding"),
            confirmation_id=result["confirmation_id"],
            conversation_id=lease.conversation_id,
            run_id=lease.run_id,
            mode_epoch=lease.epoch,
            request_digest=digest,
            **provenance(ctx, now),
        )
    )
    return {**result, "business_effect": "none"}


def verify_origin(session, confirmation_id, ctx):
    binding = (
        session.execute(select(bindings).where(bindings.c.confirmation_id == confirmation_id))
        .mappings()
        .first()
    )
    if not binding:
        return  # 本人业务页面提案不依赖聊天模式。
    conversation = (
        session.execute(
            select(conversations)
            .where(
                conversations.c.conversation_id == binding["conversation_id"],
                conversations.c.owner_id == ctx.subject_id,
                conversations.c.store_id == ctx.store_id,
                conversations.c.data_batch_id == ctx.data_batch_id,
                conversations.c.batch_generation == ctx.batch_generation,
            )
            .with_for_update()
        )
        .mappings()
        .first()
    )
    if (
        not conversation
        or conversation["status"] != "open"
        or conversation["mode"] != "agent"
        or conversation["mode_epoch"] != binding["mode_epoch"]
    ):
        raise AppError("state_conflict", 409, "会话服务已变化，请重新核对操作")
