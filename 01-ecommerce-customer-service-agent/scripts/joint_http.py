"""真实双服务联调；仅合成开发数据，不直接写业务表，不调用模型。

所有业务写入走已有HTTP入口。可信测试时钟和显式合成成本通过本run文件更新。
每次执行独立留证；异常只公开状态/错误码，不公开Cookie、密码或连接串。
"""

import argparse
import hashlib
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import httpx

ROOT = Path(__file__).resolve().parents[1]
RUN = "XOPS-20261023-01"
PRIVATE = ROOT / ".runtime" / RUN
OUT = ROOT / "docs/06-acceptance" / RUN
ORIGIN = "http://127.0.0.1:5173"


def now():
    return datetime.fromisoformat(json.loads((PRIVATE / "joint-clock.json").read_text(encoding="utf-8"))["now"])


def atomic_json(path, value):
    temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def advance(target):
    if isinstance(target, timedelta):
        target = now() + target
    if target.tzinfo is None or target < now():
        raise ValueError("trusted integration clock cannot move backwards")
    atomic_json(PRIVATE / "joint-clock.json", {"now": target.isoformat()})
    return target


class HttpFailure(RuntimeError):
    def __init__(self, status, code, path):
        self.status, self.code, self.path = status, code, path
        super().__init__(f"HTTP {status} {code} at {path}")


