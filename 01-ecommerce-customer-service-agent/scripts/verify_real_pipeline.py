"""固定测试库：真实知识发布/pgvector/重排/原生Graph；不清理应用库。"""

import argparse
import hashlib
import json
import math
import statistics
import sys
import time
import unicodedata
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from cs_agent.config import PROJECT_ROOT, Settings
from cs_agent.conversation.identity import principal_for
from cs_agent.core.clock import Clock
from cs_agent.db.session import Database
from cs_agent.identity.service import authenticate
from cs_agent.knowledge.service import KnowledgeService
from cs_agent.main import create_app
from cs_agent.query.executor import SQLExecutor
from cs_agent.runtime.worker import AgentWorker
from cs_agent.runtime_profiles import apply_runtime_profile
from fastapi.testclient import TestClient
from sqlalchemy import text as sql_text


def expect(response, status):
    if response.status_code != status:
        raise RuntimeError(
            f"controlled HTTP stage failed: {response.status_code}, expected {status}"
        )
    return response.json()["data"]


def main(target, reset, publish_only=False):
    if target != "test" and reset:
        raise RuntimeError("application reset forbidden")
    _, values = apply_runtime_profile("debian")
    if reset:
        sys.path.insert(0, str(PROJECT_ROOT))
        from scripts.prepare_b2_verification import main as prepare

        prepare("debian")
    settings = Settings(
        database_url=values["CS_TEST_DATABASE_URL"]
        if target == "test"
        else values["CS_DATABASE_URL"],
        query_database_url=values["CS_TEST_QUERY_DATABASE_URL"]
        if target == "test"
        else values["CS_QUERY_DATABASE_URL"],
        expected_database="cs_agent_b0_test" if target == "test" else "cs_agent_b0",
        cookie_secure=False,
        expiration_worker_enabled=False,
        agent_worker_enabled=False,
    )
    dataset_path = PROJECT_ROOT / "tests/evaluation/rag/dataset.json"
    dataset = json.loads(dataset_path.read_text(encoding="utf8"))
    accounts = json.loads(
        (PROJECT_ROOT / ".runtime/demo-accounts.json").read_text(encoding="utf8")
    )
    app = create_app(settings)
    report = {
        "target": target,
        "checked_at": datetime.now(UTC).isoformat(),
        "model": "deepseek-flash",
        "dataset_sha256": hashlib.sha256(dataset_path.read_bytes()).hexdigest(),
        "corpus_sources": len(dataset["sources"]),
        "scope": "existing synthetic seven-source regression; not full-scale or unseen holdout",
        "published": [],
        "retrieval_cases": [],
        "agent_cases": [],
    }
    origin = {"Origin": "http://127.0.0.1:5173"}
    with TestClient(app) as client:
        assert app.state.db is not None

        def login(name):
            client.cookies.clear()
            data = expect(
                client.post(
                    "/api/v1/session",
                    headers=origin,
                    json={"login_name": name, "password": accounts[name]},
                ),
                200,
            )
            return {**origin, "X-CSRF-Token": data["csrf_token"]}

        headers = login("knowledge_admin")
        existing = expect(client.get("/api/v1/knowledge"), 200)
        title_map = {item["title"]: item for item in existing["units"]}
        revision_map = {}
        for source in dataset["sources"]:
            title = source["title"]
            if title in title_map:
                unit = title_map[title]
                matches = [
                    r
                    for r in existing["revisions"]
                    if r["unit_id"] == unit["unit_id"] and r["status"] == "published"
                ]
                if not matches:
                    raise RuntimeError(
                        "existing baseline requires review; preserving draft/history"
                    )
                ref = matches[0]["revision_id"]
                detail = expect(
                    client.get(f"/api/v1/knowledge/admin/knowledge/{ref}"), 200
                )
                if (
                    detail["body"]
                    != unicodedata.normalize("NFKC", source["body"]).strip()
                    or detail["source_ref"] != "PRD:7.4"
                    or detail["source_revision"] != "REQ-CS-v0.2"
                ):
                    raise RuntimeError(
                        "existing titled source differs; preserving operator content"
                    )
                revision_map[source["source_id"]] = ref
                continue
            created = expect(
                client.post(
                    "/api/v1/knowledge/units",
                    headers=headers,
                    json={
                        "title": title,
                        "topic": source["title"],
                        "body": source["body"],
                        "source_ref": "PRD:7.4",
                        "source_revision": "REQ-CS-v0.2",
                        "visibility": "public",
                        "valid_from": (
                            datetime.now(UTC) - timedelta(minutes=1)
                        ).isoformat(),
                    },
                ),
                201,
            )
            ref = created["revision_id"]
            expect(
                client.post(
                    f"/api/v1/knowledge/revisions/{ref}/review",
                    headers=headers,
                    json={},
                ),
                200,
            )
            expect(
                client.post(
                    f"/api/v1/knowledge/revisions/{ref}/publish",
                    headers=headers,
                    json={},
                ),
                200,
            )
            revision_map[source["source_id"]] = ref
            report["published"].append(source["source_id"])
        if target == "test" and not publish_only:
            client.cookies.clear()
            guest = expect(client.post("/api/v1/guest-session", headers=origin), 201)
            headers = {**origin, "X-CSRF-Token": guest["csrf_token"]}
            request = SimpleNamespace(
                app=app,
                cookies=dict(client.cookies),
                headers={k.lower(): v for k, v in headers.items()},
                state=SimpleNamespace(
                    request_id="real-qwen-evaluation", ctx=None, audit_identity=None
                ),
            )
            with app.state.db.transaction() as session:
                principal = principal_for(session, request)
            for case in dataset["answerable"]:
                started = time.perf_counter()
                result = KnowledgeService().retrieve(
                    app.state.db,
                    principal,
                    case["question"],
                    app.state.model_gateway,
                    app.state.clock.now(),
                )
                ids = [r["citation"]["reference_id"] for r in result["items"]]
                expected = revision_map[case["gold_source_id"]]
                report["retrieval_cases"].append(
                    {
                        "id": case["id"],
                        "gold_rank": ids.index(expected) + 1
                        if expected in ids
                        else None,
                        "degraded": result["degraded"],
                        "reranked": result["reranked"],
                        "retrieval": result["retrieval"],
                        "seconds": round(time.perf_counter() - started, 4),
                    }
                )
                if len(report["retrieval_cases"]) % 10 == 0:
                    print(
                        f"real PG/Qwen retrieval {len(report['retrieval_cases'])}/70",
                        flush=True,
                    )
            worker = AgentWorker(app.state.db, settings, app.state.clock)
            try:
                for label, text, customer in [
                    ("policy_guest", "没有发货时，订单收货地址允许改几次？", False),
                    ("private_guest", "查询我的订单和账户余额", False),
                    ("wallet_customer", "请查询我当前的账户余额", True),
                ]:
                    if customer:
                        headers = login("customer_a")
                    conversation = expect(
                        client.post("/api/v1/conversations", headers=headers, json={}),
                        201,
                    )["conversation_id"]
                    run = expect(
                        client.post(
                            f"/api/v1/conversations/{conversation}/messages",
                            headers=headers,
                            json={
                                "client_message_id": "real-model-" + label,
                                "text": text,
                            },
                        ),
                        202,
                    )["run_id"]
                    deadline = time.monotonic() + 60
                    while True:
                        worker.process_once()
                        status = expect(client.get("/api/v1/runs/" + run), 200)[
                            "status"
                        ]
                        if (
                            status not in {"queued", "running"}
                            or time.monotonic() > deadline
                        ):
                            break
                        time.sleep(0.2)
                    history = expect(
                        client.get(f"/api/v1/conversations/{conversation}/messages"),
                        200,
                    )["items"]
                    answer = next(
                        (
                            r["text"]
                            for r in reversed(history)
                            if r["source_role"] == "assistant"
                        ),
                        "",
                    )
                    report["agent_cases"].append(
                        {
                            "id": label,
                            "run_status": status,
                            "assistant_messages": sum(
                                r["source_role"] == "assistant" for r in history
                            ),
                            "login_guidance": "登录" in answer
                            if label == "private_guest"
                            else None,
                        }
                    )
                # 仅在固定测试库用显式测试预算验证一次真实SQL候选执行；不打开应用成本开关。
                request = SimpleNamespace(
                    app=app,
                    cookies=dict(client.cookies),
                    headers={k.lower(): v for k, v in headers.items()},
                    state=SimpleNamespace(
                        request_id="real-sql-candidate", ctx=None, audit_identity=None
                    ),
                )
                with app.state.db.transaction() as session:
                    ctx, _, _, _ = authenticate(session, request)
                candidate = app.state.model_gateway.invoke(
                    "sql",
                    {
                        "question": "列出本人最近5个订单的order_id和trade_status，不需要时间参数",
                        "parameter_types": {},
                    },
                    remaining=8,
                )
                executor = SQLExecutor(
                    Settings(
                        query_database_url=values["CS_TEST_QUERY_DATABASE_URL"],
                        expected_database="cs_agent_b0_test",
                        query_plan_cost_limit=1000,
                    )
                )
                try:
                    if candidate["kind"] != "query" or candidate["parameter_names"]:
                        raise RuntimeError(
                            "fixed SQL smoke did not produce the expected parameterless candidate"
                        )
                    result = executor.query(
                        ctx,
                        candidate["sql"],
                        {},
                        app.state.clock.now(),
                        schema_revision=candidate["schema_revision"],
                        schema_digest=candidate["schema_digest"],
                    )
                    report["sql_roundtrip"] = {
                        "source": result["source"],
                        "row_count": len(result["rows"]),
                        "plan_cost": result["plan_cost"],
                        "test_budget": 1000,
                        "application_cost_gate_changed": False,
                    }
                finally:
                    executor.close()
            finally:
                worker.close()
    if report["retrieval_cases"]:
        cases = report["retrieval_cases"]
        report["metrics"] = {
            "recall_at_5": statistics.mean(c["gold_rank"] is not None for c in cases),
            "top1": statistics.mean(c["gold_rank"] == 1 for c in cases),
            "mrr": statistics.mean(
                1 / c["gold_rank"] if c["gold_rank"] else 0 for c in cases
            ),
            "degraded_count": sum(c["degraded"] for c in cases),
            "reranked_count": sum(c["reranked"] for c in cases),
            "p95_seconds": sorted(c["seconds"] for c in cases)[
                math.ceil(0.95 * len(cases)) - 1
            ],
        }
    suffix = "-publish" if publish_only else ""
    output = PROJECT_ROOT / f".runtime/real-pipeline-{target}{suffix}.json"
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf8"
    )
    print(
        json.dumps(
            {k: v for k, v in report.items() if k != "retrieval_cases"},
            ensure_ascii=False,
        )
    )


