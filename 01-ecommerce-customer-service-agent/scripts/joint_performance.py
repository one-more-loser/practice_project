"""小样本真实HTTP性能/竞争证据；须在gold结束且固定test独占窗口执行。

运行：backend/.venv/Scripts/python.exe scripts/joint_performance.py --execute
不重置数据、不调用模型；通过正常取消释放本脚本未付订单。
"""
import argparse
import json
import math
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

from joint_files import source_manifest

ROOT = Path(__file__).resolve().parents[1]
OPS = ROOT.parent / "02-ecommerce-operations-agent"
RUN = "XOPS-20261023-01"
PRIVATE = ROOT / ".runtime" / RUN
OUT = ROOT / "docs/06-acceptance" / RUN


def percentile(values, percentile_value):
    """最近秩：样本量/算法同时出现在报告中。"""
    values = sorted(values)
    return values[max(0, math.ceil(len(values) * percentile_value) - 1)] if values else None


def summary(rows, elapsed):
    successful = [row["elapsed_ms"] for row in rows if row["success"]]
    return {"attempts": len(rows), "successes": len(successful),
            "errors": sum(not row["success"] for row in rows),
            "timeouts": sum(row.get("timeout", False) for row in rows),
            "denominator": len(rows), "p50_ms": percentile(successful, .50),
            "p95_ms": percentile(successful, .95), "wall_seconds": elapsed,
            "attempts_per_second": len(rows) / elapsed if elapsed else None,
            "successes_per_second": len(successful) / elapsed if elapsed else None,
            "latency_population": "successful logical operations; failed latency retained in raw rows",
            "percentile_method": "nearest rank"}


class Observer:
    """采样非事务级：不宣称未观测到Lock等于没有锁等待。"""
    def __init__(self):
        self.stop_event = threading.Event()
        self.rows = []
        self.engine = None

    def __enter__(self):
        from cs_agent.config import Settings
        from cs_agent.db.session import verify_target
        from cs_agent.runtime_profiles import profile_values
        from sqlalchemy import create_engine
        _, values = profile_values("debian")
        self.engine = create_engine(values["CS_TEST_MIGRATION_URL"], hide_parameters=True,
                                    pool_size=1, max_overflow=0,
                                    connect_args={"connect_timeout": 3, "options": "-c statement_timeout=2000"})
        verify_target(self.engine, Settings(expected_database="cs_agent_b0_test",
                                           isolation_file=values["CS_ISOLATION_FILE"]), migration=True)
        state = json.loads((PRIVATE / "joint-processes.json").read_text(encoding="utf-8"))
        self.pids = [int(row["pid"]) for row in state["processes"] if row["service"] in {"cs", "ops"}]
        self.thread = threading.Thread(target=self.poll, daemon=True)
        self.thread.start()
        return self

    def poll(self):
        from sqlalchemy import text
        while not self.stop_event.is_set():
            row = {"at": datetime.now(UTC).isoformat()}
            try:
                with self.engine.connect() as connection:
                    row["postgres"] = [dict(item) for item in connection.execute(text(
                        "SELECT datname, count(*) AS connections, "
                        "count(*) FILTER (WHERE state='active') AS active, "
                        "count(*) FILTER (WHERE wait_event_type='Lock') AS lock_waiters, "
                        "count(*) FILTER (WHERE state IS NULL) AS state_hidden "
                        "FROM pg_stat_activity WHERE datname IN "
                        "('cs_agent_b0_test','ops02_joint_xops_20261023_01') GROUP BY datname"
                    )).mappings()]
                command = ("Get-Process -Id " + ",".join(map(str, self.pids)) +
                           " | Select-Object Id,CPU,WorkingSet64,PrivateMemorySize64 | ConvertTo-Json -Compress")
                result = subprocess.run(["powershell.exe", "-NoProfile", "-Command", command],
                                        capture_output=True, text=True, timeout=5, check=False,
                                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                row["processes"] = json.loads(result.stdout) if result.stdout.strip() else []
            except Exception as exc:  # noqa: BLE001 -- 观察失败只记异常类型，避免凭据进入公开证据。
                # 异常只记类型，避免连接错误中泄漏URL/凭据。
                row["observation_error"] = type(exc).__name__
            self.rows.append(row)
            self.stop_event.wait(.25)

    def __exit__(self, *_):
        self.stop_event.set()
        self.thread.join(timeout=10)
        self.engine.dispose()


def batch(label, jobs, operation, workers=2):
    client_locks = {id(client): threading.Lock() for client, _ in jobs}
    def invoke(job):
        client, payload = job
        with client_locks[id(client)]:
            before = len(client.calls)
            started = time.perf_counter()
            row = {"group": label}
            try:
                value = operation(client, payload)
                row.update(success=True, result=value)
            except Exception as exc:  # noqa: BLE001 -- 每次失败保留在分母和原始行中，不能中断采集而漏报。
                row.update(success=False, error_type=type(exc).__name__, timeout="timeout" in type(exc).__name__.lower())
            row["elapsed_ms"] = (time.perf_counter() - started) * 1000
            row["request_chain"] = client.calls[before:]
        return row
    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        rows = list(pool.map(invoke, jobs))
    return {"summary": summary(rows, time.perf_counter() - started), "rows": rows}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true", help="调用者已取得父执行者固定test独占窗口")
    parser.add_argument("--samples", type=int, default=20)
    options = parser.parse_args()
    if not options.execute:
        parser.error("真实HTTP/PG观测必须显式--execute并已获父执行者窗口")
    if not 20 <= options.samples <= 100:
        parser.error("每组样本必须20至100")
    from joint_http import JointClient, advance, now
    report = {"integration_run_id": RUN, "started_at": datetime.now(UTC).isoformat(),
              "concurrency": 2, "samples_per_group": options.samples, "models": "disabled",
              "sources_before": {"01": source_manifest(ROOT), "02": source_manifest(OPS)},
              "groups": {}, "limitations": ["小样本并发2，不证明15分钟或8小时稳定性", "新增P95≤50ms未签核，报告实测不判该门槛", "PG锁等待为采样观察，可能错过短等待；state_hidden报告可见性", "CPU为进程累计CPU秒；RAM为工作集/私有字节；观察器本机开销包含在环境中"]}
    OUT.mkdir(parents=True, exist_ok=True)
    attempt = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    target = OUT / ("performance-attempt-" + attempt + ".json")
    clients = []
    try:
        execute(report, options, JointClient, now, advance, clients)
        report["status"] = "completed"
    except Exception as exc:
        report["status"] = "failed"
        report["error_type"] = type(exc).__name__
        raise
    finally:
        for client in clients:
            client.close()
        report["finished_at"] = datetime.now(UTC).isoformat()
        report["sources_after"] = {"01": source_manifest(ROOT), "02": source_manifest(OPS)}
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"status": report["status"], "evidence": str(target)}))


