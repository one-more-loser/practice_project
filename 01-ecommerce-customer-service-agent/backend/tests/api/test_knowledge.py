"""真实隔离PG与HTTP；Embedding/重排仅显式替身，不构成真实模型质量验收。"""

from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select, update

from cs_agent.config import PROJECT_ROOT
from cs_agent.conversation.identity import Principal
from cs_agent.core.context import Role, TrustedContext
from cs_agent.db.knowledge_tables import faqs, revisions, units
from cs_agent.knowledge.service import KnowledgeService, seed_faq_drafts


class TestEmbedding:
    __test__ = False
    test_double = True

    def __init__(self):
        self.calls = []

    def invoke(self, capability, payload, remaining=20):
        self.calls.append((capability, payload))
        if capability == "embedding":
            return {
                "model": "fixture-embedding",
                "service_revision": "fixture-v1",
                "vectors": [[0.01] * 1024 for _ in payload["texts"]],
            }
        return {
            "results": [
                {"id": item["id"], "score": 1, "score_type": "provider_logit"}
                for item in payload["candidates"]
            ]
        }


def principal(clock, *, kind="guest"):
    return Principal("test-guest", kind, "store_sim", "sim_b0_20261022", 1, "test-guest", 1)


def admin():
    return TrustedContext(
        "knowledge_admin",
        "store_sim",
        Role.KNOWLEDGE_ADMIN,
        "test",
        1,
        "sim_b0_20261022",
        1,
        "req_test_knowledge",
    )


def draft(client, headers, clock, **changes):
    data = {
        "title": "模拟退货政策",
        "topic": "退货退款",
        "body": "普通退货在本人确认收货后七天内申请，审核通过并核验收到退货后退款。",
        "source_ref": "SIM-POLICY",
        "source_revision": "v1",
        "visibility": "public",
        "valid_from": (clock.now() - timedelta(days=1)).isoformat(),
        "valid_until": None,
        **changes,
    }
    response = client.post("/api/v1/knowledge/units", headers=headers, json=data)
    assert response.status_code == 201, response.text
    return response.json()["data"]


def publish_test(db, app, headers, ref):
    # 使用真实鉴权/CSRF构造请求；显式test_index只准固定测试库，API不提供此开关。
    request = SimpleNamespace(
        app=app,
        cookies={"cs_session": app._test_client.cookies.get("cs_session")},
        headers={k.lower(): v for k, v in headers.items()},
        state=SimpleNamespace(request_id="req_test_publish", ctx=None, audit_identity=None),
    )
    return KnowledgeService(test_index=True).publish_revision(
        db, request, ref, TestEmbedding(), app.state.clock.now()
    )


def test_faq_drafts_review_publish_normalized_match_and_retire(environment, login_as):
    client, _, clock, *_ = environment
    db = client.app.state.db
    assert (
        seed_faq_drafts(db, PROJECT_ROOT / "data/synthetic/faq-sim-v0.2.json", clock.now())[
            "inserted"
        ]
        == 14
    )
    headers = login_as("knowledge_admin")
    assert len(client.get("/api/v1/knowledge").json()["data"]["faqs"]) == 14
    service = KnowledgeService()
    fixture = __import__("json").loads(
        (PROJECT_ROOT / "data/synthetic/faq-sim-v0.2.json").read_text(encoding="utf-8")
    )
    for item in fixture["items"]:
        ref = item["faq_id"]
        assert (
            client.post(
                f"/api/v1/knowledge/faqs/{ref}/publish", headers=headers, json={}
            ).status_code
            == 409
        )
        assert (
            client.post(
                f"/api/v1/knowledge/faqs/{ref}/review", headers=headers, json={}
            ).status_code
            == 200
        )
        assert (
            client.post(
                f"/api/v1/knowledge/faqs/{ref}/publish", headers=headers, json={}
            ).status_code
            == 200
        )
        with db.transaction() as session:
            for query in [item["question"], *item["variants"]]:
                found = service._faq_match(
                    session, principal(clock), query, ["public"], clock.now()
                )
                assert found["items"][0]["citation"]["faq_id"] == ref
    # 十二治理负例均不得复用稳定FAQ；后续是否RAG/澄清/业务查询分别验收。
    negatives = __import__("json").loads(
        (PROJECT_ROOT / "tests/evaluation/faq/faq_governance_negatives.json").read_text(
            encoding="utf-8"
        )
    )
    with db.transaction() as session:
        for case in negatives:
            assert (
                service._faq_match(
                    session, principal(clock), case["query"], ["public"], clock.now()
                )
                is None
            )
    assert (
        client.post(
            "/api/v1/knowledge/faqs/FAQ-SIM-001/retire", headers=headers, json={}
        ).status_code
        == 200
    )
    with db.transaction() as session:
        assert (
            service._faq_match(
                session, principal(clock), fixture["items"][0]["question"], ["public"], clock.now()
            )
            is None
        )


