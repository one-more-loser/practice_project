"""完整合成批次重置。只有维护凭据和停服务窗口可调用，不注册公开接口。"""

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.orm import Session

from cs_agent.actions.repository import new_id
from cs_agent.db.models import Base, RoleGrant, ShippingInput, SourceBatch, Store, Subject
from cs_agent.db.runtime_tables import runs
from cs_agent.db.seed_business import seed_accounts, seed_catalog
from cs_agent.retention.service import clear_checkpoints, require_maintenance


def reset_synthetic_batch(engine, expected_batch, expected_generation, now):
    require_maintenance(engine)
    with Session(engine) as session, session.begin():
        session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended('cs-maintenance',0))"))
        # 明确停API/Worker；不在活跃请求或后台模型任务中删除财务记录。
        busy = session.scalar(
            text(
                "SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND usename='cs_agent_app_b0'"
            )
        )
        if busy:
            raise RuntimeError("stop project API and workers before resetting the synthetic batch")
        session.execute(text("LOCK TABLE login_session IN ACCESS EXCLUSIVE MODE"))
        batch = session.get(SourceBatch, expected_batch)
        if (
            not batch
            or batch.status != "active"
            or batch.source_kind != "synthetic"
            or batch.generation != expected_generation
        ):
            raise RuntimeError("synthetic batch identity/generation mismatch")
        if session.scalar(select(func.count()).select_from(Store)) != 1:
            raise RuntimeError("whole-demo reset requires one isolated store")
        for table in Base.metadata.sorted_tables:
            if "data_batch_id" in table.c:
                other = session.scalar(
                    select(func.count())
                    .select_from(table)
                    .where(
                        table.c.data_batch_id.is_not(None), table.c.data_batch_id != expected_batch
                    )
                )
                if other:
                    raise RuntimeError(
                        "another dataset is present; reset requires a separate reviewed plan"
                    )
        store = session.execute(select(Store.__table__)).mappings().one()
        if store["data_batch_id"] != expected_batch:
            raise RuntimeError("store is not bound to the expected demo batch")
        subjects = [dict(row) for row in session.execute(select(Subject.__table__)).mappings()]
        grants = [dict(row) for row in session.execute(select(RoleGrant.__table__)).mappings()]
        # 仅保留原始合成收货样本，不复制顾客后来录入的完整资料。
        contacts = [
            dict(row)
            for row in session.execute(
                select(ShippingInput.__table__).where(
                    ShippingInput.input_id.in_(["shipping_sim_a", "shipping_sim_b"]),
                    ShippingInput.source_kind == "synthetic",
                )
            ).mappings()
        ]
        threads = session.scalars(select(runs.c.checkpoint_thread)).all()
        clear_checkpoints(session, threads)
        # 整个隔离演示批次重置时也移除失去public引用的旧检查点。
        for table in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
            if session.scalar(text("SELECT to_regclass(:name)"), {"name": "lg." + table}):
                session.execute(text(f"DELETE FROM lg.{table}"))
        for table in reversed(Base.metadata.sorted_tables):
            if table.name == "source_batch":
                continue
            if "data_batch_id" in table.c:
                session.execute(delete(table).where(table.c.data_batch_id == expected_batch))
        session.execute(
            update(SourceBatch)
            .where(SourceBatch.batch_id == expected_batch)
            .values(status="retired")
        )
        next_batch, next_generation = new_id("sim"), expected_generation + 1
        original = {
            column.name: getattr(batch, column.name) for column in SourceBatch.__table__.columns
        }
        original.update(
            batch_id=next_batch, generation=next_generation, status="active", created_at=now
        )
        session.execute(SourceBatch.__table__.insert().values(**original))

        def fresh(row):
            return {
                **row,
                "data_batch_id": next_batch,
                "batch_generation": next_generation,
                "version": 1,
                "created_at": now,
                "updated_at": now,
            }

        session.execute(Store.__table__.insert().values(**fresh(dict(store))))
        for row in subjects:
            row = fresh(row)
            row["auth_version"] += 1
            session.execute(Subject.__table__.insert().values(**row))
        for row in grants:
            row = fresh(row)  # 重置数据不恢复已撤销角色，也不延长授权有效期。
            session.execute(RoleGrant.__table__.insert().values(**row))
        for row in contacts:
            session.execute(ShippingInput.__table__.insert().values(**fresh(row)))
        session.flush()
        seed_accounts(session)
        seed_catalog(session)
        return {
            "retired_batch": expected_batch,
            "new_batch": next_batch,
            "generation": next_generation,
            "whole_financial_batch_reset": True,
            "balances_initialized_minor": "0",
            "old_sessions_preserved": False,
        }
