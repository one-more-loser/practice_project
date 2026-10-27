"""只读探测Debian应用库：先验证隔离身份，再测固定规模查询和资金对账。"""

import json
import math
import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import create_engine, text

from cs_agent.config import PROJECT_ROOT, Settings
from cs_agent.db.session import verify_target
from cs_agent.runtime_profiles import profile_values


def main():
    _, values = profile_values("debian")
    engine = create_engine(
        values["CS_DATABASE_URL"],
        pool_size=10,
        max_overflow=0,
        pool_timeout=3,
        pool_pre_ping=True,
        hide_parameters=True,
        connect_args={
            "connect_timeout": 3,
            "options": "-c statement_timeout=2000 -c timezone=UTC -c default_transaction_read_only=on",
        },
    )
    verify_target(
        engine,
        Settings(
            expected_database=values["CS_EXPECTED_DATABASE"],
            isolation_file=Path(values["CS_ISOLATION_FILE"]),
        ),
    )
    with engine.connect() as connection:
        # 只保存公开运行版本、行数/一致性计数，证据不含顾客资料和资金明细。
        version = connection.execute(text("SHOW server_version")).scalar_one()
        vector = connection.execute(
            text("SELECT extversion FROM pg_extension WHERE extname='vector'")
        ).scalar_one()
        schema = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        query = text(
            "SELECT order_id, trade_status FROM customer_order WHERE owner_id=:customer "
            "AND store_id=:store ORDER BY created_at DESC LIMIT 20"
        )
        # 两个参数来自已有合成样本；不让模型或用户提供任意SQL。
        parameters = {"customer": "customer_a", "store": "store_sim"}
        connection.execute(query, parameters).all()
        reconciliation = {
            "wallet_amount_or_sequence_mismatches": connection.execute(
                text(
                    "SELECT count(*) FROM wallet_account a LEFT JOIN "
                    "(SELECT account_id, sum(CASE WHEN kind='payment' THEN -amount_minor "
                    "ELSE amount_minor END) AS total, max(sequence) AS last_sequence, "
                    "count(*) AS entries FROM wallet_entry GROUP BY account_id) e "
                    "ON e.account_id=a.account_id WHERE e.account_id IS NULL "
                    "OR a.balance_minor<>coalesce(e.total,0) "
                    "OR a.ledger_sequence<>e.last_sequence OR e.entries<>e.last_sequence+1"
                )
            ).scalar_one(),
            "wallet_chain_mismatches": connection.execute(
                text(
                    "SELECT count(*) FROM (SELECT balance_before_minor, sequence, "
                    "lag(balance_after_minor) OVER(PARTITION BY account_id ORDER BY sequence) "
                    "AS previous FROM wallet_entry) e WHERE sequence>0 AND "
                    "balance_before_minor IS DISTINCT FROM previous"
                )
            ).scalar_one(),
            "payment_link_mismatches": connection.execute(
                text(
                    "SELECT count(*) FROM order_payment p "
                    "JOIN customer_order o ON o.order_id=p.order_id "
                    "JOIN wallet_entry e ON e.entry_id=p.entry_id "
                    "WHERE p.amount_minor<>o.paid_minor OR p.amount_minor<>e.amount_minor "
                    "OR e.kind<>'payment' OR p.operation_id<>e.operation_id "
                    "OR o.owner_id<>e.owner_id OR o.store_id<>e.store_id"
                )
            ).scalar_one(),
        }

    barrier = threading.Barrier(10)

    def worker(_):
        durations = []
        errors = 0
        # 并发预热10条连接；计时包括连接获取、pre_ping、查询与事务结束。
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        barrier.wait(timeout=10)
        for _ in range(50):
            started = time.perf_counter()
            try:
                with engine.connect() as connection:
                    connection.execute(query, parameters).all()
            except Exception:
                errors += 1
            durations.append((time.perf_counter() - started) * 1000)
        return durations, errors

    try:
        started = time.perf_counter()
        with ThreadPoolExecutor(max_workers=10) as executor:
            results = list(executor.map(worker, range(10)))
        elapsed = time.perf_counter() - started
        ordered = sorted(value for durations, _ in results for value in durations)
        errors = sum(count for _, count in results)

        def percentile(value):
            return round(ordered[math.ceil(len(ordered) * value) - 1], 3)

        report = {
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "profile": "debian",
            "schema_revision": schema,
            "postgresql": version,
            "pgvector": vector,
            "workload": {
                "level": "read_only_database_probe",
                "workers": 10,
                "samples": len(ordered),
                "query": "bounded customer/store orders projection, limit 20",
                "pool_size": 10,
                "max_overflow": 0,
                "statement_timeout_ms": 2000,
                "includes": "pool checkout, pre_ping, query, rollback; warm connections",
                "dataset": "migrated B1 synthetic application data",
            },
            "results": {
                "errors": errors,
                "elapsed_seconds_including_warmup": round(elapsed, 3),
                "median_ms": round(statistics.median(ordered), 3),
                "p95_ms": percentile(0.95),
                "p99_ms": percentile(0.99),
                "maximum_ms": round(max(ordered), 3),
            },
            "development_budget": {"p95_ms": 200, "errors": 0},
            "business_reconciliation": reconciliation,
            "passed": errors == 0
            and percentile(0.95) <= 200
            and all(value == 0 for value in reconciliation.values()),
            "limits": "DB-only short probe; excludes HTTP/model/15-minute mixed load/8-hour availability",
        }
        output = PROJECT_ROOT / "docs/06-acceptance/debian-db/database-probe.json"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False))
        if not report["passed"]:
            raise SystemExit(1)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
