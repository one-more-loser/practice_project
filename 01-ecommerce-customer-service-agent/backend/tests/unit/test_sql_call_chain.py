"""真实 Graph/绑定/网关边界，内存 SQLite 仅验证解析查询；不连接业务库。"""
import json
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from langchain_core.messages import HumanMessage
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from cs_agent.agent.graph import AgentGraph
from cs_agent.core.context import Role, TrustedContext
from cs_agent.core.errors import AppError
from cs_agent.models.gateway import ModelGateway, ServiceConfig
from cs_agent.privacy.redaction import external_text
from cs_agent.query.schema import DIGEST, METRICS_REVISION, REVISION
from cs_agent.query.service import QueryService
from cs_agent.query.validator import validate_query

NOW = datetime(2026, 12, 5, 4, tzinfo=UTC)
CTX = TrustedContext("cust_a", "store_a", Role.CUSTOMER, "session_a", 1, "batch_1", 1, "request_a")


@pytest.fixture
def graph():
    engine = create_engine("sqlite://")
    with engine.begin() as c:
        c.exec_driver_sql("CREATE TABLE customer_order(order_id TEXT,display_no TEXT,owner_id TEXT,store_id TEXT,data_batch_id TEXT,batch_generation INT,created_at TIMESTAMP)")
        c.exec_driver_sql("INSERT INTO customer_order VALUES ('order_a','SIM-A','cust_a','store_a','batch_1',1,'2026-11-10'),('order_b','SIM-B','cust_b','store_a','batch_1',1,'2026-11-10'),('order_old','SIM-OLD','cust_a','store_a','batch_old',1,'2026-11-10')")
        c.exec_driver_sql("CREATE TABLE product(product_id TEXT,store_id TEXT,data_batch_id TEXT,batch_generation INT,status TEXT)")
        c.exec_driver_sql("CREATE TABLE sku(sku_id TEXT,product_id TEXT,store_id TEXT,data_batch_id TEXT,batch_generation INT,status TEXT)")
        c.exec_driver_sql("INSERT INTO product VALUES ('p_a','store_a','batch_1',1,'active'),('p_b','store_b','batch_1',1,'active')")
        c.exec_driver_sql("INSERT INTO sku VALUES ('sku_a','p_a','store_a','batch_1',1,'active'),('sku_c','p_a','store_a','batch_1',1,'active'),('sku_b','p_b','store_b','batch_1',1,'active')")
    calls = []

    class Executor:
        def query(self, ctx, sql, parameters, now, **kwargs):
            validate_query(sql)
            calls.append({"ctx": ctx, "sql": sql, "parameters": parameters, "now": now})
            return {"rows": [{"n": 1}], "as_of": now.isoformat()}

    runner = AgentGraph.__new__(AgentGraph)
    runner.clock = SimpleNamespace(now=lambda: NOW)
    runner.deadline = time.monotonic()+20
    runner.query_service = QueryService(Executor())
    runner.ctx = CTX
    runner.calls = calls
    runner.knowledge = SimpleNamespace(_resolve_product=lambda *a:set(), _faq_match=lambda *a:None)

    @contextmanager
    def guard():
        with Session(engine) as session:
            yield session, (None, None, SimpleNamespace(context=runner.ctx, kind="customer"))

    runner._guard = guard
    yield runner
    engine.dispose()


def install_candidate(graph, sql=None, names=None, result=None):
    candidate = result or {"kind":"query", "sql":sql, "parameter_names":names or []}
    graph.requests = []

    class Gateway:
        def invoke(self, capability, payload, **kwargs):
            assert capability == "sql"
            graph.requests.append(payload)
            return candidate
    graph.gateway = Gateway()


def execute(graph, user, *, question=None, slots=None, dialog=None):
    return graph._execute({"messages":[HumanMessage(content=user)], "dialog_state":dialog or {},
        "actions":[{"name":"sql.query", "arguments":{"question":question or user, **({"slots":slots} if slots else {})}}]})


def result(output):
    return output["tool_results"][0]["result"]


def test_explicit_month_uses_user_text_not_model_rephrasing(graph):
    install_candidate(graph,"SELECT count(*) AS n FROM cs_order_self WHERE created_at>=:start_at AND created_at<:end_at",["start_at","end_at"])
    out = execute(graph,"查2026年11月创建的订单数",question="查最近30天的订单数")
    assert "rows" in result(out)
    assert graph.calls[0]["parameters"] == {"start_at":datetime(2026,10,31,16,tzinfo=UTC),"end_at":datetime(2026,11,30,16,tzinfo=UTC)}
    assert graph.requests[0]["query_context"]["business_timezone"] == "Asia/Shanghai"
    assert "cust_a" not in json.dumps(graph.requests[0],default=str)