def test_knowledge_scope_expiry_and_test_index_isolation(environment, login_as):
    client, _, clock, *_ = environment
    client.app._test_client = client
    headers = login_as("knowledge_admin")
    created = draft(client, headers, clock)
    ref = created["revision_id"]
    assert (
        client.post(
            f"/api/v1/knowledge/revisions/{ref}/review", headers=headers, json={}
        ).status_code
        == 200
    )
    # 在线API不会拿假向量替代真实Embedding。
    unavailable = client.post(
        f"/api/v1/knowledge/revisions/{ref}/publish", headers=headers, json={}
    )
    assert (
        unavailable.status_code == 503
        and unavailable.json()["error"]["code"] == "model_unconfigured"
    )
    publish_test(client.app.state.db, client.app, headers, ref)
    gateway = TestEmbedding()
    service = KnowledgeService(test_index=True)
    result = service.retrieve(
        client.app.state.db, principal(clock), "普通退货政策", gateway, clock.now(), clock=clock
    )
    assert result["items"] and result["reranked"]
    online = KnowledgeService().retrieve(
        client.app.state.db, principal(clock), "普通退货政策", gateway, clock.now()
    )
    assert online["items"] == []
    with client.app.state.db.transaction() as session:
        session.execute(
            update(revisions).where(revisions.c.revision_id == ref).values(valid_until=clock.now())
        )
    assert (
        service.retrieve(
            client.app.state.db, principal(clock), "普通退货政策", gateway, clock.now()
        )["items"]
        == []
    )
    assert (
        client.post(
            f"/api/v1/knowledge/units/{created['unit_id']}/retire", headers=headers, json={}
        ).status_code
        == 200
    )
    login_as("customer_a")
    assert client.get("/api/v1/knowledge").status_code == 403
    assert client.get(f"/api/v1/knowledge/references/knowledge/{ref}").status_code == 404


def test_knowledge_new_version_requires_new_review_and_bound_faq_expires(environment, login_as):
    client, _, clock, *_ = environment
    client.app._test_client = client
    headers = login_as("knowledge_admin")
    created = draft(client, headers, clock)
    ref = created["revision_id"]
    client.post(f"/api/v1/knowledge/revisions/{ref}/review", headers=headers, json={})
    publish_test(client.app.state.db, client.app, headers, ref)
    body = {
        "question": "模拟商品普通退货规则？",
        "answer": "七天内申请。",
        "variants": [],
        "source_ref": "SIM-POLICY",
        "source_revision": "v1",
        "knowledge_revision_id": ref,
        "topic": "退货",
        "visibility": "public",
        "valid_from": clock.now().isoformat(),
    }
    faq = client.post("/api/v1/knowledge/faqs", headers=headers, json=body)
    assert faq.status_code == 201, faq.text
    faq_id = faq.json()["data"]["faq_id"]
    client.post(f"/api/v1/knowledge/faqs/{faq_id}/review", headers=headers, json={})
    assert (
        client.post(
            f"/api/v1/knowledge/faqs/{faq_id}/publish", headers=headers, json={}
        ).status_code
        == 200
    )
    new = client.post(
        f"/api/v1/knowledge/units/{created['unit_id']}/revisions",
        headers=headers,
        json={
            "body": "普通退货规则已更新，请核对适用版本。",
            "source_ref": "SIM-POLICY",
            "source_revision": "v2",
            "valid_from": clock.now().isoformat(),
        },
    )
    assert new.status_code == 201
    new_ref = new.json()["data"]["revision_id"]
    assert (
        client.post(
            f"/api/v1/knowledge/revisions/{new_ref}/publish", headers=headers, json={}
        ).status_code
        == 409
    )
    client.post(f"/api/v1/knowledge/revisions/{new_ref}/review", headers=headers, json={})
    publish_test(client.app.state.db, client.app, headers, new_ref)
    with client.app.state.db.transaction() as session:
        assert session.scalar(select(faqs.c.status).where(faqs.c.faq_id == faq_id)) == "retired"
        assert (
            session.scalar(
                select(units.c.active_revision).where(units.c.unit_id == created["unit_id"])
            )
            == 2
        )


def test_source_visibility_unknown_source_and_sensitive_drafts_are_rejected(environment, login_as):
    client, _, clock, *_ = environment
    headers = login_as("knowledge_admin")
    with pytest.raises(AssertionError):
        draft(client, headers, clock, body="收货地址是某街某楼")
    payload = {
        "question": "无依据问题",
        "answer": "不应发布",
        "variants": [],
        "source_ref": "unknown",
        "source_revision": "v1",
        "topic": "未知",
        "visibility": "public",
        "valid_from": clock.now().isoformat(),
    }
    created = client.post("/api/v1/knowledge/faqs", headers=headers, json=payload)
    assert created.status_code == 201
    ref = created.json()["data"]["faq_id"]
    client.post(f"/api/v1/knowledge/faqs/{ref}/review", headers=headers, json={})
    assert (
        client.post(f"/api/v1/knowledge/faqs/{ref}/publish", headers=headers, json={}).status_code
        == 409
    )
    login_as("support_a")
    assert client.get("/api/v1/knowledge").status_code == 403
