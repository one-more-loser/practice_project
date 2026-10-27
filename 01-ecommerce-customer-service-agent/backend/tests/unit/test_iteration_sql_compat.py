"""03 common.py shape is a candidate DTO, not a complete HTTP service envelope."""
from tests.unit.test_sql_call_chain import execute, graph, install_candidate, result  # noqa: F401


def test_missing_alias_from_03_supports_order_clarification_continuation(graph):  # noqa: F811
    install_candidate(graph,result={"kind":"clarify","missing":["order_id"],"reason_code":"missing_order"})
    first = execute(graph,"2026年11月查那笔订单")
    assert result(first)["missing"] == ["order_id"]
    assert result(first)["missing_slots"] == ["order_id"]
    waiting = graph._assistant(first)
    install_candidate(graph,"SELECT order_id FROM cs_order_self WHERE order_id=:order_id AND created_at>=:start_at AND created_at<:end_at",["order_id","start_at","end_at"])
    second = execute(graph,"order_a",dialog=waiting["dialog_state"])
    assert "rows" in result(second)
    assert graph.calls[-1]["parameters"]["order_id"] == "order_a"