@pytest.mark.parametrize("user,expected",[("昨天的订单",datetime(2026,12,3,16,tzinfo=UTC)),("2026-11-30的订单",datetime(2026,11,29,16,tzinfo=UTC))])
def test_shanghai_day_and_relative_clock(graph,user,expected):
    install_candidate(graph,"SELECT count(*) AS n FROM cs_order_self WHERE created_at>=:start_at AND created_at<:end_at",["start_at","end_at"])
    assert "rows" in result(execute(graph,user))
    assert graph.calls[0]["parameters"]["start_at"] == expected


def test_unrecognized_explicit_time_does_not_fall_back(graph):
    install_candidate(graph,"SELECT order_id FROM cs_order_self",[])
    assert result(execute(graph,"2026年第三季度的订单"))["kind"] == "clarify"
    assert graph.calls == []


def test_explicit_month_cannot_be_ignored_by_candidate(graph):
    install_candidate(graph,"SELECT count(*) AS n FROM cs_order_self",[])
    assert result(execute(graph,"2026年11月的订单"))["kind"] == "clarify"
    assert graph.calls == []


@pytest.mark.parametrize("reference",["order_a","SIM-A"])
def test_order_id_or_display_number_resolved_by_backend(graph,reference):
    install_candidate(graph,"SELECT order_id FROM cs_order_self WHERE order_id=:order_id",["order_id"])
    out = execute(graph,f"查订单{reference}",slots={"order_ref":reference})
    assert "rows" in result(out)
    assert graph.calls[0]["parameters"] == {"order_id":"order_a"}


@pytest.mark.parametrize("reference",["order_b","order_old","no_such_order"])
def test_order_scope_failures_are_indistinguishable(graph,reference):
    install_candidate(graph,"SELECT order_id FROM cs_order_self WHERE order_id=:order_id",["order_id"])
    out = execute(graph,f"查订单{reference}",slots={"order_ref":reference})
    assert result(out) == {"error":"not_found"}
    assert graph.calls == []


def test_sku_and_status_are_bound_after_scope_validation(graph):
    install_candidate(graph,"SELECT sku_id FROM cs_order_item_self WHERE sku_id=:sku_id",["sku_id"])
    assert "rows" in result(execute(graph,"查sku_a",slots={"sku_id":"sku_a"}))
    assert graph.calls[-1]["parameters"] == {"sku_id":"sku_a"}
    install_candidate(graph,"SELECT order_id FROM cs_order_self WHERE trade_status=:status",["status"])
    assert "rows" in result(execute(graph,"查状态为已付款订单"))
    assert graph.calls[-1]["parameters"] == {"status":"paid"}


def test_foreign_sku_and_model_invented_order_are_not_bound(graph):
    install_candidate(graph,"SELECT sku_id FROM cs_order_item_self WHERE sku_id=:sku_id",["sku_id"])
    assert result(execute(graph,"查sku_b",slots={"sku_id":"sku_b"})) == {"error":"not_found"}
    install_candidate(graph,"SELECT order_id FROM cs_order_self WHERE order_id=:order_id",["order_id"])
    assert result(execute(graph,"查我的订单",slots={"order_ref":"order_a"}))["kind"] == "clarify"
    assert graph.calls == []


def test_missing_slot_and_structured_clarify_reaches_waiting_state(graph):
    install_candidate(graph,"SELECT order_id FROM cs_order_self WHERE order_id=:order_id",["order_id"])
    out = execute(graph,"查这笔订单")
    assert result(out)["missing_slots"] == ["order_id"]
    final = graph._assistant(out)
    assert final["waiting"] is True
    assert final["dialog_state"]["sql_clarification"]["missing_slots"] == ["order_id"]
    # 下一轮本人补充，重新核验归属；不会把模型补造的ID当值。
    out2 = execute(graph,"order_a",dialog=final["dialog_state"])
    assert "rows" in result(out2)
    assert graph.calls[-1]["parameters"] == {"order_id":"order_a"}


def test_model_structured_clarify_survives_without_chat_rewrite(graph):
    clarify = {"kind":"clarify","message":"请选择退款范围", "missing_slots":["refund_scope"],
        "clarify":{"question":"查订单累计退款还是退款到账？","options":[{"value":"order","label":"订单累计退款"}]}}
    install_candidate(graph,result=clarify)
    out = execute(graph,"查退款")
    final = graph._assistant(out)
    assert final["waiting"]
    assert final["dialog_state"]["sql_clarification"]["clarify"]["options"] == clarify["clarify"]["options"]
    assert final["dialog_state"]["sql_clarification"]["clarify"]["question"] == external_text(clarify["clarify"]["question"])
    assert "请选择退款范围" in final["answer"]


