"""合成联调的真实HTTP竞争、丢响应、断连及进程恢复。

仅本机转发器注入传输故障，业务请求仍由真实双服务和PG处理。
不reset，不直接写业务表，所有服务停启核对登记PID身份。
"""

import argparse
import json
import socket
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import uuid4

import httpx
from cs_agent.config import Settings
from cs_agent.db.activity_tables import activities, deliveries, slots
from cs_agent.db.session import verify_target
from cs_agent.runtime_profiles import apply_runtime_profile
from joint_http import (
    OUT,
    PRIVATE,
    ROOT,
    RUN,
    HttpFailure,
    JointClient,
    advance,
    atomic_json,
    now,
    wait_for,
)
from sqlalchemy import create_engine, func, select


def control(action, service):
    result = subprocess.run([str(ROOT / "backend/.venv/Scripts/python.exe"),
                             str(ROOT / "scripts/joint_control.py"), action, "--only", service],
                            cwd=ROOT, check=True, capture_output=True, text=True, encoding="utf-8",
                            timeout=45, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return {"action": action, "service": service, "exit_code": result.returncode}


class FaultProxy:
    """转发真实请求并丢弃选定回包；不伪造grant/receipt/actual或鉴权。"""
    def __init__(self, port, upstream, drop_path):
        self.drop_path, self.upstream, self.events = drop_path, upstream, []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                self.forward()

            def do_POST(self):
                self.forward()

            def forward(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                headers = {key: value for key, value in self.headers.items()
                           if key.lower() not in {"host", "connection", "content-length"}}
                with httpx.Client(timeout=10, trust_env=False) as client:
                    response = client.request(self.command, owner.upstream + self.path,
                                              headers=headers, content=body)
                if self.path == owner.drop_path:
                    owner.events.append({"path": self.path, "upstream_status": response.status_code,
                                         "upstream_body": response.json()})
                    self.close_connection = True
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.connection.close()
                    return
                self.send_response(response.status_code)
                for key, value in response.headers.items():
                    if key.lower() not in {"transfer-encoding", "connection", "content-encoding", "content-length"}:
                        self.send_header(key, value)
                self.send_header("Content-Length", str(len(response.content)))
                self.end_headers()
                self.wfile.write(response.content)

        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)


class EvidenceDB:
    def __init__(self):
        _, values = apply_runtime_profile("debian")
        self.engine = create_engine(values["CS_TEST_MIGRATION_URL"], hide_parameters=True,
                                    pool_size=1, max_overflow=0,
                                    connect_args={"connect_timeout": 3, "options": "-c statement_timeout=2000"})
        verify_target(self.engine, Settings(expected_database="cs_agent_b0_test"), migration=True)

    def slot(self, intent):
        with self.engine.connect() as connection:
            return dict(connection.execute(select(slots).where(slots.c.intent_id == intent)).mappings().one())

    def delivery(self, activity):
        with self.engine.connect() as connection:
            return [dict(row) for row in connection.execute(select(
                deliveries.c.event_id, deliveries.c.kind, deliveries.c.status, deliveries.c.attempts,
                deliveries.c.available_at, deliveries.c.last_code, deliveries.c.sequence
            ).where(deliveries.c.activity_id == activity).order_by(deliveries.c.sequence)).mappings()]

    def activity_count(self, intent):
        with self.engine.connect() as connection:
            return connection.scalar(select(func.count()).select_from(activities).where(activities.c.intent_id == intent))


def service_request(service, method, path, body=None, expected=None):
    token = json.loads((PRIVATE / "joint-config.json").read_text(encoding="utf-8"))["service_token"]
    port, prefix = (8000, "/api/v1") if service == "cs" else (8120, "/api/ops/v1")
    with httpx.Client(timeout=5, trust_env=False) as client:
        response = client.request(method, f"http://127.0.0.1:{port}" + prefix + path,
                                  json=body, headers={"Authorization": "Bearer " + token})
    if expected is not None:
        assert response.status_code == expected, (response.status_code, response.json().get("error"))
        return response.json()
    response.raise_for_status()
    return response.json()["data"]


def formal_refund(admin, buyer, order, clients):
    supervisor, support = JointClient("supervisor"), JointClient("support_a")
    clients.extend([supervisor, support])
    paid = buyer.detail(order)
    admin.cs("POST", f"/shop/orders/{order}/shipments", {
        "quantities": {line["order_item_id"]: line["ordered_qty"] for line in paid["items"]},
        "expected_version": paid["version"]})
    buyer.action("order.receipt.confirm", order, buyer.detail(order)["version"])
    current = buyer.detail(order)
    claim = buyer.action("aftersale.apply", order, current["version"], {
        "quantities": {current["items"][0]["order_item_id"]: 1}, "reason": "ordinary",
        "description": "活动结束后正式部分退货"})
    ref = claim["claim_id"]
    ticket = supervisor.cs("GET", "/staff/tickets/" + claim["ticket_id"])
    supervisor.cs("POST", f"/staff/tickets/{claim['ticket_id']}/assignments", {
        "staff_id": "support_a", "reason": "合成迟到退款分派", "expected_version": ticket["version"]})
    support.cs("POST", f"/aftersales/{ref}/reviews", {"expected_version": 1,
        "decision": "approve", "basis": "核对活动结束前成交事实", "evidence_refs": ["policy:REQ-CS-v0.2"]})
    buyer.cs("POST", f"/aftersales/{ref}/return-shipment", {"expected_version": 2,
        "carrier": "合成退货快递", "tracking_no": "XOPS-LATE-" + uuid4().hex[:8]})
    result = admin.cs("POST", f"/refund-claims/{ref}/inspections", {"expected_version": 3,
        "lines": {current["items"][0]["order_item_id"]: {"received_qty": 1, "saleable_qty": 1}},
        "evidence_refs": ["inspection:XOPS-late-refund"]})
    assert result["goods_minor"] == "9200"
    return result


def run(report, clients, evidence):
    admin, other_admin = JointClient("shop_admin"), JointClient("shop_admin")
    buyers = [JointClient(actor) for actor in ("customer_a", "customer_b", "customer_a", "customer_b")]
    clients.extend([admin, other_admin, *buyers])
    # 提交后客户端丢回包。代理上游已收到真实committed，重试只恢复原slot。
    p = admin.create_product("lost-publish")
    confirmed = admin.confirm_plan(p["sku_id"], quantity_cap=4, label="lost-publish")
    with FaultProxy(18100, "http://127.0.0.1:8000", "/api/v1/shop/activities") as proxy:
        admin.cs_port = 18100
        try:
            admin.cs("POST", "/shop/activities", confirmed["publish_payload"])
        except httpx.HTTPError as exc:
            report["lost_response_error"] = type(exc).__name__
        else:
            raise AssertionError("expected dropped response")
        finally:
            admin.cs_port = 8000
        committed = other_admin.cs("POST", "/shop/activities", confirmed["publish_payload"])
        assert committed == proxy.events[0]["upstream_body"]["data"] and committed["status"] == "committed"
        assert evidence.activity_count(committed["intent_id"]) == 1
        report["lost_commit_response"] = {"upstream": proxy.events, "retry": committed, "activity_count": 1}
    # 真实grant已经在02提交，回包丢失：01必须unknown，审批冻结；取消/到期闭合原slot。
    config_path = PRIVATE / "joint-config.json"
    configuration = json.loads(config_path.read_text(encoding="utf-8"))
    with FaultProxy(18120, "http://127.0.0.1:8120", "/api/ops/v1/internal/execution-grants") as proxy:
        try:
            atomic_json(config_path, {**configuration, "ops_base_url": "http://127.0.0.1:18120"})
            control("stop", "cs")
            control("start", "cs")
            report["unknown_barriers"] = []
            for mode in ("cancel", "expiry"):
                product = admin.create_product("unknown-" + mode)
                flow = admin.confirm_plan(product["sku_id"], quantity_cap=4, label="unknown-" + mode)
                unknown = admin.cs("POST", "/shop/activities", flow["publish_payload"])
                assert unknown["status"] == "unknown"
                plan_id = flow["plan"]["plan_id"]
                assert admin.ops("GET", "/plans/" + plan_id)["status"] == "publishing"
                current = evidence.slot(unknown["intent_id"])
                assert current["status"] == "pending" and evidence.activity_count(current["intent_id"]) == 0
                if mode == "cancel":
                    result = admin.ops("POST", f"/plans/{plan_id}/cancel-approval", {
                        "expected_version": 1, "reason": "未知结果只通过原slot持久取消屏障关闭"})
                else:
                    advance(current["grant_expires_at"] + timedelta(seconds=1))
                    result = service_request("ops", "POST", f"/internal/executions/{current['intent_id']}/reconcile",
                                             {"store_id": "store_sim", "finalize": True})
                closed = admin.cs("POST", "/shop/activities", flow["publish_payload"])
                assert closed["status"] == "closed_no_effect"
                assert evidence.activity_count(current["intent_id"]) == 0
                assert evidence.slot(current["intent_id"])["status"] == "closed_no_effect"
                report["unknown_barriers"].append({"mode": mode, "unknown": unknown,
                    "close_result": result, "retry": closed, "activity_count": 0})
            report["grant_drop_events"] = proxy.events
        finally:
            atomic_json(config_path, configuration)
            control("stop", "cs")
            control("start", "cs")
    # 真实并发建单争抢数量上限4：四个独立登录会话均买3件，只能一单成功。
    product = admin.create_product("concurrency-outbox")
    flow = admin.plan_publish(product["sku_id"], quantity_cap=4, label="concurrency-outbox")
    ref, plan_id = flow["receipt"]["activity_id"], flow["plan"]["plan_id"]
    advance(datetime.fromisoformat(flow["plan"]["draft"]["start_at"]) + timedelta(seconds=1))
    quotes = [buyer.quote(product["sku_id"], 3) for buyer in buyers]
    gate = threading.Barrier(4)

    def compete(pair):
        buyer, quote = pair
        gate.wait(timeout=5)
        try:
            return {"status": 200, "order_id": buyer.order(quote), "actor": buyer.actor}
        except HttpFailure as exc:
            return {"status": exc.status, "code": exc.code, "actor": buyer.actor}

    with ThreadPoolExecutor(max_workers=4) as pool:
        race = list(pool.map(compete, zip(buyers, quotes, strict=True)))
    winners = [index for index, row in enumerate(race) if row["status"] == 200]
    assert len(winners) == 1 and all(row["status"] in {200, 409} for row in race)
    assert admin.activity(ref)["reserved_discount_minor"] == "2400"
    winner, order = buyers[winners[0]], race[winners[0]]["order_id"]
    winner.action("order.cancel", order, winner.detail(order)["version"])
    assert admin.activity(ref)["reserved_discount_minor"] == "0"
    report["concurrent_quantity"] = {"limit": 4, "requests": 4, "qty_per_request": 3,
                                     "results": race, "after_release": admin.activity(ref)}
    # 付款与取消竞争使用相同版本、不同确认，只有一个事务成功。
    buyer = buyers[0]
    buyer.cs("POST", "/me/topups", {"currency": "CNY", "amount_minor": "100000"})
    order = buyer.order(buyer.quote(product["sku_id"]))
    confirmations = [client.cs("POST", "/action-proposals", {
        "action": action, "resource_ref": order, "expected_version": 1, "parameters": {}})
        for client, action in [(buyers[0], "order.pay"), (buyers[2], "order.cancel")]]
    race_gate = threading.Barrier(2)

    def submit(pair):
        client, proposal = pair
        race_gate.wait(timeout=5)
        try:
            return {"status": 200, "result": client.cs("POST", f"/confirmations/{proposal['confirmation_id']}/submit", {"decision": "confirm"})}
        except HttpFailure as exc:
            return {"status": exc.status, "code": exc.code}

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(submit, zip([buyers[0], buyers[2]], confirmations, strict=True)))
    assert sorted(row["status"] for row in results) == [200, 409]
    report["pay_cancel_race"] = {"results": results, "order": buyer.detail(order)}
    # 已发布活动在02停止时仍能报价/建单/付款；业务事实留在持久outbox。
    wait_for(lambda: evidence.delivery(ref), lambda rows: bool(rows) and all(row["status"] == "acked" for row in rows))
    report["service_actions"] = [control("stop", "ops")]
    try:
        q = buyer.quote(product["sku_id"], 2)
        outage_order = buyer.order(q)
        buyer.action("order.pay", outage_order, buyer.detail(outage_order)["version"])
        assert buyer.detail(outage_order)["paid_minor"] == "18400"
        pending = wait_for(lambda: evidence.delivery(ref),
            lambda rows: any(row["status"] != "acked" and row["attempts"] > 0 for row in rows))
        report["outbox_while_ops_down"] = {"order_id": outage_order, "quote": q, "delivery": pending}
        report["service_actions"].append(control("stop", "cs"))
        advance(timedelta(seconds=20))
    finally:
        report["service_actions"].append(control("start", "ops"))
        report["service_actions"].append(control("start", "cs"))
    acked = wait_for(lambda: evidence.delivery(ref), lambda rows: bool(rows) and all(row["status"] == "acked" for row in rows), seconds=30)
    report["outbox_after_restart"] = acked
    report["flow"] = flow
    before = wait_for(lambda: admin.ops("GET", "/monitoring?plan_id=" + plan_id),
        lambda value: value.get("actual") is not None and int(value["actual"]["payment_minor"]) >= 18400)
    # 接收边界验证：错来源/错窗/未来水位拒绝；相同修订和旧修订不叠加金额。
    actual = {key: value for key, value in before["actual"].items()
              if key not in {"net_paid_minor", "contribution_profit_minor", "profit_missing_reason"}}
    report["actual_negative"] = []
    for name, change in [("source", {"source_kind": "business_import"}),
                         ("window", {"window_start": (now() - timedelta(days=1)).isoformat()}),
                         ("future", {"watermark": (now() + timedelta(days=1)).isoformat()})]:
        response = service_request("ops", "POST", "/internal/activity-actuals", {**actual, **change},
                                   expected=422 if name == "future" else 409)
        report["actual_negative"].append({"case": name, "error": response["error"]})
    service_request("ops", "POST", "/internal/activity-actuals", actual)
    advance(datetime.fromisoformat(flow["plan"]["draft"]["end_at"]) + timedelta(seconds=1))
    ended = wait_for(lambda: admin.ops("GET", "/monitoring?plan_id=" + plan_id),
        lambda value: value.get("actual") is not None and datetime.fromisoformat(value["actual"]["watermark"]) >= now(), seconds=30)
    report["natural_end"] = ended
    advance(timedelta(seconds=10))
    report["late_refund"] = formal_refund(admin, buyer, outage_order, clients)
    late = wait_for(lambda: admin.ops("GET", "/monitoring?plan_id=" + plan_id),
        lambda value: value.get("actual") is not None and value["actual"]["refund_minor"] == "9200")
    assert late["actual"]["snapshot_id"] == before["actual"]["snapshot_id"]
    assert late["actual"]["payment_minor"] == before["actual"]["payment_minor"]
    assert late["actual"]["paid_units"] == before["actual"]["paid_units"]
    assert late["actual"]["consumed_discount_minor"] == before["actual"]["consumed_discount_minor"]
    service_request("ops", "POST", "/internal/activity-actuals", actual)
    assert admin.ops("GET", "/monitoring?plan_id=" + plan_id)["actual"] == late["actual"]
    report["late_actual"] = late
    # 零订单活动自然结束也必须产出完整零水位。
    product = admin.create_product("zero-orders")
    zero = admin.plan_publish(product["sku_id"], quantity_cap=4, label="zero-orders")
    advance(datetime.fromisoformat(zero["plan"]["draft"]["end_at"]) + timedelta(seconds=1))
    result = wait_for(lambda: admin.ops("GET", "/monitoring?plan_id=" + zero["plan"]["plan_id"]),
        lambda value: value.get("actual") is not None and datetime.fromisoformat(value["actual"]["watermark"]) >= now(), seconds=30)
    assert result["actual"]["payment_minor"] == "0" and result["actual"]["paid_units"] == 0
    report["zero_order_end"] = result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true")
    options = parser.parse_args()
    if not options.execute:
        parser.error("requires exclusive integration window and --execute")
    clients, evidence = [], EvidenceDB()
    report = {"integration_run_id": RUN, "models": "disabled", "started_at": datetime.now(UTC).isoformat(),
              "boundary": "real HTTP/PG with explicit local transport fault injection; no database fixture writes"}
    target = OUT / ("recovery-attempt-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ") + ".json")
    try:
        run(report, clients, evidence)
        report["status"] = "completed"
    except Exception as exc:
        report.update(status="failed", error_type=type(exc).__name__)
        if isinstance(exc, HttpFailure):
            report["error"] = {"status": exc.status, "code": exc.code, "path": exc.path}
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
