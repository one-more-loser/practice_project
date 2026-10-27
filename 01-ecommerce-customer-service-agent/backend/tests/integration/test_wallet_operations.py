from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import pytest
from sqlalchemy import func, select

from cs_agent.core.errors import AppError
from cs_agent.db.business_tables import operations, wallet_entries, wallets
from cs_agent.wallet.service import WalletService


def test_topups_use_zero_opening_balance_and_exact_ledger(business):
    db, owner, ctx, now = business
    service = WalletService()
    with db.transaction() as session:
        first, status = service.topup(session, ctx, "1234", "topup_key_first", now)
        second, _ = service.topup(session, ctx, "66", "topup_key_second", now)
    assert status == 200 and first["balance_minor"] == "1234"
    assert second["balance_minor"] == "1300"
    with owner.connect() as conn:
        rows = conn.execute(
            select(
                wallet_entries.c.amount_minor,
                wallet_entries.c.balance_before_minor,
                wallet_entries.c.balance_after_minor,
            )
            .where(wallet_entries.c.owner_id == ctx.subject_id)
            .order_by(wallet_entries.c.sequence)
        ).all()
        assert rows == [(0, 0, 0), (1234, 0, 1234), (66, 1234, 1300)]


def test_same_key_replay_after_24_hours_never_credits_again(business):
    db, owner, ctx, now = business
    with db.transaction() as session:
        original = WalletService().topup(session, ctx, "10000", "stable_topup_key", now)
    with db.transaction() as session:
        replay = WalletService().topup(
            session, ctx, "10000", "stable_topup_key", now + timedelta(hours=25)
        )
    assert replay == original
    with db.transaction() as session, pytest.raises(AppError) as exc:
        WalletService().topup(session, ctx, "20000", "stable_topup_key", now)
    assert exc.value.code == "idempotency_conflict"


def test_concurrent_identical_topup_has_one_effect(business):
    db, owner, ctx, now = business
    barrier = Barrier(2)

    def run():
        with db.transaction() as session:
            barrier.wait(timeout=10)
            return WalletService().topup(session, ctx, "10000", "concurrent_topup_key", now)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: run(), range(2)))
    assert results[0] == results[1]
    with owner.connect() as conn:
        assert (
            conn.execute(
                select(wallets.c.balance_minor).where(wallets.c.owner_id == ctx.subject_id)
            ).scalar_one()
            == 10000
        )
        assert (
            conn.execute(
                select(func.count())
                .select_from(wallet_entries)
                .where(wallet_entries.c.kind == "topup")
            ).scalar_one()
            == 1
        )


def test_rollback_does_not_leave_a_successful_money_effect(business):
    db, owner, ctx, now = business
    with pytest.raises(RuntimeError):
        with db.transaction() as session:
            WalletService().topup(session, ctx, "10000", "rolled_back_topup", now)
            raise RuntimeError("synthetic transport failure before commit")
    with owner.connect() as conn:
        assert (
            conn.execute(
                select(wallets.c.balance_minor).where(wallets.c.owner_id == ctx.subject_id)
            ).scalar_one()
            == 0
        )
        assert conn.execute(select(func.count()).select_from(operations)).scalar_one() == 0


@pytest.mark.parametrize("amount", ["0", "-1", "01", "1.23", "1e2", 1, True])
def test_invalid_topup_is_rejected_before_financial_effect(business, amount):
    db, _, ctx, now = business
    with db.transaction() as session, pytest.raises(AppError):
        WalletService().topup(session, ctx, amount, "invalid_topup_key", now)
