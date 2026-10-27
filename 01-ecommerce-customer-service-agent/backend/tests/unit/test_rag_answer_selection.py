"""Evidence choice in the existing chat turn; no actual model/index/database calls."""
import json
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage

from cs_agent.agent.facts import render_facts
from cs_agent.core.errors import AppError
from tests.unit.test_sql_call_chain import graph  # noqa: F401


def item(ref, text, chunk):
    return {"text": text, "citation": {"kind":"knowledge", "reference_id":ref,
            "chunk_id":chunk, "title":ref, "revision":1}}


def answer(runner, refs, extra_tools=()):
    items=[item("addr","发货前每单仅允许一次成功改址。","a1"),
           item("wallet","新账户初始余额为0，可模拟充值。","w1"),
           item("addr","已发货后不能改址；修改前仍须本人确认。","a2")]
    body={"kind":"answer","text":"模型自由生成的金额999元不能成为事实。"}
    if refs is not None:
        body["evidence_refs"]=refs
    runner.gateway=SimpleNamespace(invoke=lambda *a,**k:AIMessage(content=json.dumps(body)))
    return runner._assistant({"tool_results":[{"name":"knowledge.search","result":{"items":items}}, *extra_tools],
                             "citations":[v["citation"] for v in items]})


def test_selected_policy_preserves_all_its_conditions_and_only_its_citations(graph):  # noqa: F811
    result=answer(graph,["addr"])
    assert "一次成功改址" in result["answer"]
    assert "已发货后不能改址" in result["answer"]
    assert "本人确认" in result["answer"]
    assert "充值" not in result["answer"] and "999" not in result["answer"]
    assert {v["reference_id"] for v in result["citations"]}=={"addr"}
    assert {v["chunk_id"] for v in result["citations"]}=={"a1","a2"}


def test_multi_intent_evidence_selection_keeps_each_selected_policy(graph):  # noqa: F811
    result=answer(graph,["addr","wallet"])
    assert "一次成功改址" in result["answer"] and "模拟充值" in result["answer"]
    assert {v["reference_id"] for v in result["citations"]}=={"addr","wallet"}
    order={"display_no":"SIM-A","trade_status":"paid","fulfillment_status":"unshipped",
           "paid_minor":"1000","refunded_minor":"0"}
    pending=answer(graph,[],[{"name":"orders.detail","result":order}])
    assert pending["waiting"] and "订单SIM-A" in pending["answer"]
    assert "一次成功改址" not in pending["answer"] and pending["citations"]==[]


@pytest.mark.parametrize("refs",[None,[]])
def test_ambiguous_or_empty_evidence_selection_clarifies_without_dumping_all_policies(graph,refs):  # noqa: F811
    result=answer(graph,refs)
    assert result["waiting"] is True
    assert "充值" not in result["answer"]
    assert result["citations"]==[]


@pytest.mark.parametrize("refs",[["unknown"],"addr",["addr",12]])
def test_model_cannot_invent_or_malformed_evidence_references(graph,refs):  # noqa: F811
    with pytest.raises(AppError):
        answer(graph,refs)


def test_long_policy_is_not_silently_cut_before_an_important_exception():
    text="完整条款。"*1000+"仅在核验收退后才会退款。"
    result=render_facts([{"name":"knowledge.search","result":{"items":[item("return",text,"r1")]}}])
    assert "完整条款。完整条款。" not in result
    assert "引用" in result and "完整" in result
    assert len(result)<4000