def resume_agents():
    _, values = apply_runtime_profile("debian")
    settings = Settings(
        database_url=values["CS_TEST_DATABASE_URL"],
        query_database_url=values["CS_TEST_QUERY_DATABASE_URL"],
        expected_database="cs_agent_b0_test",
    )
    db = Database(settings)
    worker = AgentWorker(db, settings, Clock())
    output = PROJECT_ROOT / ".runtime/real-pipeline-test.json"
    report = json.loads(output.read_text(encoding="utf8"))
    backup = output.with_name("real-pipeline-test-before-recovery.json")
    if not backup.exists():
        backup.write_bytes(output.read_bytes())
    try:
        for case in report["agent_cases"]:
            with db.transaction() as session:
                row = (
                    session.execute(
                        sql_text(
                            "SELECT r.run_id,r.status,r.conversation_id FROM agent_run r JOIN conversation_message m ON m.message_id=r.message_id WHERE m.client_message_id=:key ORDER BY r.created_at DESC LIMIT 1"
                        ),
                        {"key": "real-model-" + case["id"]},
                    )
                    .mappings()
                    .one()
                )
            run_id = row["run_id"]
            status = row["status"]
            deadline = time.monotonic() + 60
            while status in {"queued", "running"} and time.monotonic() < deadline:
                worker.process_once()
                with db.transaction() as session:
                    status = session.scalar(
                        sql_text("SELECT status FROM agent_run WHERE run_id=:id"),
                        {"id": run_id},
                    )
                if status in {"queued", "running"}:
                    time.sleep(0.2)
            with db.transaction() as session:
                texts = (
                    session.execute(
                        sql_text(
                            "SELECT text FROM conversation_message WHERE run_id=:id AND source_role='assistant' ORDER BY message_seq"
                        ),
                        {"id": run_id},
                    )
                    .scalars()
                    .all()
                )
                attempts = session.scalar(
                    sql_text("SELECT attempts FROM agent_task WHERE run_id=:id"),
                    {"id": run_id},
                )
            case.update(
                run_status=status, assistant_messages=len(texts), attempts=attempts
            )
            if case["id"] == "private_guest":
                case["login_guidance"] = bool(texts and "登录" in texts[-1])
        report["agent_recovery_checked_at"] = datetime.now(UTC).isoformat()
        output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf8"
        )
        print(json.dumps({"agent_cases": report["agent_cases"]}, ensure_ascii=False))
        if any(
            c["run_status"] not in {"completed", "waiting"}
            or c["assistant_messages"] != 1
            for c in report["agent_cases"]
        ):
            raise RuntimeError("real Agent run not completed or safely waiting")
    finally:
        worker.close()
        db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", choices=["test", "application"], default="test")
    parser.add_argument("--reset-test", action="store_true")
    parser.add_argument("--resume-agents", action="store_true")
    parser.add_argument("--publish-only", action="store_true")
    args = parser.parse_args()
    if args.resume_agents:
        if args.target != "test" or args.reset_test:
            raise RuntimeError("resume only fixed existing test runs")
        resume_agents()
    else:
        main(args.target, args.reset_test, args.publish_only)