class JointClient:
    def __init__(self, actor):
        self.actor, self.calls = actor, []
        self.cs_port = 8000
        self.http = httpx.Client(timeout=15, trust_env=False, follow_redirects=False)
        self.csrf = ""
        passwords = json.loads((ROOT / ".runtime/demo-accounts.json").read_text(encoding="utf-8"))
        session = self.cs("POST", "/session", {"login_name": actor, "password": passwords[actor]})
        self.csrf = session["csrf_token"]
        self.shipping = "shipping_sim_a" if actor == "customer_a" else None

    def close(self):
        self.http.close()

    def request(self, service, method, path, body=None, *, key=None, expected=None, headers=None):
        prefix = "/api/v1" if service == "cs" else "/api/ops/v1"
        port = self.cs_port if service == "cs" else 8120
        path = prefix + path
        request_headers = {"Origin": ORIGIN}
        if method != "GET":
            request_headers.update({"X-CSRF-Token": self.csrf, "Idempotency-Key": key or ("joint_" + uuid4().hex)})
        request_headers.update(headers or {})
        started = time.perf_counter()
        row = {"service": service, "method": method, "path": path}
        try:
            response = self.http.request(method, f"http://127.0.0.1:{port}" + path,
                                         json=body, headers=request_headers)
            row.update(status=response.status_code, request_id=response.headers.get("X-Request-ID"),
                       query_count=response.headers.get("X-Joint-Query-Count"),
                       sql_ms=response.headers.get("X-Joint-SQL-Ms"))
            try:
                value = response.json()
            except ValueError:
                value = {}
            code = value.get("error", {}).get("code", "http_error")
            if response.status_code >= 400:
                row["error_code"] = code
            if expected is not None:
                assert response.status_code == expected, (response.status_code, code, path)
                return value
            if not response.is_success:
                raise HttpFailure(response.status_code, code, path)
            return value["data"]
        finally:
            row["elapsed_ms"] = (time.perf_counter() - started) * 1000
            self.calls.append(row)

    def cs(self, method, path, body=None, **kwargs):
        return self.request("cs", method, path, body, **kwargs)

    def ops(self, method, path, body=None, **kwargs):
        return self.request("ops", method, path, body, **kwargs)

    def action(self, action, resource, version=1, parameters=None, **kwargs):
        proposal = self.cs("POST", "/action-proposals", {
            "action": action, "resource_ref": resource, "expected_version": version,
            "parameters": parameters or {},
        })
        return self.cs("POST", f"/confirmations/{proposal['confirmation_id']}/submit",
                       {"decision": "confirm"}, **kwargs)

    def quote(self, sku_id, qty=1):
        if self.shipping is None:
            shipping = self.cs("POST", "/me/shipping-inputs", {
                "recipient": "合成联调顾客", "phone": "13800001234", "province": "上海市",
                "city": "上海市", "district": "浦东新区", "street": "合成联调路1号",
            })
            self.shipping = shipping["business_ref"]
        return self.cs("POST", "/me/quotes", {"items": [{"sku_id": sku_id, "qty": qty}],
                      "cart_item_ids": [], "shipping_input_ref": self.shipping})

    def order(self, quote):
        ref = quote["quote_id"] if isinstance(quote, dict) else quote
        return self.action("order.create", ref, 1, {"quote_ref": ref})["business_ref"]

    def detail(self, ref):
        return self.cs("GET", "/me/orders/" + ref)

    def activity(self, ref):
        return self.cs("GET", "/shop/activities/" + ref)

    def manage(self, ref, phase):
        current = self.activity(ref)
        return self.cs("POST", f"/shop/activities/{ref}/{phase}", {
            "expected_revision": current["revision"], "reason": "合成联合验收 " + phase,
        })

    def create_product(self, label="gold", stock=200):
        value = self.cs("POST", "/shop/products", {"name": RUN + " " + label,
            "category": "联调合成商品", "description": "合成验收样本，非真实商家数据",
            "specifications": {"样本": label}, "price_minor": "10000"})
        value["inventory"] = self.cs("POST", f"/shop/skus/{value['sku_id']}/inventory-adjustments",
            {"delta": stock, "expected_version": 1, "reason": "合成联调初始库存"})
        source = json.loads((PRIVATE / "synthetic-inputs.json").read_text(encoding="utf-8"))
        source["skus"][value["sku_id"]] = {"cost_minor": 6000, "variable_fee_minor": 500}
        atomic_json(PRIVATE / "synthetic-inputs.json", source)
        return value

    def draft(self, snapshot, sku_id, *, quantity_cap=100, start=None, label="gold"):
        start = start or now() + timedelta(seconds=30)
        end = start + timedelta(hours=1)
        return {"title": RUN + " " + label, "snapshot_id": snapshot["snapshot_id"],
            "expected_snapshot_revision": snapshot["revision"], "start_at": start.isoformat(),
            "end_at": end.isoformat(), "selected_candidate": "direct", "fixed_fee_minor": "10000",
            "goal": {"min_revenue_minor": "0", "min_profit_minor": "0",
                     "budget_minor": "100000", "scenario_policy": "all"},
            "lines": [{"sku_id": sku_id, "quantities": {"low": quantity_cap, "base": quantity_cap, "high": quantity_cap},
                       "direct_price_minor": "9200", "basket_qty": 1, "coupon_minor": "800", "coupon_threshold_minor": "10000"}],
            "resources": [{"role": role, "volume_per_unit": "0.3", "productivity_per_hour": "200",
                           "people": 1, "shift_hours": "1", "attendance": "1", "efficiency": "1",
                           "source": "synthetic gold-v1; not approved real staffing", "confirmed": True}
                          for role in ("客服", "仓储")],
            "tasks": [{"title": "合成发布复核", "owner": "合成店长", "stage": "before_publish",
                       "due_at": now().isoformat(), "done": True},
                      {"title": "合成值守", "owner": "合成客服", "stage": "during", "due_at": start.isoformat(), "done": False},
                      {"title": "合成复盘", "owner": "合成店长", "stage": "after", "due_at": end.isoformat(), "done": False}],
            "monitoring": {"interval_minutes": 5, "data_source": "01.operations_snapshot",
                           "stop_conditions": ["额度不足拒绝新增活动订单"]},
            "notes": "合成销量情景与成本，不是经营预测或真实收益"}

    def prepare_plan(self, sku_id, **kwargs):
        source = self.ops("POST", "/snapshots/pull")
        plan = self.ops("POST", "/plans", self.draft(source, sku_id, **kwargs))
        evaluated = self.ops("POST", f"/plans/{plan['plan_id']}/evaluate", {"expected_version": plan["version"]})
        assert evaluated["status"] == "evaluated", evaluated.get("evaluation")
        return evaluated, source

    def confirm_plan(self, sku_id, **kwargs):
        plan, source = self.prepare_plan(sku_id, **kwargs)
        preflight = self.ops("POST", f"/plans/{plan['plan_id']}/execution-preview")
        plan = self.ops("POST", f"/plans/{plan['plan_id']}/confirm", {
            "expected_version": plan["version"], "reason": "核对合成来源、预算、库存与岗位容量"})
        approval = plan["approval"]
        payload = {"plan_id": plan["plan_id"], "plan_version": plan["version"],
                   "approval_ref": approval["approval_ref"], "manifest_digest": approval["manifest_digest"],
                   "preview_ref": approval["preview"]["preview_ref"]}
        return {"snapshot": source, "plan": plan, "preview": preflight,
                "publish_payload": payload}

    def plan_publish(self, sku_id, **kwargs):
        result = self.confirm_plan(sku_id, **kwargs)
        result["receipt"] = self.cs("POST", "/shop/activities", result["publish_payload"])
        assert result["receipt"]["status"] == "committed", result["receipt"]
        return result


