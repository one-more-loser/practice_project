import os
from datetime import timedelta

from sqlalchemy import func, select, text, update

from cs_agent.db.business_tables import orders, wallet_entries, wallets
from cs_agent.db.experience_tables import snapshots
from cs_agent.db.models import AuditEvent, SourceBatch
from cs_agent.db.runtime_tables import events, messages, runs
from cs_agent.db.seed import BATCH, STORE
from cs_agent.db.session import Database
from cs_agent.retention.reset import reset_synthetic_batch
from cs_agent.retention.service import EXPIRED, expire
from cs_agent.runtime.worker import AgentWorker
from tests.api.test_checkout import create_order, key
from tests.api.test_graph_scripts import ScriptedModels


def test_retention_scrubs_history_checkpoint_and_cursor_without_changing_money(
    environment, login_as
):
    from scripts.setup_checkpoints import setup

    setup(os.environ["CS_TEST_MIGRATION_URL"], os.environ["CS_ISOLATION_FILE"])
    client, engine, clock, *_ = environment
    customer = login_as("customer_a")
    order = create_order(client, customer)
    ref = client.post("/api/v1/conversations", headers=customer, json={}).json()["data"][
        "conversation_id"
    ]
    accepted = client.post(
        f"/api/v1/conversations/{ref}/messages",
        headers=customer,
        json={"client_message_id": "retention-old", "text": "retention-source-marker"},
    )
    worker = AgentWorker(client.app.state.db, client.app.state.settings, clock)
    try:
        worker.gateway = ScriptedModels([{"kind": "answer", "text": "请补充信息"}])
        assert worker.process_once()
    finally:
        worker.close()
    run = accepted.json()["data"]["run_id"]
    with engine.begin() as conn:
        assert (
            conn.scalar(
                text("SELECT count(*) FROM lg.checkpoints WHERE thread_id=:id"), {"id": run}
            )
            > 0
        )
        conn.execute(
            update(messages)
            .where(messages.c.conversation_id == ref)
            .values(created_at=clock.now() - timedelta(days=31))
        )
        conn.execute(
            update(events)
            .where(events.c.conversation_id == ref)
            .values(created_at=clock.now() - timedelta(days=31))
        )
        audit = conn.scalar(select(AuditEvent.audit_id).limit(1))
        conn.execute(
            update(AuditEvent)
            .where(AuditEvent.audit_id == audit)
            .values(occurred_at=clock.now() - timedelta(days=91))
        )
        conn.execute(
            snapshots.insert().values(
                snapshot_id="expired-summary-row",
                window_start=clock.now() - timedelta(days=190),
                window_end=clock.now() - timedelta(days=183),
                revision=1,
                source_digest="0" * 64,
                source_ids={},
                payload={"snapshot_id": "expired-summary"},
                store_id=STORE,
                data_batch_id=BATCH,
                batch_generation=1,
                source_kind="derived",
                version=1,
                created_at=clock.now(),
                updated_at=clock.now(),
            )
        )
        before = list(
            conn.execute(
                select(
                    wallet_entries.c.entry_id,
                    wallet_entries.c.amount_minor,
                    wallet_entries.c.balance_after_minor,
                ).order_by(wallet_entries.c.entry_id)
            )
        )
    # 同一会话的新记录仍保留；旧上下文和正在处理的旧代次失效。
    fresh = client.post(
        f"/api/v1/conversations/{ref}/messages",
        headers=customer,
        json={"client_message_id": "retention-fresh", "text": "新的咨询内容"},
    )
    result = expire(engine, clock.now())
    assert result["message_contents_expired"] == 2 and result["summary_revisions_expired"] == 1
    assert result["financial_records_modified"] is False
    with engine.connect() as conn:
        assert (
            conn.scalar(
                text("SELECT count(*) FROM lg.checkpoints WHERE thread_id=:id"), {"id": run}
            )
            == 0
        )
        assert (
            conn.scalar(
                select(func.count()).select_from(AuditEvent).where(AuditEvent.audit_id == audit)
            )
            == 0
        )
        assert (
            list(
                conn.execute(
                    select(
                        wallet_entries.c.entry_id,
                        wallet_entries.c.amount_minor,
                        wallet_entries.c.balance_after_minor,
                    ).order_by(wallet_entries.c.entry_id)
                )
            )
            == before
        )
        assert conn.scalar(select(orders.c.order_id).where(orders.c.order_id == order)) == order
        assert (
            conn.scalar(
                select(runs.c.status).where(runs.c.run_id == fresh.json()["data"]["run_id"])
            )
            == "cancelled"
        )
    history = client.get(f"/api/v1/conversations/{ref}/messages").json()["data"]["items"]
    assert [m["text"] for m in history[:2]] == [EXPIRED, EXPIRED]
    assert history[-1]["text"] == "新的咨询内容"
    assert (
        client.get(f"/api/v1/conversations/{ref}/events?after_seq=0&follow=false").status_code
        == 409
    )
    assert expire(engine, clock.now())["conversations_fenced"] == 0


def test_whole_synthetic_reset_changes_generation_revokes_old_refs_and_keeps_zero_ledger(
    environment, login_as
):
    client, engine, clock, *_ = environment
    customer = login_as("customer_a")
    assert (
        client.post(
            "/api/v1/me/topups",
            headers={**customer, **key()},
            json={"currency": "CNY", "amount_minor": "10000"},
        ).status_code
        == 200
    )
    order = create_order(client, customer)
    proposal = client.post(
        "/api/v1/action-proposals",
        headers=customer,
        json={"action": "order.cancel", "resource_ref": order, "expected_version": 1},
    ).json()["data"]["confirmation_id"]
    ref = client.post("/api/v1/conversations", headers=customer, json={}).json()["data"][
        "conversation_id"
    ]
    settings = client.app.state.settings
    client.app.state.db.close()
    client.app.state.db = None
    reset = reset_synthetic_batch(engine, BATCH, 1, clock.now())
    assert reset["generation"] == 2 and reset["new_batch"] != BATCH
    client.app.state.db = Database(settings)
    assert client.get("/api/v1/session").status_code == 401
    customer = login_as("customer_a")
    assert client.get("/api/v1/me/wallet").json()["data"]["balance_minor"] == "0"
    assert client.get(f"/api/v1/me/orders/{order}").status_code == 404
    assert client.get(f"/api/v1/conversations/{ref}").status_code == 404
    assert (
        client.post(
            f"/api/v1/confirmations/{proposal}/submit",
            headers={**customer, **key()},
            json={"decision": "confirm"},
        ).status_code
        == 404
    )
    with engine.connect() as conn:
        assert (
            conn.scalar(select(SourceBatch.status).where(SourceBatch.batch_id == BATCH))
            == "retired"
        )
        assert conn.scalar(select(func.count()).select_from(wallet_entries)) == 2
        assert all(
            row.balance_minor == 0 and row.ledger_sequence == 0
            for row in conn.execute(select(wallets))
        )
