"""真实PG锁边界：业务准备走HTTP，数据库仅取锁/读取，过期调用正式业务函数。"""
import argparse
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

from cs_agent.commerce.expiration import expire_due
from cs_agent.db.business_tables import orders, products, skus
from joint_http import OUT, RUN, HttpFailure, JointClient, advance, now, wait_for
from joint_recovery import EvidenceDB, control
from sqlalchemy import select, text
from sqlalchemy.orm import Session


def confirm(admin, draft):
    plan = admin.ops("POST", "/plans", draft)
    ref = plan["plan_id"]
    admin.ops("POST", f"/plans/{ref}/evaluate", {"expected_version": 1})
    admin.ops("POST", f"/plans/{ref}/execution-preview")
    plan = admin.ops("POST", f"/plans/{ref}/confirm", {"expected_version": 1, "reason": "合成PG锁边界"})
    approval = plan["approval"]
    return {"plan_id": ref, "plan_version": 1, "approval_ref": approval["approval_ref"],
            "manifest_digest": approval["manifest_digest"], "preview_ref": approval["preview"]["preview_ref"]}


def run(report, clients, evidence):
    admin, buyer = JointClient("shop_admin"), JointClient("customer_a")
    clients.extend([admin, buyer])
    product = admin.create_product("publish-lock-end")
    source = admin.ops("POST", "/snapshots/pull")
    initial = now()
    draft = admin.draft(source, product["sku_id"], quantity_cap=4, start=initial + timedelta(seconds=1), label="publish-lock-end")
    draft["end_at"] = (initial + timedelta(seconds=2)).isoformat()
    draft["tasks"][-1]["due_at"] = draft["end_at"]
    for resource in draft["resources"]:
        resource.update(volume_per_unit="0", shift_hours="0.0001")
    payload = confirm(admin, draft)
    with evidence.engine.connect() as blocker:
        pid = blocker.scalar(select(skus.c.product_id).where(skus.c.sku_id == product["sku_id"]))
        blocker.execute(select(products.c.product_id).where(products.c.product_id == pid).with_for_update())
        with ThreadPoolExecutor(max_workers=1) as pool:
            def publish():
                try:
                    return {"status": 200, "body": admin.cs("POST", "/shop/activities", payload)}
                except HttpFailure as exc:
                    return {"status": exc.status, "code": exc.code}
            future = pool.submit(publish)
            # 单独只读连接观测真实PG等待；不以sleep猜测已经阻塞。
            from sqlalchemy import create_engine
            observer = create_engine(evidence.engine.url, hide_parameters=True,
                                     connect_args={"connect_timeout": 3, "options": "-c statement_timeout=1000"})
            try:
                def waiting():
                    with observer.connect() as connection:
                        return connection.scalar(text("SELECT count(*) FROM pg_locks WHERE NOT granted"))
                count = wait_for(waiting, lambda n: n > 0, seconds=1.5)
                advance(initial + timedelta(seconds=3))
                blocker.rollback()
                result = future.result(timeout=10)
            finally:
                blocker.rollback()
                observer.dispose()
    assert result == {"status": 409, "code": "activity_window_expired"}, result
    assert not any(row["plan_id"] == payload["plan_id"] for row in admin.cs("GET", "/shop/activities")["items"])
    report["publish_wait_crosses_end"] = {"plan_id": payload["plan_id"], "observed_pg_waiters": count,
                                           "result": result, "activities": 0}
    # 关闭未提交slot，保留同一意图的持久终态。
    admin.ops("POST", f"/plans/{payload['plan_id']}/cancel-approval", {"expected_version": 1, "reason": "临界时间拒绝后关闭原意图"})
    # 两个活动各含两种SKU，使两过期worker的SKU集合不交叉、活动集合交叉。
    groups = []
    for index in range(2):
        pair = [admin.create_product(f"expiry-{index}-{j}") for j in range(2)]
        source = admin.ops("POST", "/snapshots/pull")
        draft = admin.draft(source, pair[0]["sku_id"], quantity_cap=4, label=f"expiry-{index}")
        draft["lines"].append({**draft["lines"][0], "sku_id": pair[1]["sku_id"]})
        payload = confirm(admin, draft)
        receipt = admin.cs("POST", "/shop/activities", payload)
        groups.append({"products": pair, "receipt": receipt, "draft": draft})
    advance(datetime.fromisoformat(groups[-1]["draft"]["start_at"]) + timedelta(seconds=1))
    # 获取两个活动中ID最小/最大的订单组合，组成 A→B 与 B→A。额外订单正常取消。
    candidates = []
    for group in groups:
        for product in group["products"]:
            for _ in range(3):
                ref = buyer.order(buyer.quote(product["sku_id"]))
                candidates.append({"order_id": ref, "sku_id": product["sku_id"],
                                   "activity_id": group["receipt"]["activity_id"]})
    # 从每SKU三个随机ID中穷举有限组合，确保两个批次相反的活动顺序。
    import itertools
    buckets = [[r for r in candidates if r["sku_id"] == p["sku_id"]] for g in groups for p in g["products"]]
    chosen = next((rows for rows in itertools.product(*buckets)
                   if (rows[0]["order_id"] < rows[2]["order_id"]) !=
                      (rows[1]["order_id"] < rows[3]["order_id"])), None)
    if chosen is None:
        raise AssertionError("random order IDs did not produce opposite batch ordering")
    keep = {row["order_id"] for row in chosen}
    for row in candidates:
        if row["order_id"] not in keep:
            buyer.action("order.cancel", row["order_id"], buyer.detail(row["order_id"])["version"])
    control("stop", "cs")
    try:
        advance(timedelta(minutes=16))
        gate = threading.Barrier(2)
        def worker(rows):
            with Session(evidence.engine) as session, session.begin():
                refs = [row["order_id"] for row in rows]
                session.execute(select(orders.c.order_id).where(orders.c.order_id.in_(refs)).order_by(orders.c.order_id).with_for_update()).all()
                gate.wait(timeout=5)
                count = expire_due(session, now(), limit=2)
                assert count == 2
                return {"ordered_batch": sorted(rows, key=lambda row: row["order_id"]), "expired": count}
        # 独立连接两条，避免EvidenceDB的pool_size=1使barrier串行自等。
        with evidence.engine.connect() as connection:
            url = connection.engine.url
        from sqlalchemy import create_engine
        parallel = create_engine(url, pool_size=2, max_overflow=0, hide_parameters=True,
                                 connect_args={"connect_timeout": 3, "options": "-c statement_timeout=2000"})
        original = evidence.engine
        evidence.engine = parallel
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(worker, [(chosen[0], chosen[2]), (chosen[1], chosen[3])]))
        finally:
            evidence.engine = original
            parallel.dispose()
        report["opposite_multi_activity_expiration"] = results
    finally:
        control("start", "cs")
    for row in chosen:
        assert buyer.detail(row["order_id"])["trade_status"] == "cancelled_unpaid"
    report["activity_after_expiration"] = [admin.activity(g["receipt"]["activity_id"]) for g in groups]
    assert all(a["reserved_discount_minor"] == "0" for a in report["activity_after_expiration"])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true")
    if not parser.parse_args().execute:
        parser.error("requires exclusive synthetic integration window and --execute")
    clients, evidence = [], EvidenceDB()
    target = OUT / ("boundaries-attempt-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ") + ".json")
    report = {"integration_run_id": RUN, "status": "running", "started_at": datetime.now(UTC).isoformat()}
    try:
        run(report, clients, evidence)
        report["status"] = "completed"
    except Exception as exc:
        report.update(status="failed", error_type=type(exc).__name__)
        raise
    finally:
        report["request_chains"] = [{"actor": c.actor, "calls": c.calls} for c in clients]
        report["finished_at"] = datetime.now(UTC).isoformat()
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        for client in clients:
            client.close()
        evidence.engine.dispose()
        print(json.dumps({"status": report["status"], "evidence": str(target)}))


if __name__ == "__main__":
    main()
