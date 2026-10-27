"""仅在独占固定test窗口准备合成联合样本；复用本run备份，不操作应用库。"""
import hashlib
import json
from datetime import UTC, datetime, timedelta

from alembic import command
from alembic.config import Config
from cs_agent.config import PROJECT_ROOT, Settings
from cs_agent.core.context import Role
from cs_agent.db.models import Subject
from cs_agent.db.seed import seed
from cs_agent.db.seed_business import seed_accounts, seed_catalog
from cs_agent.db.session import verify_target
from cs_agent.identity.operations_seed import seed_operations_capabilities
from cs_agent.runtime_profiles import apply_runtime_profile
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session


def main():
    import sys
    sys.path.insert(0, str(PROJECT_ROOT / "backend"))
    from tests.helpers import reset_test_data
    _, values = apply_runtime_profile("debian")
    run = PROJECT_ROOT / ".runtime/XOPS-20261023-01"
    backup = json.loads((run / "test-backup.json").read_text())
    assert hashlib.sha256((run / "test-before.dump").read_bytes()).hexdigest() == backup["sha256"]
    engine = create_engine(values["CS_TEST_MIGRATION_URL"], hide_parameters=True,
                           connect_args={"connect_timeout": 3, "options": "-c statement_timeout=10000"})
    verify_target(engine, Settings(expected_database="cs_agent_b0_test"), migration=True)
    configuration = Config(str(PROJECT_ROOT / "backend/alembic.ini"))
    configuration.attributes["database_url"] = values["CS_TEST_MIGRATION_URL"]
    command.upgrade(configuration, "head")
    reset_test_data(engine)
    seed(values["CS_TEST_MIGRATION_URL"])
    now = datetime.now(UTC)
    (run / "joint-clock.json").write_text(json.dumps({"now": now.isoformat()}), encoding="utf-8")
    with Session(engine) as session, session.begin():
        seed_accounts(session)
        seed_catalog(session)
        subject = session.get(Subject, "shop_admin")
        batch_id, generation = subject.data_batch_id, subject.batch_generation
        capabilities = ["metrics.read", "cost.read", "data.import", "plan.edit", "plan.confirm", "activity.publish", "activity.manage"]
        assignments = [("shop_admin", Role.SHOP_ADMIN, cap) for cap in capabilities]
        # 三种岗位按显式测试授权；普通员工/顾客不自动获得运营能力。
        assignments += [("supervisor", Role.SUPERVISOR, cap) for cap in ("metrics.read", "cost.read", "data.import", "plan.edit")]
        assignments += [("support_a", Role.SUPPORT, "metrics.read")]
        existing = {row for row in session.scalars(select(Subject.subject_id))}
        assignments = [row for row in assignments if row[0] in existing]
        seed_operations_capabilities(session, assignments=assignments, store_id="store_sim", data_batch_id=batch_id,
                                    batch_generation=generation, integration_run_id="XOPS-20261023-01",
                                    now=now, valid_to=now + timedelta(days=1))
    source = {"schema_version": "OPS-SYNTHETIC-INPUTS-v1", "source_kind": "synthetic",
              "inputs_version": "XOPS-20261023-01-gold-v1", "store_id": "store_sim",
              "data_batch_id": batch_id, "batch_generation": generation, "skus": {},
              "capacity_basis": "synthetic staffing gold-v1; 2 roles, bounded capacity assumptions"}
    (run / "synthetic-inputs.json").write_text(json.dumps(source, ensure_ascii=False, indent=2), encoding="utf-8")
    report = {"integration_run_id": "XOPS-20261023-01", "source_kind": "synthetic", "database": "cs_agent_b0_test",
              "data_batch_id": batch_id, "batch_generation": generation, "as_of": now.isoformat(),
              "assignments": [{"subject_id": actor, "role": role.value, "capability": cap} for actor, role, cap in assignments],
              "credentials_reference": ".runtime/demo-accounts.json (private; not copied)",
              "backup_reused": backup["sha256"], "application_database_reset": False}
    (PROJECT_ROOT / "docs/06-acceptance/XOPS-20261023-01/prepared-data.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    engine.dispose()


if __name__ == "__main__":
    main()
