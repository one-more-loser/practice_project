"""Build the v4 first-round 01 SFT candidate package.

Fixed split: 152 train (120 query / 24 clarify / 8 unsupported), 12 dev,
20 test. Test is byte-copied from the frozen test-v2 archive; dev is copied
from the retained v2 dev. Train groups are fixed before question paraphrases.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_REV = "CS-QUERY-B2-v1"
METRIC_REV = "CS-METRIC-B2-v1"
SCHEMA_DIGEST = "509990f5bdbafd1025ec769d6e1f3b3ad41462562f0c867ccd61ddc09caf64b9"
NOV = {"start": "2026-11-01T00:00:00+08:00", "end": "2026-12-01T00:00:00+08:00"}
NOV30 = {"start": "2026-11-30T00:00:00+08:00", "end": "2026-12-01T00:00:00+08:00"}
PAID = "trade_status IN ('paid','completed','cancelled_refunded','closed_refunded') AND paid_minor > 0"


def q(sql: str, params: list[str], parameters: dict | None = None, window: dict | None = NOV,
      fixture: str = "fx_a", expected: dict | None = None) -> dict:
    d = {"expected_kind": "query", "sql": sql, "parameter_names": params,
         "parameters": parameters or {}, "fixture": fixture}
    if window:
        d["window"] = window
    if expected:
        d["expected"] = expected
    return d


def clarify(slots: list[str], reason: str) -> dict:
    return {"expected_kind": "clarify", "clarify_slots": slots, "reason_code": reason}


def unsupported(reason: str) -> dict:
    return {"expected_kind": "unsupported", "reason_code": reason}


def load_extra(path: str | None) -> dict[str, dict]:
    if not path:
        return {}
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("extra_seeds") or data.get("seeds") or data.get("rows")
    assert isinstance(data, list), type(data)
    out = {row["id"]: row for row in data}
    assert set(out) == {f"N{i:02d}" for i in range(1, 9)}, sorted(out)
    return out


def _n_questions(nid: str, seed: dict) -> list[str]:
    extra = {
        "N01": ["2026年11月创建的订单按交易状态分组计数。", "统计我2026年11月创建订单的各状态单量。",
                "2026年11月下的订单，按交易状态分别有多少笔？", "看一下2026年11月创建订单的状态分布。",
                "按trade_status汇总2026年11月创建的订单数。", "2026年11月创建订单的状态计数是多少？",
                "帮我数2026年11月创建订单，按交易状态分。"],
        "N02": ["列出2026年11月创建、当前待付款的订单，按创建时间倒序，最多20条。",
                "2026年11月创建且待付款的订单明细，返回订单号、状态、实付和创建时间。",
                "查我2026年11月创建、还没付款的订单，最多20条。",
                "给我2026年11月创建的待付款订单列表（订单号/状态/实付分/创建时间）。",
                "2026年11月创建、状态为awaiting_payment的订单，按创建时间从新到旧，限20条。",
                "列出我2026年11月创建、当前待支付的订单及金额。",
                "2026年11月创建但尚未付款的订单有哪些？返回订单号、状态、实付分、创建时间。"],
        "N03": ["列出2026年11月创建且已支付订单中sku_1的明细，含明细号、订单号、件数、商品实付分。",
                "2026年11月创建且已支付订单里sku_1的商品行，按明细号排序，最多20条。",
                "查2026年11月创建、已支付订单中SKU sku_1的明细和实付。",
                "2026年11月创建且已支付的订单里，sku_2的明细行有哪些？",
                "列出2026年11月创建且已支付订单中sku_2的商品明细和件数。",
                "查2026年11月创建、已支付订单中SKU sku_2的明细与商品实付分。",
                "2026年11月创建且已支付订单里SKU sku_2的明细，按明细号排序最多20条。"],
        "N04": ["2026年11月创建的订单，按交易状态汇总整单实付分。", "统计2026年11月创建订单各交易状态的实付金额合计。",
                "按状态汇总我2026年11月创建订单的实付分。", "2026年11月创建订单，分交易状态算paid_minor合计。",
                "给我2026年11月创建订单按交易状态的整单实付金额。", "2026年11月创建订单各状态的实付总额是多少？",
                "汇总2026年11月创建订单的整单实付分，按状态分组。"],
        "N05": ["2026年11月创建且已支付订单中，整单实付最高的5单。", "按整单实付分降序列出2026年11月创建且已支付订单前5。",
                "2026年11月创建已支付订单，实付金额前5名。", "给我2026年11月创建、已支付订单实付分最高的5单。",
                "2026年11月创建且已支付订单按paid_minor从高到低取5条。", "列出我2026年11月创建已支付订单里实付最多的5笔。",
                "2026年11月创建、已支付订单实付分TOP5。"],
        "N06": ["公开商品中可售数量最多的5个SKU。", "按available_qty倒序列出公开商品前5个SKU。",
                "当前公开商品库存最多的5个SKU及价格。", "给我公开商品可售库存TOP5（SKU/名称/现价/可售数）。",
                "哪些公开SKU当前可售数量最多？取前5。", "公开商品里库存最多的5个SKU是什么？",
                "按可售库存从高到低列5个公开SKU。"],
        "N07": ["列出我2026年11月钱包支付流水（流水号、金额、时间），最新在前。",
                "2026年11月钱包payment类型的流水明细，按时间倒序限20条。",
                "查我2026年11月钱包支付记录，返回entry_id/amount/occurred_at。",
                "2026年11月钱包扣款流水有哪些？按发生时间倒序。",
                "给我2026年11月钱包payment流水，最新20条。",
                "列出2026年11月钱包支付的流水号、支付分和发生时间。",
                "2026年11月钱包支付明细，按时间从新到旧。"],
        "N08": ["2026年11月钱包refund发生额合计是多少分？", "统计我2026年11月钱包退款到账总额（分）。",
                "2026年11月退款进入钱包的金额合计。", "算2026年11月钱包refund流水金额总和。",
                "2026年11月钱包收到的退款合计多少分？", "汇总2026年11月钱包退款发生额。",
                "2026年11月钱包退款到账总金额（单位分）。"],
    }[nid]
    return [seed["question"]] + extra


def build_groups(extra: dict[str, dict]) -> list[dict]:
    groups: list[dict] = []

    def add(gid: str, tid: str, label: dict, questions: list[str], windows=None) -> None:
        assert len(questions) in (5, 6, 7, 8), (gid, len(questions))
        groups.append({"group_id": gid, "template_id": tid, "split": "train",
                       "label": label, "questions": questions,
                       "windows": windows or [None] * len(questions)})

    t01 = "SELECT count(*) AS n FROM cs_order_self WHERE created_at >= :start_at AND created_at < :end_at"
    add("G01", "T01", q(t01, ["start_at", "end_at"]),
        ["2026年11月我总共下了多少单？", "统计我 2026 年 11 月创建的订单数。",
         "帮我看下我 2026 年 11 月下了几笔订单。", "我在 2026 年 11 月创建了多少个订单？",
         "2026-11-30 上海时间当天创建了多少订单？", "按上海时区统计 2026 年 11 月 30 日创建的订单数。",
         "2026 年 11 月 30 日（上海）我下了几单？", "2026年11月30日当天（上海时区）创建的订单数量。"],
        [NOV] * 4 + [NOV30] * 4)

    t02 = f"SELECT count(*) AS n FROM cs_order_self WHERE created_at >= :start_at AND created_at < :end_at AND {PAID}"
    add("G02", "T02", q(t02, ["start_at", "end_at"]),
        ["2026年11月创建的订单中，已支付的有几单？", "统计我 2026 年 11 月创建的已支付订单数量。",
         "2026 年 11 月创建的已支付订单有几单？", "2026 年 11 月创建的订单中，已支付的订单数是多少？",
         "2026年11月创建、已支付的订单有几笔？", "统计2026年11月创建订单里已支付单量。",
         "2026年11月创建的paid cohort订单数是多少？", "帮我数一下2026年11月创建且已支付的订单。"])
    t03 = f"SELECT COALESCE(SUM(paid_minor),0) AS paid_minor FROM cs_order_self WHERE created_at >= :start_at AND created_at < :end_at AND {PAID}"
    add("G03", "T03", q(t03, ["start_at", "end_at"]),
        ["2026年11月创建且已支付的订单，实付金额合计是多少？", "统计我 2026 年 11 月创建的已支付订单实付金额。",
         "2026 年 11 月创建的订单中，已支付订单的实付合计是多少？", "我 2026 年 11 月创建且已支付订单的实付金额是多少？",
         "2026年11月创建且已支付的订单，实付合计多少钱？", "汇总2026年11月创建、已支付订单的实付分。",
         "算2026年11月创建、已支付订单的实付总额。", "2026年11月创建订单中已支付部分的实付合计。"])
    t04 = f"SELECT COALESCE(SUM(net_paid_minor),0) AS net_paid_minor FROM cs_order_self WHERE created_at >= :start_at AND created_at < :end_at AND {PAID}"
    add("G25", "T04", q(t04, ["start_at", "end_at"]),
        ["2026年11月创建且已支付订单的净支付金额是多少？", "2026 年 11 月创建的已支付订单扣掉退款后的净额。",
         "算一下我 2026 年 11 月创建且已支付订单的净支付金额。", "我 2026 年 11 月创建且已支付订单实际净付了多少钱？",
         "2026年11月创建且已支付订单的净实付分合计是多少？", "算一下2026年11月创建、已支付订单扣除退款后的净额。",
         "2026年11月创建、已支付订单的净支付金额合计（分）。", "我2026年11月创建且已支付的订单，退款后净付多少？"])
    t05 = "SELECT product_id, sku_id, name, price_minor, available_qty FROM cs_product_public WHERE sku_id = :sku_id"
    add("G05", "T05", q(t05, ["sku_id"], parameters={"sku_id": "sku_1"}, window=None),
        ["查一下 SKU sku_1 的公开名称、现价和可售库存。", "sku_1 现在卖多少钱，还有多少可售？",
         "给我看下商品 sku_1 的公开价格和库存。", "SKU sku_1 的现价与 available_qty 是多少？",
         "查一下SKU sku_2的公开名称、现价和可售库存。", "sku_2现在卖多少钱，库存还有多少？",
         "给我看sku_2的公开价格和可售数量。", "SKU sku_2的现价与available_qty。"])
    t07 = "SELECT balance_minor, currency, status FROM cs_wallet_summary_self"
    add("G06", "T07", q(t07, [], window=None),
        ["我的账户余额和状态。", "查一下我的钱包余额。", "我现在账户里还有多少钱？", "我的钱包当前余额和币种是什么？",
         "我的钱包余额、币种和状态是什么？", "查我账户当前余额和可用状态。",
         "我的账户现在余额多少，是什么币种？", "给我钱包摘要：余额和状态。"])
    t08 = "SELECT entry_id, amount_minor, occurred_at FROM cs_wallet_entry_self WHERE kind = 'refund' AND occurred_at >= :start_at AND occurred_at < :end_at ORDER BY occurred_at DESC"
    add("G07", "T08", q(t08, ["start_at", "end_at"]),
        ["2026年11月钱包退款到账明细，按时间倒序。", "我 2026 年 11 月退款到钱包的记录有哪些？",
         "列一下我 2026 年 11 月钱包收到的退款，最新的在前。", "2026 年 11 月钱包退款到账流水。",
         "列出2026年11月退款进入钱包的流水，按时间倒序。", "2026年11月钱包退款到账记录，最新在前。",
         "查我2026年11月退款到账的钱包流水。", "2026年11月钱包收到的退款明细。"])

    n_sql = {
        "N01": "SELECT trade_status, count(*) AS n FROM cs_order_self WHERE created_at >= :start_at AND created_at < :end_at GROUP BY trade_status ORDER BY trade_status",
        "N02": "SELECT order_id, trade_status, paid_minor, created_at FROM cs_order_self WHERE created_at >= :start_at AND created_at < :end_at AND trade_status = :status ORDER BY created_at DESC, order_id LIMIT 20",
        "N03": "SELECT i.order_item_id, i.order_id, i.ordered_qty, i.paid_goods_minor FROM cs_order_item_self i JOIN cs_order_self o ON i.order_id = o.order_id WHERE o.created_at >= :start_at AND o.created_at < :end_at AND i.sku_id = :sku_id AND o.trade_status IN ('paid','completed','cancelled_refunded','closed_refunded') AND o.paid_minor > 0 ORDER BY i.order_item_id LIMIT 20",
        "N04": "SELECT trade_status, COALESCE(SUM(paid_minor),0) AS paid_minor FROM cs_order_self WHERE created_at >= :start_at AND created_at < :end_at GROUP BY trade_status ORDER BY trade_status",
        "N05": "SELECT order_id, paid_minor FROM cs_order_self WHERE created_at >= :start_at AND created_at < :end_at AND trade_status IN ('paid','completed','cancelled_refunded','closed_refunded') AND paid_minor > 0 ORDER BY paid_minor DESC, order_id LIMIT 5",
        "N06": "SELECT sku_id, name, price_minor, available_qty FROM cs_product_public ORDER BY available_qty DESC, sku_id LIMIT 5",
        "N07": "SELECT entry_id, amount_minor, occurred_at FROM cs_wallet_entry_self WHERE kind = 'payment' AND occurred_at >= :start_at AND occurred_at < :end_at ORDER BY occurred_at DESC, entry_id LIMIT 20",
        "N08": "SELECT COALESCE(SUM(amount_minor),0) AS amount_minor FROM cs_wallet_entry_self WHERE kind = 'refund' AND occurred_at >= :start_at AND occurred_at < :end_at",
    }
    for nid in [f"N{i:02d}" for i in range(1, 9)]:
        seed = extra[nid]
        assert seed["sql"].strip() == n_sql[nid], nid
        add(f"G{8 + int(nid[1:]):02d}", nid,
            q(n_sql[nid], list(seed["parameter_names"]), parameters=dict(seed.get("parameters") or {}),
              window=seed.get("window") or None),
            _n_questions(nid, seed))

    add("G17", "T09", clarify(["refund_scope"], "refund_scope_ambiguous"),
        ["2026年11月我退款了多少钱？", "我 2026 年 11 月退了多少钱？",
         "2026 年 11 月退款金额是多少？", "统计一下我 2026 年 11 月的退款金额。",
         "2026年11月退款金额按哪个口径算？", "我2026年11月退的钱，是订单累计还是钱包到账？",
         "2026年11月退款总额是多少？", "帮我统计2026年11月退款金额。"])
    add("G18", "T10", clarify(["start_at", "end_at", "amount_scope"], "missing_time_and_amount_scope"),
        ["我最近花了多少钱？", "看看我花了多少。", "我最近花费是多少？", "帮我算算最近花掉的钱。",
         "我花了多少钱？", "帮我算算总花费。", "我最近消费了多少？", "看一下我的花费金额。"])
    add("G19", "T11", clarify(["order_id"], "missing_order_scope"),
        ["查一下我的订单。", "看看我的订单情况。", "帮我查订单。", "我的订单怎么样？",
         "帮我看看订单。", "我的订单状态是什么？", "查一下我的订单信息。", "订单现在怎么样？"])
    add("G20", "T12", unsupported("write_operation_not_supported"),
        ["帮我把订单 O1 退款。", "给订单 O1 发起退款。", "把 O1 取消并退款。", "我要退款订单 O1，直接处理。",
         "把订单O1退款。", "给O1办理退款。", "取消O1并退款。", "帮我退掉O1。"])
    return groups


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--output-dir", default="data/sft_v1")
    ap.add_argument("--extra-seeds", required=True)
    ap.add_argument("--seed", type=int, default=20261024)
    ap.add_argument("--dev-source", default="data/sft/dev.jsonl")
    ap.add_argument("--test-source", default="data/eval_archive/test-v2.jsonl")
    args = ap.parse_args()
    out = (ROOT / args.output_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)
    extra = load_extra(args.extra_seeds)
    groups = build_groups(extra)

    train = []
    for g in groups:
        for i, (question, label) in enumerate(zip(g["questions"], [g["label"]] * 8), 1):
            row = {"id": f"{g['template_id']}-{i:02d}", "profile": "customer_self",
                   "question": question, "template_id": g["template_id"],
                   "group_id": g["group_id"], "split": "train", "source": "03_synthetic_v4",
                   "review_status": "candidate_unreviewed", "schema_revision": SCHEMA_REV,
                   "metric_revision": METRIC_REV, "expected_kind": label["expected_kind"]}
            if label["expected_kind"] == "query":
                row.update({"sql": label["sql"], "parameter_names": label["parameter_names"],
                            "parameters": dict(label["parameters"])})
                window = g["windows"][i - 1] or label.get("window")
                if window:
                    row["window"] = window
                if label.get("fixture"):
                    row["fixture"] = label["fixture"]
            elif label["expected_kind"] == "clarify":
                row.update({"clarify_slots": label["clarify_slots"], "reason_code": label["reason_code"]})
            else:
                row["reason_code"] = label["reason_code"]
            train.append(row)

    for row in train:
        n = int(row["id"].split("-")[1])
        if row["template_id"] == "T05":
            row["parameters"]["sku_id"] = "sku_1" if n <= 4 else "sku_2"
        if row["template_id"] == "N03":
            row["parameters"]["sku_id"] = "sku_1" if n <= 4 else "sku_2"

    dev = [json.loads(x) for x in (ROOT / args.dev_source).read_text(encoding="utf-8").splitlines()]
    for row in dev:
        row["id"] = "DV-" + row["id"]  # content retained; IDs disambiguated from new train D02-D05
        if row["id"].startswith("DV-D03"):
            row["sql"] = "SELECT order_id FROM cs_order_self WHERE created_at >= :start_at AND created_at < :end_at AND trade_status = 'paid' AND fulfillment_status = 'unshipped'"
            row["parameter_names"] = ["start_at", "end_at"]
            row.pop("expected", None)
        if row["id"].startswith("DV-D04"):
            n = int(row["id"][-2:]) - 1
            row["question"] = ["2026年11月创建、11月承运商已签收的订单数。",
                               "统计我2026年11月创建且11月承运商签收的订单。",
                               "2026年11月创建、承运商在11月签收的订单有几单？"][n]
            row["sql"] = "SELECT count(*) AS n FROM (SELECT DISTINCT s.order_id FROM cs_shipment_self s JOIN cs_order_self o ON s.order_id = o.order_id WHERE o.created_at >= :start_at AND o.created_at < :end_at AND s.carrier_signed_at >= :start_at AND s.carrier_signed_at < :end_at) t"
            row["parameter_names"] = ["start_at", "end_at"]
            row.pop("expected", None)
    dev_bytes = ("".join(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n" for r in dev)).encode("utf-8")
    test_bytes = (ROOT / args.test_source).read_bytes()
    (out / "train.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n" for r in train), encoding="utf-8")
    (out / "dev.jsonl").write_bytes(dev_bytes)
    (out / "test.jsonl").write_bytes(test_bytes)

    test = [json.loads(x) for x in test_bytes.decode().splitlines()]
    all_rows = train + dev + test
    seeds = {"version": "sft-v1", "seed": args.seed, "extra_seeds": list(extra.values()), "groups": groups, "rows": all_rows}
    (out / "seeds.json").write_text(json.dumps(seeds, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def sha(p: Path) -> str:
        return hashlib.sha256(p.read_bytes()).hexdigest()

    manifest = {
        "version": "sft-v1", "seed": args.seed, "profile": "customer_self",
        "schema_revision": SCHEMA_REV, "metric_revision": METRIC_REV, "schema_digest": SCHEMA_DIGEST,
        "created_at": "2026-10-24T17:22:00+08:00",
        "split_policy": "group by business question + normalized SQL before paraphrases; train has 15 query groups + 4 non-query groups; test copied byte-for-byte from frozen test-v2; dev retained 12 rows with DV- IDs",
        "counts": {"train": len(train), "dev": len(dev), "test": len(test), "total": len(all_rows),
                   "train_query": sum(r["expected_kind"] == "query" for r in train),
                   "train_clarify": sum(r["expected_kind"] == "clarify" for r in train),
                   "train_unsupported": sum(r["expected_kind"] == "unsupported" for r in train)},
        "files": {
            "train": {"path": str((out / "train.jsonl").relative_to(ROOT)), "rows": len(train), "sha256": sha(out / "train.jsonl")},
            "dev": {"path": str((out / "dev.jsonl").relative_to(ROOT)), "rows": len(dev), "sha256": sha(out / "dev.jsonl")},
            "test": {"path": str((out / "test.jsonl").relative_to(ROOT)), "rows": len(test), "sha256": sha(out / "test.jsonl")},
        },
        "test_v1_sha256": "cf431555d87520d35faee61b965ca5c3450b341e00145a756641f443bb6b6c3d",
        "test_v2_sha256": sha(out / "test.jsonl"),
        "source": {"seed_pack": "training/seeds/首轮SFT新增8个模板候选.json", "existing_pool": "data/sft (47/12/20)", "public_data": "not integrated in this batch"},
        "templates": sorted({r["template_id"] for r in train}),
        "limitations": ["candidate_unreviewed; independent business review required",
                        "train 152 is an execution quota, not 152 independent SQL problems",
                        "test must remain byte-identical to the archived test-v2"],
    }
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(out), "counts": manifest["counts"], "test_sha256": manifest["test_v2_sha256"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
