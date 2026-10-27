"""固定test只读账本勾稽与真实导出；不reset，不修改业务表。"""
import json
from datetime import UTC, datetime

from joint_http import OUT, RUN, JointClient, wait_for
from joint_recovery import EvidenceDB
from sqlalchemy import text

CHECKS = {
    "wallet_balance_sequence": "SELECT count(*) FROM wallet_account a LEFT JOIN (SELECT account_id,sum(CASE WHEN kind='payment' THEN -amount_minor ELSE amount_minor END) total,max(sequence) last_sequence,count(*) entries FROM wallet_entry GROUP BY account_id) e USING(account_id) WHERE a.balance_minor<>coalesce(e.total,0) OR a.ledger_sequence IS DISTINCT FROM e.last_sequence OR e.entries<>e.last_sequence+1",
    "wallet_entry_chain": "WITH e AS (SELECT account_id,sequence,balance_before_minor,lag(balance_after_minor) OVER(PARTITION BY account_id ORDER BY sequence) previous FROM wallet_entry) SELECT count(*) FROM e WHERE sequence>0 AND balance_before_minor IS DISTINCT FROM previous",
    "order_payment_wallet": "SELECT count(*) FROM customer_order o LEFT JOIN order_payment p USING(order_id) LEFT JOIN wallet_entry e ON e.entry_id=p.entry_id WHERE o.paid_minor<>coalesce(p.amount_minor,0) OR (p.entry_id IS NOT NULL AND (e.kind<>'payment' OR e.amount_minor<>p.amount_minor))",
    "order_refund_totals": "WITH r AS (SELECT order_id,goods_minor+freight_minor amount FROM refund_execution UNION ALL SELECT order_id,amount_minor FROM cancellation_refund), t AS (SELECT order_id,sum(amount) amount FROM r GROUP BY order_id) SELECT count(*) FROM customer_order o LEFT JOIN t USING(order_id) WHERE o.refunded_minor<>coalesce(t.amount,0)",
    "formal_refund_wallet": "SELECT count(*) FROM refund_execution r JOIN wallet_entry e USING(entry_id) WHERE e.kind<>'refund' OR e.amount_minor<>r.goods_minor+r.freight_minor",
    "inventory_reservations": "SELECT count(*) FROM inventory i LEFT JOIN (SELECT sku_id,sum(qty) qty FROM stock_reservation WHERE status='reserved' GROUP BY sku_id) r USING(sku_id) WHERE i.reserved_qty<>coalesce(r.qty,0)",
    "inventory_movement_totals": "SELECT count(*) FROM inventory i LEFT JOIN (SELECT sku_id,sum(stock_delta) stock,sum(reserved_delta) reserved FROM stock_movement GROUP BY sku_id) m USING(sku_id) WHERE i.stock_qty<>coalesce(m.stock,0) OR i.reserved_qty<>coalesce(m.reserved,0)",
    "activity_budget_ledger": "WITH a AS (SELECT activity_id,sum(discount_minor) FILTER(WHERE status='reserved') reserved,sum(discount_minor) FILTER(WHERE status='consumed') consumed FROM activity_order_allocation GROUP BY activity_id) SELECT count(*) FROM activity_execution e LEFT JOIN a USING(activity_id) WHERE e.reserved_discount_minor<>coalesce(a.reserved,0) OR e.consumed_discount_minor<>coalesce(a.consumed,0)",
    "activity_quantity_ledger": "WITH a AS (SELECT activity_id,sku_id,sum(qty) FILTER(WHERE status='reserved') reserved,sum(qty) FILTER(WHERE status='consumed') consumed FROM activity_order_allocation GROUP BY activity_id,sku_id) SELECT count(*) FROM activity_execution_line e LEFT JOIN a USING(activity_id,sku_id) WHERE e.reserved_qty<>coalesce(a.reserved,0) OR e.consumed_qty<>coalesce(a.consumed,0)",
    "activity_refund_events": "WITH r AS (SELECT allocation_id,sum(amount_minor) amount FROM activity_refund_event GROUP BY allocation_id) SELECT count(*) FROM activity_order_allocation a LEFT JOIN r USING(allocation_id) WHERE a.refund_minor<>coalesce(r.amount,0)",
    "allocation_order_state": "SELECT count(*) FROM activity_order_allocation a JOIN customer_order o USING(order_id) WHERE (a.status='reserved' AND o.trade_status<>'awaiting_payment') OR (a.status='consumed' AND o.paid_minor=0) OR (a.status='released' AND o.trade_status<>'cancelled_unpaid')",
}


