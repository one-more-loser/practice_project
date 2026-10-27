import re

from sqlalchemy import select, update

from cs_agent.actions.repository import execute_once, new_id, provenance
from cs_agent.audit.service import record
from cs_agent.core.context import Role
from cs_agent.core.errors import AppError
from cs_agent.db.business_tables import MAX_MINOR, outbox, wallet_entries, wallets
from cs_agent.identity.policies import require_role


class WalletService:
    def topup(self, session, ctx, amount_minor, key, now):
        require_role(ctx, Role.CUSTOMER)
        amount = parse_amount(amount_minor)

        def execute(op_id):
            entry_id, balance, version = post_entry(
                session, ctx, amount, "topup", op_id, op_id, now
            )
            return {
                "business_ref": entry_id,
                "balance_minor": str(balance),
                "currency": "CNY",
                "version": version,
                "simulated": True,
            }

        return execute_once(
            session,
            ctx,
            "wallet.topup",
            key,
            {"amount_minor": str(amount), "currency": "CNY"},
            now,
            execute,
        )

    def read(self, session, ctx):
        require_role(ctx, Role.CUSTOMER)
        account = account_row(session, ctx)
        return {
            "account_id": account["account_id"],
            "balance_minor": str(account["balance_minor"]),
            "currency": "CNY",
            "version": account["version"],
            "status": account["status"],
            "simulated": True,
        }

    def entries(self, session, ctx, limit=20, offset=0):
        require_role(ctx, Role.CUSTOMER)
        rows = session.execute(
            select(wallet_entries)
            .where(
                wallet_entries.c.owner_id == ctx.subject_id,
                wallet_entries.c.store_id == ctx.store_id,
                wallet_entries.c.data_batch_id == ctx.data_batch_id,
                wallet_entries.c.batch_generation == ctx.batch_generation,
            )
            .order_by(wallet_entries.c.sequence.desc())
            .limit(limit)
            .offset(offset)
        ).mappings()
        return [
            {
                "entry_id": row["entry_id"],
                "kind": row["kind"],
                "sequence": row["sequence"],
                "amount_minor": str(row["amount_minor"]),
                "balance_before_minor": str(row["balance_before_minor"]),
                "balance_after_minor": str(row["balance_after_minor"]),
                "currency": "CNY",
                "business_ref": row["business_ref"],
                "operation_id": row["operation_id"],
                "occurred_at": row["occurred_at"].isoformat(),
            }
            for row in rows
        ]


def parse_amount(value):
    if (
        type(value) is not str
        or not re.fullmatch(r"[1-9][0-9]{0,18}", value)
        or int(value) > MAX_MINOR
    ):
        raise AppError("invalid_input", 422, "请输入分单位的合法正金额")
    return int(value)


def account_row(session, ctx, lock=False):
    stmt = select(wallets).where(
        wallets.c.owner_id == ctx.subject_id,
        wallets.c.store_id == ctx.store_id,
        wallets.c.data_batch_id == ctx.data_batch_id,
        wallets.c.batch_generation == ctx.batch_generation,
    )
    row = session.execute(stmt.with_for_update() if lock else stmt).mappings().first()
    if row is None:
        raise AppError("unavailable", 503, "账户尚未初始化")
    return row


def event(session, ctx, event_type, aggregate_id, version, business_ref, payload, now):
    session.execute(
        outbox.insert().values(
            event_id=new_id("evt"),
            event_type=event_type,
            aggregate_id=aggregate_id,
            aggregate_version=version,
            business_ref=business_ref,
            dedupe_key=f"{event_type}:{business_ref}",
            contract_version="EVT-CS-v0.1",
            payload=payload,
            occurred_at=now,
            recorded_at=now,
            status="pending",
            **provenance(ctx, now),
        )
    )


def post_entry(session, ctx, amount, kind, operation_id, business_ref, now):
    account = account_row(session, ctx, lock=True)
    if account["status"] != "active":
        raise AppError("state_conflict", 409, "账户当前不可用于交易")
    after = account["balance_minor"] + (-amount if kind == "payment" else amount)
    if after < 0:
        raise AppError("insufficient_balance", 409, "余额不足，请先模拟充值")
    if after > MAX_MINOR:
        raise AppError("invalid_input", 422, "金额超出系统可表示范围")
    entry_id = new_id("entry")
    version = account["version"] + 1
    session.execute(
        update(wallets)
        .where(wallets.c.account_id == account["account_id"])
        .values(
            balance_minor=after,
            ledger_sequence=account["ledger_sequence"] + 1,
            version=version,
            updated_at=now,
        )
    )
    session.execute(
        wallet_entries.insert().values(
            entry_id=entry_id,
            account_id=account["account_id"],
            owner_id=ctx.subject_id,
            operation_id=operation_id,
            sequence=account["ledger_sequence"] + 1,
            kind=kind,
            currency="CNY",
            amount_minor=amount,
            balance_before_minor=account["balance_minor"],
            balance_after_minor=after,
            business_ref=business_ref,
            occurred_at=now,
            recorded_at=now,
            **provenance(ctx, now),
        )
    )
    event(
        session,
        ctx,
        "wallet.entry.posted",
        account["account_id"],
        version,
        entry_id,
        {
            "owner_ref": ctx.subject_id,
            "kind": kind,
            "amount_minor": str(amount),
            "balance_minor": str(after),
        },
        now,
    )
    record(
        session,
        request_id=ctx.request_id,
        action="wallet." + kind,
        outcome="succeeded",
        ctx=ctx,
        resource_ref=entry_id,
        summary={"version": version},
    )
    return entry_id, after, version