def wait_for(operation, condition, *, seconds=20):
    deadline = time.monotonic() + seconds
    while True:
        value = operation()
        if condition(value):
            return value
        if time.monotonic() >= deadline:
            raise TimeoutError("bounded joint assertion not yet satisfied")
        time.sleep(.25)


def gold(report, clients):
    admin, buyer, supervisor, viewer, outsider = [JointClient(actor) for actor in (
        "shop_admin", "customer_a", "supervisor", "support_a", "customer_b")]
    clients.extend((admin, buyer, supervisor, viewer, outsider))
    report["identity"] = {actor.actor: {k: v for k, v in actor.cs("GET", "/session").items()
                            if k not in {"csrf_token", "expires_at"}} for actor in clients}
    assert buyer.ops("GET", "/context")["capabilities"] == []
    buyer.ops("GET", "/dashboard", expected=403)
    supervisor.ops("POST", "/plans/missing/confirm", {"expected_version": 1, "reason": "合成"}, expected=403)
    viewer.ops("GET", "/context")
    viewer.ops("POST", "/snapshots/pull", expected=403)
    admin.ops("POST", "/snapshots/pull", headers={"Origin": "http://untrusted.invalid"}, expected=403)
    product = admin.create_product()
    report["product"] = product
    flow = admin.plan_publish(product["sku_id"])
    report["flow"] = flow
    ref, sku = flow["receipt"]["activity_id"], product["sku_id"]
    # 同审批两个登录会话、换HTTP键重试：slot和活动必须一致。
    second = JointClient("shop_admin")
    clients.append(second)
    with ThreadPoolExecutor(max_workers=2) as pool:
        retries = list(pool.map(lambda client: client.cs("POST", "/shop/activities", flow["publish_payload"]), [admin, second]))
    assert all(r["activity_id"] == ref and r["intent_id"] == flow["receipt"]["intent_id"] for r in retries)
    report["publish_retries"] = retries
    advance(datetime.fromisoformat(flow["plan"]["draft"]["start_at"]) + timedelta(seconds=1))
    wait_for(lambda: admin.activity(ref), lambda row: row["phase"] == "active")
    before_wallet = buyer.cs("GET", "/me/wallet")["balance_minor"]
    buyer.cs("POST", "/me/topups", {"currency": "CNY", "amount_minor": "100000"})
    quote = buyer.quote(sku, 2)
    report["quote"] = quote
    assert [quote[k] for k in ("goods_minor", "discount_minor", "payable_minor")] == ["20000", "1600", "18400"]
    order = buyer.order(quote)
    report["order_id"] = order
    outsider.cs("GET", "/me/orders/" + order, expected=404)
    reserved = admin.activity(ref)
    assert reserved["reserved_discount_minor"] == "1600"
    buyer.action("order.pay", order, buyer.detail(order)["version"])
    paid = buyer.detail(order)
    assert paid["paid_minor"] == "18400"
    assert int(buyer.cs("GET", "/me/wallet")["balance_minor"]) == int(before_wallet) + 100000 - 18400
    admin.cs("POST", f"/shop/orders/{order}/shipments", {
        "quantities": {line["order_item_id"]: line["ordered_qty"] for line in paid["items"]},
        "expected_version": paid["version"]})
    buyer.action("order.receipt.confirm", order, buyer.detail(order)["version"])
    current = buyer.detail(order)
    claim = buyer.action("aftersale.apply", order, current["version"], {
        "quantities": {current["items"][0]["order_item_id"]: 1}, "reason": "ordinary",
        "description": "合成金样本正式部分退货"})
    report["claim"] = claim
    claim_id = claim["claim_id"]
    ticket = supervisor.cs("GET", "/staff/tickets/" + claim["ticket_id"])
    supervisor.cs("POST", f"/staff/tickets/{claim['ticket_id']}/assignments", {
        "staff_id": "support_a", "reason": "合成售后责任分派", "expected_version": ticket["version"]})
    viewer.cs("POST", f"/aftersales/{claim_id}/reviews", {"expected_version": 1,
        "decision": "approve", "basis": "核对合成订单政策与申请", "evidence_refs": ["policy:REQ-CS-v0.2"]})
    buyer.cs("POST", f"/aftersales/{claim_id}/return-shipment", {"expected_version": 2,
        "carrier": "合成退货快递", "tracking_no": "XOPS-RETURN-" + uuid4().hex[:8]})
    body = {"expected_version": 3, "lines": {current["items"][0]["order_item_id"]: {"received_qty": 1, "saleable_qty": 1}},
            "evidence_refs": ["inspection:XOPS-gold-v1"]}
    replay_key = "joint_inspection_" + uuid4().hex
    inspection = admin.cs("POST", f"/refund-claims/{claim_id}/inspections", body, key=replay_key)
    replay = admin.cs("POST", f"/refund-claims/{claim_id}/inspections", body, key=replay_key)
    assert inspection == replay and inspection["goods_minor"] == "9200"
    report["inspection"] = inspection
    after = buyer.detail(order)
    assert after["paid_minor"] == "18400" and after["refunded_minor"] == "9200"
    assert int(buyer.cs("GET", "/me/wallet")["balance_minor"]) == int(before_wallet) + 100000 - 9200
    committed = admin.activity(ref)
    assert committed["consumed_discount_minor"] == "1600" and committed["reserved_discount_minor"] == "0"
    report["ledger_after_refund"] = committed
    report["gold_actual"] = wait_for(
        lambda: admin.ops("GET", "/monitoring?plan_id=" + flow["plan"]["plan_id"]),
        lambda value: value.get("actual") is not None and value["actual"]["refund_minor"] == "9200")
    assert report["gold_actual"]["actual"]["payment_minor"] == "18400"
    assert report["gold_actual"]["actual"]["paid_units"] == 2
    assert 90000 - int(committed["consumed_discount_minor"]) == 88400
    # 暂停使未建单旧报价失效；已经建单仍按承诺价格支付。
    old_quote = buyer.quote(sku)
    promised = buyer.order(buyer.quote(sku))
    admin.manage(ref, "pause")
    try:
        buyer.order(old_quote)
    except HttpFailure as exc:
        assert exc.status == 409
        report["paused_old_quote"] = {"status": exc.status, "code": exc.code}
    else:
        raise AssertionError("paused quote incorrectly accepted")
    buyer.action("order.pay", promised, buyer.detail(promised)["version"])
    assert buyer.detail(promised)["paid_minor"] == "9200"
    admin.manage(ref, "resume")
    cancelled = buyer.order(buyer.quote(sku))
    buyer.action("order.cancel", cancelled, buyer.detail(cancelled)["version"])
    assert buyer.detail(cancelled)["trade_status"] == "cancelled_unpaid"
    expired = buyer.order(buyer.quote(sku))
    advance(timedelta(minutes=16))
    wait_for(lambda: buyer.detail(expired), lambda value: value["trade_status"] == "cancelled_unpaid")
    admin.manage(ref, "end")
    assert buyer.quote(sku)["discount_minor"] == "0"
    advance(datetime.fromisoformat(flow["plan"]["draft"]["end_at"]) + timedelta(seconds=1))
    monitor = wait_for(lambda: admin.ops("GET", "/monitoring?plan_id=" + flow["plan"]["plan_id"]),
        lambda value: value.get("actual") is not None and value["actual"]["payment_minor"] == "27600"
        and datetime.fromisoformat(value["actual"]["watermark"]) >= datetime.fromisoformat(flow["plan"]["draft"]["end_at"]), seconds=30)
    assert monitor["actual"]["refund_minor"] == "9200" and monitor["actual"]["consumed_discount_minor"] == "2400"
    assert monitor["actual"]["paid_units"] == 3 and monitor["actual"]["reserved_discount_minor"] == "0"
    report["monitor_final"] = monitor
    report["lifecycle_orders"] = {"promised_paid": promised, "cancelled": cancelled, "expired": expired}
    updated = admin.ops("POST", "/snapshots/pull")
    gold_order = next(row for row in updated["orders"] if row["order_id"] == order)
    assert gold_order["paid_minor"] == "18400"
    assert any(row["order_id"] == order and row["amount_minor"] == "9200" for row in updated["refunds"])
    report["source_after_refund"] = updated
    report["synthetic_inputs_sha256"] = hashlib.sha256((PRIVATE / "synthetic-inputs.json").read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true")
    options = parser.parse_args()
    if not options.execute:
        parser.error("real HTTP execution requires --execute and exclusive prepared test data")
    clients = []
    report = {"integration_run_id": RUN, "started_at": datetime.now(UTC).isoformat(),
              "source_kind": "synthetic", "models": "disabled", "status": "running"}
    target = OUT / ("http-attempt-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ") + ".json")
    try:
        gold(report, clients)
        report["status"] = "completed"
    except Exception as exc:
        report.update(status="failed", error_type=type(exc).__name__)
        if isinstance(exc, HttpFailure):
            report["error"] = {"status": exc.status, "code": exc.code, "path": exc.path}
        raise
    finally:
        report["request_chains"] = [{"actor": client.actor, "calls": client.calls} for client in clients]
        report["finished_at"] = datetime.now(UTC).isoformat()
        OUT.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        for client in clients:
            client.close()
        print(json.dumps({"status": report["status"], "evidence": str(target)}))


if __name__ == "__main__":
    main()
