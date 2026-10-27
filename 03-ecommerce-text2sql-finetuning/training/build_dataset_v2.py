"""Build the first customer_self SFT candidate set (v0.2).

Candidate set: hand-written seed templates, no teacher API, no gold claim.
Split rule: group by business question + normalized SQL, then split groups;
a SQL group and its paraphrases live in exactly one split. T01 and D01 were
the same normalized SQL and are therefore both in train; D03 now uses a
01-supported predicate instead of NOT EXISTS; T06 is removed from this batch.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "sft"
SCHEMA_REVISION = "CS-QUERY-B2-v1"
METRIC_REVISION = "CS-METRIC-B2-v1"
SCHEMA_DIGEST = "509990f5bdbafd1025ec769d6e1f3b3ad41462562f0c867ccd61ddc09caf64b9"
PAID = "trade_status IN ('paid','completed','cancelled_refunded','closed_refunded') AND paid_minor > 0"
NOV = ("2026-11-01T00:00:00+08:00", "2026-12-01T00:00:00+08:00")


def q(sql: str, params: list[str], *, parameters: dict | None = None, fixture: str | None = None, expected: dict | None = None) -> dict:
    obj = {"expected_kind": "query", "sql": sql, "parameter_names": params}
    if parameters:
        obj["parameters"] = parameters
    if fixture:
        obj["fixture"] = fixture
    if expected:
        obj["expected"] = expected
    return obj


def clarify(slots: list[str], reason: str) -> dict:
    return {"expected_kind": "clarify", "clarify_slots": slots, "reason_code": reason}


def unsupported(reason: str) -> dict:
    return {"expected_kind": "unsupported", "reason_code": reason}


# (template, group, split, window, label, questions)
SEEDS = [
    # ---------- train: order cohort counts/amounts ----------
    ("T01", "TR-ORDER-COUNT", "train", NOV,
     q("SELECT count(*) AS n FROM cs_order_self WHERE created_at >= :start_at AND created_at < :end_at", ["start_at", "end_at"]),
     ["2026年11月我总共下了多少单？", "统计我 2026 年 11 月创建的订单数。",
      "帮我看下我 2026 年 11 月下了几笔订单。", "我在 2026 年 11 月创建了多少个订单？"]),
    ("T02", "TR-PAID-COUNT", "train", NOV,
     q(f"SELECT count(*) AS n FROM cs_order_self WHERE created_at >= :start_at AND created_at < :end_at AND {PAID}", ["start_at", "end_at"]),
     ["2026年11月创建的订单中，已支付的有几单？", "统计我 2026 年 11 月创建的已支付订单数量。",
      "2026 年 11 月创建的已支付订单有几单？", "2026 年 11 月创建的订单中，已支付的订单数是多少？"]),
    ("T03", "TR-PAID-AMOUNT", "train", NOV,
     q(f"SELECT COALESCE(SUM(paid_minor),0) AS paid_minor FROM cs_order_self WHERE created_at >= :start_at AND created_at < :end_at AND {PAID}", ["start_at", "end_at"]),
     ["2026年11月创建且已支付的订单，实付金额合计是多少？", "统计我 2026 年 11 月创建的已支付订单实付金额。",
      "2026 年 11 月创建的订单中，已支付订单的实付合计是多少？", "我 2026 年 11 月创建且已支付订单的实付金额是多少？"]),
    ("T04", "TR-NET-AMOUNT", "train", NOV,
     q(f"SELECT COALESCE(SUM(net_paid_minor),0) AS net_paid_minor FROM cs_order_self WHERE created_at >= :start_at AND created_at < :end_at AND {PAID}", ["start_at", "end_at"]),
     ["2026年11月创建且已支付订单的净支付金额是多少？", "2026 年 11 月创建的已支付订单扣掉退款后的净额。",
      "算一下我 2026 年 11 月创建且已支付订单的净支付金额。", "我 2026 年 11 月创建且已支付订单实际净付了多少钱？"]),
    # ---------- train: resource lookups ----------
    ("T05", "TR-PRODUCT", "train", None,
     q("SELECT product_id, sku_id, name, price_minor, available_qty FROM cs_product_public WHERE sku_id = :sku_id", ["sku_id"], parameters={"sku_id": "sku_1"}, fixture="fx_a"),
     ["查一下 SKU sku_1 的公开名称、现价和可售库存。", "sku_1 现在卖多少钱，还有多少可售？",
      "给我看下商品 sku_1 的公开价格和库存。", "SKU sku_1 的现价与 available_qty 是多少？"]),
    ("T07", "TR-WALLET", "train", None,
     q("SELECT balance_minor, currency, status FROM cs_wallet_summary_self", [], fixture="fx_a"),
     ["我的账户余额和状态。", "查一下我的钱包余额。",
      "我现在账户里还有多少钱？", "我的钱包当前余额和币种是什么？"]),
    ("T08", "TR-WALLET-ENTRY", "train", NOV,
     q("SELECT entry_id, amount_minor, occurred_at FROM cs_wallet_entry_self WHERE kind = 'refund' AND occurred_at >= :start_at AND occurred_at < :end_at ORDER BY occurred_at DESC", ["start_at", "end_at"], fixture="fx_a"),
     ["2026年11月钱包退款到账明细，按时间倒序。", "我 2026 年 11 月退款到钱包的记录有哪些？",
      "列一下我 2026 年 11 月钱包收到的退款，最新的在前。", "2026 年 11 月钱包退款到账流水。"]),
    # ---------- train: clarify ----------
    ("T09", "TR-CLARIFY", "train", None,
     clarify(["refund_scope"], "refund_scope_ambiguous"),
     ["2026年11月我退款了多少钱？", "我 2026 年 11 月退了多少钱？",
      "2026 年 11 月退款金额是多少？", "统计一下我 2026 年 11 月的退款金额。"]),
    ("T10", "TR-CLARIFY", "train", None,
     clarify(["start_at", "end_at", "amount_scope"], "missing_time_and_amount_scope"),
     ["我最近花了多少钱？", "看看我花了多少。",
      "我最近花费是多少？", "帮我算算最近花掉的钱。"]),
    ("T11", "TR-CLARIFY", "train", None,
     clarify(["order_id"], "missing_order_scope"),
     ["查一下我的订单。", "看看我的订单情况。",
      "帮我查订单。", "我的订单怎么样？"]),
    # ---------- train: unsupported write ----------
    ("T12", "TR-UNSUPPORTED", "train", None,
     unsupported("write_operation_not_supported"),
     ["帮我把订单 O1 退款。", "给订单 O1 发起退款。",
      "把 O1 取消并退款。", "我要退款订单 O1，直接处理。"]),
    # D01 is the same normalized SQL as T01 (one-day window); keep it in train,
    # never in dev. This is the exact leak the user found.
    ("D01", "TR-ORDER-COUNT", "train", ("2026-11-30T00:00:00+08:00", "2026-12-01T00:00:00+08:00"),
     q("SELECT count(*) AS n FROM cs_order_self WHERE created_at >= :start_at AND created_at < :end_at", ["start_at", "end_at"]),
     ["2026-11-30 上海时间当天创建了多少订单？", "按上海时区统计 2026 年 11 月 30 日创建的订单数。",
      "2026 年 11 月 30 日（上海）我下了几单？"]),
    # ---------- dev: held-out SQL groups ----------
    ("D02", "DV-SHIP-TRANSIT", "dev", NOV,
     q("SELECT s.order_id FROM cs_shipment_self s JOIN cs_order_self o ON s.order_id = o.order_id WHERE o.created_at >= :start_at AND o.created_at < :end_at AND s.shipped_at >= :start_at AND s.shipped_at < :end_at AND s.status = 'in_transit'", ["start_at", "end_at"], fixture="fx_a"),
     ["2026年11月创建、且11月已发货、当前仍在运输中的订单有哪些？", "2026 年 11 月创建、11 月发货且当前运输中的订单。",
      "查我 2026 年 11 月创建、11 月已发货但还没签收的订单。"]),
    ("D03", "DV-UNSHIPPED-PAID", "dev", NOV,
     q(f"SELECT order_id FROM cs_order_self WHERE created_at >= :start_at AND created_at < :end_at AND {PAID} AND fulfillment_status = 'unshipped'", ["start_at", "end_at"], fixture="fx_a"),
     ["2026年11月创建的订单中，已支付但尚未发货的订单有哪些？", "2026 年 11 月创建的订单中，已支付但还没有发货的订单。",
      "2026 年 11 月创建的订单中，哪些已支付订单尚未发货？"]),
    ("D04", "DV-CARRIER-SIGNED", "dev", NOV,
     q("SELECT count(*) AS n FROM (SELECT DISTINCT s.order_id FROM cs_shipment_self s JOIN cs_order_self o ON s.order_id = o.order_id WHERE o.created_at >= :start_at AND o.created_at < :end_at AND s.carrier_signed_at >= :start_at AND s.carrier_signed_at < :end_at) t", ["start_at", "end_at"], fixture="fx_a"),
     ["2026年11月创建的订单中，11月承运商已签收的订单数。", "2026 年 11 月创建、11 月由承运商签收的订单数。",
      "统计我 2026 年 11 月创建、11 月承运商已签收的订单数量。"]),
    ("D05", "DV-CUSTOMER-RECEIVED", "dev", NOV,
     q("SELECT count(*) AS n FROM cs_order_self WHERE created_at >= :start_at AND created_at < :end_at AND received_at >= :start_at AND received_at < :end_at", ["start_at", "end_at"], fixture="fx_a"),
     ["2026年11月创建的订单中，11月本人确认收货的订单有多少？", "2026 年 11 月创建、11 月本人确认收货的订单数。",
      "统计我 2026 年 11 月创建、11 月本人已收货的订单数。"]),
    # ---------- test: frozen held-out groups ----------
    ("E01", "TS-REFUND", "test", NOV,
     q(f"SELECT COALESCE(SUM(refunded_minor),0) AS refunded_minor FROM cs_order_self WHERE created_at >= :start_at AND created_at < :end_at AND {PAID}", ["start_at", "end_at"], fixture="fx_a", expected={"columns": ["refunded_minor"], "rows": [[25500]]}),
     ["2026年11月创建、已支付订单的累计退款是多少？", "按订单累计口径，2026 年 11 月创建且已支付订单的累计退款金额。",
      "用订单累计退款口径，算 2026 年 11 月创建的已支付订单退款总额。", "2026 年 11 月创建且已支付订单的累计退款合计是多少？"]),
    ("E02", "TS-DETAIL", "test", None,
     q("SELECT order_item_id, sku_id, ordered_qty, refunded_qty FROM cs_order_item_self WHERE order_id = :order_id", ["order_id"], parameters={"order_id": "O1"}, fixture="fx_a", expected={"columns": ["order_item_id", "sku_id", "ordered_qty", "refunded_qty"], "rows": [["OI1", "sku_1", 1, 0], ["OI2", "sku_2", 1, 0]]}),
     ["订单 O1 的商品明细和退款数量。", "查订单 O1 的明细行、SKU 和退款件数。",
      "O1 里买了什么，退了几件？", "给我订单 O1 的 order_item 明细。"]),
    ("E03", "TS-DETAIL", "test", None,
     q("SELECT shipment_id, carrier, status, shipped_at, carrier_signed_at FROM cs_shipment_self WHERE order_id = :order_id", ["order_id"], parameters={"order_id": "O2"}, fixture="fx_a", expected={"columns": ["shipment_id", "carrier", "status", "shipped_at", "carrier_signed_at"], "rows": [["SH2", "SF", "carrier_signed", "2026-11-30T17:00:00+00:00", "2026-12-02T02:00:00+00:00"]]}),
     ["订单 O2 的承运商物流状态和承运商签收时间。", "查 O2 的包裹、承运商和承运商签收时间。",
      "O2 发货了吗，承运商什么时候签收的？", "订单 O2 的 shipment 记录。"]),
    ("E04", "TS-CLARIFY", "test", None,
     clarify(["refund_rate_scope"], "refund_rate_definition_ambiguous"),
     ["2026年11月我的退款率是多少？", "算一下我 2026 年 11 月的退款率。",
      "2026 年 11 月退款订单占比是多少？", "我 2026 年 11 月退款率怎么样？"]),
    ("E05", "TS-UNSUPPORTED", "test", None,
     unsupported("cross_customer_scope_not_allowed"),
     ["查一下其他顾客的订单。", "把顾客 cust_b 的订单都列出来。",
      "看看别人的订单和退款。", "导出所有顾客的订单。"]),
]


def _norm_sql(sql: str) -> str:
    import sqlglot
    return sqlglot.parse_one(sql, read="postgres").sql(dialect="postgres", normalize=True)


def _test_archive(rows: list[dict]) -> dict:
    path = ROOT / "data" / "eval_archive" / "test-v1.jsonl"
    if not path.exists():
        return {"path": str(path.relative_to(ROOT)), "exists": False}
    old = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]
    diff = []
    for a, b in zip(old, rows):
        if a != b:
            diff.append({"id": a["id"], "changed": [k for k in a if a.get(k) != b.get(k)]})
    return {
        "path": str(path.relative_to(ROOT)),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "rows": len(old),
        "diff_rows": len(diff),
        "diff_ids": [x["id"] for x in diff],
        "diff_fields": sorted({k for x in diff for k in x["changed"]}),
        "note": "test-v1 is archived; test.jsonl is test-v2; only E01 question text changed",
    }


def build() -> dict:
    splits = {"train": [], "dev": [], "test": []}
    sql_groups: dict[str, list[str]] = {}
    for tid, group, split, window, label, questions in SEEDS:
        for i, question in enumerate(questions, 1):
            row = {
                "id": f"{tid}-{i:02d}",
                "profile": "customer_self",
                "schema_revision": SCHEMA_REVISION,
                "metric_revision": METRIC_REVISION,
                "schema_digest": SCHEMA_DIGEST,
                "question": question,
                "template_id": tid,
                "group_id": group,
                "source": "handwritten_seed_candidate",
                "review_status": "candidate_unreviewed",
                "split": split,
                **label,
            }
            if window:
                row["window"] = {"start": window[0], "end": window[1]}
            splits[split].append(row)
            if row["expected_kind"] == "query":
                key = _norm_sql(row["sql"])
                sql_groups.setdefault(key, []).append(row["id"])

    split_of = {r["id"]: r["split"] for rows in splits.values() for r in rows}
    for key, ids in sql_groups.items():
        ss = {split_of[i] for i in ids}
        if len(ss) != 1:
            raise ValueError(f"SQL group crosses splits {ss}: {ids}")
    for a in splits:
        for b in splits:
            if a < b:
                sa = {_norm_sql(r["sql"]) for r in splits[a] if r["expected_kind"] == "query"}
                sb = {_norm_sql(r["sql"]) for r in splits[b] if r["expected_kind"] == "query"}
                if sa & sb:
                    raise ValueError(f"normalized SQL overlap {a}/{b}: {len(sa & sb)}")

    OUT.mkdir(parents=True, exist_ok=True)
    files = {}
    for split, rows in splits.items():
        path = OUT / f"{split}.jsonl"
        path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
        files[split] = {"path": str(path.relative_to(ROOT)), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "rows": len(rows)}
    manifest = {
        "version": "sft-candidate-v0.2",
        "created_at": "2026-10-24T15:55:00+08:00",
        "profile": "customer_self",
        "schema_revision": SCHEMA_REVISION,
        "metric_revision": METRIC_REVISION,
        "schema_digest": SCHEMA_DIGEST,
        "split_policy": "group by business question + normalized SQL, then paraphrases; no SQL group crosses splits",
        "counts": {k: len(v) for k, v in splits.items()},
        "templates": {k: sorted({r["template_id"] for r in v}) for k, v in splits.items()},
        "sql_groups": {f"g{i:02d}": ids for i, ids in enumerate(sorted(sql_groups.values()), 1)},
        "files": files,
        "test_archive": _test_archive(splits["test"]),
        "limitations": [
            "hand-written candidates, not 01 business-gold reviewed",
            "no teacher API used",
            "T06 purchase-history question removed because no time/paid scope was defined; D03 replaced NOT EXISTS with an 01-supported paid/unshipped predicate",
            "25-branch sandbox questions are not independent training questions",
            "test is small and template-held-out; not final generalization",
        ],
    }
    (OUT / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


if __name__ == "__main__":
    print(json.dumps(build(), ensure_ascii=False, indent=2))