def execute(report, options, JointClient, now, advance, clients):
    """同SKU、相同样本量的顺序A/B；建单度量只含确认链，准备报价/取消另计。"""
    admin = JointClient("shop_admin")
    buyers = [JointClient("customer_a"), JointClient("customer_b")]
    clients.extend([admin, *buyers])
    product = admin.create_product("performance-" + datetime.now(UTC).strftime("%H%M%S"))
    sku = product["sku_id"]
    report["product"] = product
    report["design"] = "same SKU, sequential no-activity then activity, concurrency up to 2; per-client calls serialized; unrandomized small sample"
    report["cleanup_orders"] = []
    with Observer() as observer:
        for phase, expected_discount in (("baseline", "0"), ("activity", "800")):
            if phase == "activity":
                flow = admin.plan_publish(sku, label="performance")
                report["flow"] = flow
                advance(datetime.fromisoformat(flow["plan"]["draft"]["start_at"]) + timedelta(seconds=1))
            # 两边先建立收货资料和预热；资料创建不混入报价测量。
            for buyer in buyers:
                assert buyer.quote(sku)["discount_minor"] == expected_discount
            quote_jobs = [(buyers[i % 2], sku) for i in range(options.samples)]

            def quote_operation(client, selected_sku, discount=expected_discount):
                value = client.quote(selected_sku)
                assert value["discount_minor"] == discount
                return {"quote_id": value["quote_id"], "discount_minor": value["discount_minor"]}

            report["groups"][phase + "_quote"] = batch(phase + "_quote", quote_jobs, quote_operation)
            order_jobs = [(buyers[i % 2], buyers[i % 2].quote(sku)) for i in range(options.samples)]

            def order_operation(client, quote):
                return {"order_id": client.order(quote)}

            created = batch(phase + "_create", order_jobs, order_operation)
            report["groups"][phase + "_create"] = created
            for i, row in enumerate(created["rows"]):
                if row["success"]:
                    ref = row["result"]["order_id"]
                    buyer = buyers[i % 2]
                    buyer.action("order.cancel", ref, buyer.detail(ref)["version"])
                    report["cleanup_orders"].append(ref)
            assert all(group["summary"]["errors"] == 0 for group in report["groups"].values())
        # 经营测算按真实evaluate接口计时，使用新的未确认方案，健康请求不进入统计。
        plan, _ = admin.prepare_plan(sku, label="evaluate-performance")
        report["groups"]["ops_evaluate"] = batch("ops_evaluate", [(admin, plan)] * options.samples,
            lambda client, p: {"status": client.ops("POST", f"/plans/{p['plan_id']}/evaluate",
                        {"expected_version": p["version"]})["status"]}, workers=1)
        report["observations"] = observer.rows
    report["differences"] = {operation: {
        "p95_added_ms": report["groups"]["activity_" + operation]["summary"]["p95_ms"] - report["groups"]["baseline_" + operation]["summary"]["p95_ms"],
        "threshold_status": "P95 added <=50ms not approved; measurement only"}
        for operation in ("quote", "create")}
    evaluation = report["groups"]["ops_evaluate"]["summary"]
    report["ops_evaluate_3s"] = {"p95_below_target": evaluation["p95_ms"] <= 3000,
                                "errors": evaluation["errors"], "dataset_skus": len(report["flow"]["snapshot"]["skus"]),
                                "scope": "current synthetic small dataset, not 100k rows or deployment hardware"}
    report["query_metrics"] = {}
    for name, group in report["groups"].items():
        queries = [int(call["query_count"]) for row in group["rows"] for call in row["request_chain"] if call.get("query_count") is not None]
        report["query_metrics"][name] = {"requests_with_metrics": len(queries), "queries_sum": sum(queries),
                                       "queries_min": min(queries) if queries else None, "queries_max": max(queries) if queries else None}
    assert evaluation["errors"] == 0


if __name__ == "__main__":
    main()