@pytest.mark.parametrize("names,sql",[(["owner"],"SELECT order_id FROM cs_order_self WHERE order_id=:owner"),(["status"],"SELECT order_id FROM cs_order_self ORDER BY :status"),(["order_id","order_id"],"SELECT order_id FROM cs_order_self WHERE order_id=:order_id")])
def test_invalid_parameter_names_positions_and_duplicates_never_execute(graph,names,sql):
    install_candidate(graph,sql,names)
    assert "error" in result(execute(graph,"查状态为已付款订单order_a",slots={"order_ref":"order_a"}))
    assert graph.calls == []


def test_scope_change_drops_saved_binding(graph):
    install_candidate(graph,"SELECT order_id FROM cs_order_self WHERE order_id=:order_id AND sku_id=:sku_id",["order_id","sku_id"])
    # 使用合法SQL先保存已确认订单，再等待SKU。
    install_candidate(graph,"SELECT order_id FROM cs_order_item_self WHERE order_id=:order_id AND sku_id=:sku_id",["order_id","sku_id"])
    pending = execute(graph,"订单order_a",slots={"order_ref":"order_a"})["dialog_state"]
    graph.ctx = TrustedContext("cust_a","store_a",Role.CUSTOMER,"new_session",2,"batch_1",1,"request_b")
    out = execute(graph,"sku_a",slots={"sku_id":"sku_a"},dialog=pending)
    assert result(out)["kind"] == "clarify"
    assert "order_id" in result(out)["missing_slots"]
    assert graph.calls == []


def test_gateway_sends_optional_context_and_preserves_clarify():
    captured = []
    response = {"contract_version":"XCS-SQL-v0.1","kind":"clarify","schema_revision":REVISION,"schema_digest":DIGEST,"metric_revision":METRICS_REVISION,"message":"请选择订单","missing_slots":["order_id"],"clarify":{"question":"哪笔订单？"},"service":{"model_identifier":"fixture","revision":"v1"}}
    def transport(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200,json=response)
    config = ServiceConfig(base_url="https://model.test/v1",model="fixture",service_revision="v1",protocol="project-sql")
    gateway = ModelGateway({"sql":config},transport=httpx.MockTransport(transport))
    context = {"reference_now":NOW.isoformat(),"business_timezone":"Asia/Shanghai","confirmed_parameters":[]}
    out = gateway.invoke("sql",{"question":"查订单","parameter_types":{"order_id":"TEXT"},"query_context":context})
    assert captured[0]["query_context"] == context
    assert captured[0]["parameters"] == {"order_id":"TEXT"}
    assert out["clarify"] == response["clarify"]


@pytest.mark.parametrize("sql",["SELECT order_id FROM cs_order_self ORDER BY :order_id","SELECT :status AS s FROM cs_order_self","SELECT order_id FROM cs_order_self WHERE order_id=:status"])
def test_parameter_usage_is_values_only_and_field_matched(sql):
    with pytest.raises(AppError):
        validate_query(sql)


def pending_state(graph):
    install_candidate(graph, "SELECT order_id FROM cs_order_item_self WHERE order_id=:order_id AND sku_id=:sku_id", ["order_id", "sku_id"])
    return graph._assistant(execute(graph, "查订单order_a", slots={"order_ref": "order_a"}))["dialog_state"]


@pytest.mark.parametrize("user", ["取消", "不查了", "算了"])
def test_cancellation_clears_query_before_model(graph, user):
    dialog = pending_state(graph)
    graph.gateway = None
    out = graph._assistant({"messages": [HumanMessage(content=user)], "dialog_state": dialog})
    assert out["dialog_state"] == {} and not out["waiting"]


@pytest.mark.parametrize("user", ["查商品", "hello", "查询余额"])
def test_new_topic_clears_previous_query(graph, user):
    from langchain_core.messages import AIMessage
    dialog = pending_state(graph)
    seen = []
    class Gateway:
        def invoke(self, capability, payload, **kwargs):
            seen.append(capability)
            return AIMessage(content='{"kind":"answer","text":"hello"}')
    graph.gateway = Gateway()
    out = graph._assistant({"messages": [HumanMessage(content=user)], "dialog_state": dialog})
    assert "sql_binding" not in out["dialog_state"]
    assert seen == ["chat"]


