"""调用方先通过固定test守卫；本模块不连接、不迁移、不reset数据库。"""
from sqlalchemy import select

from cs_agent.core.context import Role
from cs_agent.db.models import Subject
from cs_agent.db.operations_identity_tables import OperationsCapabilityGrant as Grant
from cs_agent.identity.operations import grant_capability, lock_operations_subject


def seed_operations_capabilities(session, *, assignments, store_id, data_batch_id,
                                batch_generation, integration_run_id, now, valid_to):
    """assignments显式列出(subject_id, Role, capability)，绝不默认全员/全能。

    只接受当前合成来源，已有同能力有效授权不重复写；已撤销/过期记录不能复活。
    """
    if not integration_run_id or valid_to <= now:
        raise ValueError("bounded integration seed required")
    results = []
    for subject_id, role, capability in assignments:
        role = Role(role)
        lock_operations_subject(session, subject_id)
        subject = session.get(Subject, subject_id, populate_existing=True)
        if (subject is None or subject.source_kind != "synthetic"
            or subject.store_id != store_id or subject.data_batch_id != data_batch_id
            or subject.batch_generation != batch_generation):
            raise ValueError("synthetic seed scope mismatch")
        existing = session.scalar(select(Grant).where(
            Grant.subject_id == subject_id, Grant.store_id == store_id,
            Grant.data_batch_id == data_batch_id, Grant.batch_generation == batch_generation,
            Grant.role == role.value, Grant.capability == capability,
            Grant.grant_status == "active", Grant.valid_from <= now,
            Grant.valid_to == valid_to,
        ))
        results.append(existing or grant_capability(
            session, subject_id=subject_id, store_id=store_id, role=role,
            capability=capability, now=now, valid_to=valid_to,
            actor_ref="seed:" + integration_run_id,
            reason="synthetic integration assignment " + integration_run_id,
        ))
    return results