def main():
    evidence, admin = EvidenceDB(), JointClient('shop_admin')
    report = {"integration_run_id": RUN, "captured_at": datetime.now(UTC).isoformat(),
              "boundary": "read-only guarded fixed test; all accumulated attempts retained", "queries": CHECKS}
    try:
        # 同一MVCC快照逐项勾稽；观测前等待已提交投递全部ACK。
        def pending():
            with evidence.engine.connect() as c:
                return c.scalar(text("SELECT count(*) FROM activity_delivery WHERE status<>'acked'"))
        report["unacked"] = wait_for(pending, lambda n: n == 0)
        with evidence.engine.connect() as c:
            c.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
            report["schema"] = c.scalar(text("SELECT version_num FROM alembic_version"))
            report["mismatches"] = {name: c.scalar(text(sql)) for name, sql in CHECKS.items()}
            report["counts"] = {name: c.scalar(text('SELECT count(*) FROM ' + name)) for name in (
                'customer_order','wallet_entry','order_payment','refund_execution','activity_execution',
                'activity_order_allocation','activity_refund_event','activity_delivery')}
            report["activity_ledger"] = [dict(row) for row in c.execute(text(
                "SELECT activity_id,intent_id,phase,variable_cap_minor,reserved_discount_minor,consumed_discount_minor,actual_revision,snapshot_id,watermark FROM activity_execution ORDER BY activity_id")).mappings()]
        # 绑定最近一次完整成功金样本，避免报告导出落在早期方案上。
        gold_file, gold = next((path, value) for path in sorted(OUT.glob('http-attempt-*.json'), reverse=True)
                              if (value := json.loads(path.read_text(encoding='utf-8'))).get('status') == 'completed')
        report['gold_evidence'] = gold_file.name
        plan = gold['flow']['plan']['plan_id']
        report['retrospective'] = admin.ops('POST','/retrospectives',{
            'plan_id':plan,'summary':'合成联调闭环完成；实际利润仍缺费用和成本冲回证据',
            'actions':['原01/02会话按最终指纹独立验收','后续单独进行真实模型与长时负载验证']})
        report['exports'] = []
        for format, suffix in [('markdown','md'),('csv','csv')]:
            response = admin.http.get(f'http://127.0.0.1:8120/api/ops/v1/plans/{plan}/export?format={format}')
            response.raise_for_status()
            value = response.text
            assert plan in value and gold['flow']['snapshot']['snapshot_id'] in value and gold['flow']['receipt']['activity_id'] in value
            target = OUT/('gold-full-export.'+suffix)
            target.write_text(value,encoding='utf-8')
            report['exports'].append({'path':target.name,'status':response.status_code,'bytes':target.stat().st_size,
                                      'contains_plan_activity_source_snapshot':True,
                                      'order_linkage':'gold HTTP evidence maps subsequent order; plan source snapshot predates this order'})
        # 保留已撤销cookie，在线身份必须即时失败，不能凭02旧身份缓存继续读。
        stale = JointClient('support_a')
        try:
            cookies = dict(stale.http.cookies)
            stale.cs('DELETE','/session')
            stale.http.cookies.update(cookies)
            response = stale.http.get('http://127.0.0.1:8120/api/ops/v1/context')
            assert response.status_code == 401
            report['logged_out_cookie_rejected'] = response.status_code
        finally:
            stale.close()
        assert not any(report['mismatches'].values()), report['mismatches']
        report['status'] = 'completed'
    except Exception as exc:
        report.update(status='failed',error_type=type(exc).__name__)
        raise
    finally:
        report['requests'] = admin.calls
        (OUT/'reconciliation.json').write_text(json.dumps(report,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
        evidence.engine.dispose()
        admin.close()
        print(json.dumps({'status':report['status'],'mismatches':report.get('mismatches')}))


if __name__ == '__main__':
    main()