def test_pending_continuation_routes_to_sql_without_chat(graph):
    dialog = pending_state(graph)
    out = graph._assistant({"messages": [HumanMessage(content="sku_a")], "dialog_state": dialog})
    assert out["actions"][0]["name"] == "sql.query"
    out2 = execute(graph, "sku_a", dialog=out["dialog_state"])
    assert "rows" in result(out2)
    assert graph.calls[-1]["parameters"] == {"order_id": "order_a", "sku_id": "sku_a"}


def test_three_supplements_reach_model_in_order(graph):
    sql = "SELECT order_id FROM cs_order_item_self WHERE order_id=:order_id AND sku_id=:sku_id AND created_at>=:start_at AND created_at<:end_at"
    # order_item has no created_at; use an allowed joined order query for time.
    sql = "SELECT i.order_id FROM cs_order_item_self i JOIN cs_order_self o ON i.order_id=o.order_id WHERE i.order_id=:order_id AND i.sku_id=:sku_id AND o.created_at>=:start_at AND o.created_at<:end_at"
    install_candidate(graph, sql, ["order_id", "sku_id", "start_at", "end_at"])
    first = execute(graph, "查这笔订单的商品")
    second = execute(graph, "order_a", dialog=first["dialog_state"])
    third = execute(graph, "sku_a", dialog=second["dialog_state"])
    fourth = execute(graph, "2026年11月", dialog=third["dialog_state"])
    assert "rows" in result(fourth)
    assert graph.requests[-1]["question"] == "查这笔订单的商品\n用户补充：order_a\n用户补充：sku_a\n用户补充：2026年11月"
    assert graph.requests[0]["query_context"]["query_context_id"] == graph.requests[-1]["query_context"]["query_context_id"]
    assert graph.requests[-1]["query_context"]["query_context_version"] == 4


@pytest.mark.parametrize("user", ["今天和昨天的订单", "2026/11/15的订单", "2026年11月最近3天的订单"])
def test_mixed_or_unimplemented_dates_clarify(graph, user):
    install_candidate(graph, "SELECT order_id FROM cs_order_self", [])
    assert result(execute(graph, user))["kind"] == "clarify"
    assert not graph.calls


def test_missing_time_requires_confirmation(graph):
    install_candidate(graph, "SELECT order_id FROM cs_order_self WHERE created_at>=:start_at AND created_at<:end_at", ["start_at", "end_at"])
    first = execute(graph, "查订单")
    assert result(first)["missing_slots"] == ["end_at", "start_at"]
    second = execute(graph, "最近30天", dialog=first["dialog_state"])
    assert "rows" in result(second)


@pytest.mark.parametrize("slot,question", [("order_id", "查订单order_a"), ("sku_id", "查sku_a"), ("status", "查状态为已付款订单")])
def test_candidate_cannot_drop_confirmed_conditions(graph, slot, question):
    install_candidate(graph, "SELECT order_id FROM cs_order_self", [])
    out = result(execute(graph, question))
    assert out["reason_code"] == "candidate_ignored_conditions"
    assert out["missing_slots"] == [slot]
    assert not graph.calls


@pytest.mark.parametrize("kind", ["unsupported", "error"])
def test_nonquery_protocol_fields_survive_graph_and_output(graph, kind):
    from cs_agent.runtime.leases import RuntimeService
    candidate = {"kind": kind, "reason_code": "metric_not_registered", "code": "E_QUERY", "retryable": False,
                 "request_id": "request_fixture", "schema_revision": REVISION, "metric_revision": METRICS_REVISION,
                 "error": {"code": "unsupported_metric"}, "service": {"model_identifier": "fixture", "revision": "v1"}}
    install_candidate(graph, result=candidate)
    final = graph._assistant(execute(graph, "查退款率"))
    response = final["dialog_state"]["sql_response"]
    assert response["kind"] == kind and response["error"] == candidate["error"]
    assert response["code"] == "E_QUERY" and response["retryable"] is False
    assert response["service"] == candidate["service"]
    assert not final["waiting"] and not graph.calls
    assert RuntimeService._clarification_data(final["dialog_state"], SimpleNamespace(context=CTX), False)["query_response"] == response


def test_model_context_contains_metric_rules_without_identity():
    captured = []
    response = {"contract_version": "XCS-SQL-v0.1", "kind": "unsupported", "schema_revision": REVISION,
                "schema_digest": DIGEST, "metric_revision": METRICS_REVISION, "service": {"model_identifier": "fixture", "revision": "v1"}}
    config = ServiceConfig(base_url="https://model.test/v1", model="fixture", service_revision="v1", protocol="project-sql")
    gateway = ModelGateway({"sql": config}, transport=httpx.MockTransport(lambda r: (captured.append(json.loads(r.content)) or httpx.Response(200, json=response))))
    gateway.invoke("sql", {"question": "订单数"})
    assert captured[0]["metric_definitions"]["orders.count"]["time_field"] == "created_at"
    assert "subject_id" not in json.dumps(captured[0])


@pytest.mark.parametrize("user,start,end", [
    ("2024年2月", datetime(2024, 1, 31, 16, tzinfo=UTC), datetime(2024, 2, 29, 16, tzinfo=UTC)),
    ("2026年12月", datetime(2026, 11, 30, 16, tzinfo=UTC), datetime(2026, 12, 31, 16, tzinfo=UTC)),
    ("2026-11-30至2026-11-30", datetime(2026, 11, 29, 16, tzinfo=UTC), datetime(2026, 11, 30, 16, tzinfo=UTC)),
])
def test_calendar_edges_are_aware_half_open(user, start, end):
    from cs_agent.query.bindings import explicit_window
    assert explicit_window(user, NOW) == {"start_at": start, "end_at": end}


@pytest.mark.parametrize("field,value", [("subject_id", "cust_b"), ("store_id", "store_b"),
    ("session_ref", "session_b"), ("auth_version", 2), ("data_batch_id", "batch_old"), ("batch_generation", 2)])
def test_each_scope_change_invalidates_pending_values(graph, field, value):
    from dataclasses import replace
    dialog = pending_state(graph)
    graph.ctx = replace(CTX, **{field: value})
    # 模型无权复活原order绑定；若新范围SKU不可访问，统一not_found。
    out = result(execute(graph, "sku_a", dialog=dialog))
    assert out.get("error") == "not_found" or "order_id" in out.get("missing_slots", [])
    assert not graph.calls


def test_selected_sku_change_discards_old_order_and_filters(graph):
    sql = "SELECT order_id FROM cs_order_item_self WHERE order_id=:order_id AND sku_id=:sku_id"
    install_candidate(graph, sql, ["order_id", "sku_id"])
    first = execute(graph, "查订单order_a和sku_a，状态为已付款")
    assert result(first)["reason_code"] == "candidate_ignored_conditions"
    second = execute(graph, "改为sku_c", dialog=first["dialog_state"])
    assert "order_id" in result(second)["missing_slots"]
    assert not graph.calls


@pytest.mark.parametrize("question", ["2026年11月之前的订单", "截至2026年11月的订单", "2026年11月和12月的订单"])
def test_unsupported_month_qualifiers_never_become_single_month(graph, question):
    install_candidate(graph, "SELECT order_id FROM cs_order_self WHERE created_at>=:start_at AND created_at<:end_at", ["start_at", "end_at"])
    out = result(execute(graph, question))
    assert out["kind"] == "clarify" and out["reason_code"] == "invalid_or_ambiguous_time"
    assert not graph.calls and not graph.requests


@pytest.mark.parametrize("phrase", ["已支付订单数", "已付款订单金额"])
def test_registered_paid_cohort_does_not_become_single_paid_status(graph, phrase):
    sql = "SELECT count(*) AS n FROM cs_order_self WHERE created_at>=:start_at AND created_at<:end_at AND trade_status IN ('paid','completed','cancelled_refunded','closed_refunded') AND paid_minor>0"
    install_candidate(graph, sql, ["start_at", "end_at"])
    out = execute(graph, "2026年11月" + phrase)
    assert "rows" in result(out)
    assert set(graph.calls[-1]["parameters"]) == {"start_at", "end_at"}



def test_sku_answer_is_not_reinterpreted_as_missing_order(graph):
    install_candidate(graph, "SELECT order_id FROM cs_order_item_self WHERE order_id=:order_id AND sku_id=:sku_id", ["order_id", "sku_id"])
    first = execute(graph, "查这笔订单的商品")
    second = execute(graph, "sku_a", dialog=first["dialog_state"])
    assert result(second)["missing_slots"] == ["order_id"]
    assert second["dialog_state"]["sql_binding"]["values"] == {"sku_id": "sku_a"}
    assert not graph.calls


def test_explicit_status_answer_continues_missing_status(graph):
    install_candidate(graph, "SELECT order_id FROM cs_order_self WHERE trade_status=:status", ["status"])
    first = execute(graph, "查订单状态")
    second = execute(graph, "paid", dialog=first["dialog_state"])
    assert "rows" in result(second)
    assert graph.calls[-1]["parameters"] == {"status": "paid"}
