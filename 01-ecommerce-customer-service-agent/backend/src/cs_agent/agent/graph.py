import json
import time
from typing import Literal

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, StateGraph
from pydantic import Field, ValidationError

from cs_agent.agent.facts import render_facts, select_policy_evidence
from cs_agent.agent.state import AgentState
from cs_agent.catalog.service import CatalogService
from cs_agent.core.context import Role
from cs_agent.core.errors import AppError
from cs_agent.core.types import ContractModel
from cs_agent.identity.policies import require_role
from cs_agent.knowledge.service import KnowledgeService
from cs_agent.query.bindings import (
    PARAMETER_TYPES,
    bind_candidate,
    cancelled,
    clean_clarification,
    clear_sql_state,
    continues_query,
    prepare_slots,
    scope_key,
)
from cs_agent.query.schema import DIGEST, REVISION
from cs_agent.query.service import QueryService
from cs_agent.query.validator import rejected, validate_parameters, validate_query
from cs_agent.runtime.leases import Lease, RuntimeService


class OrdersListArgs(ContractModel):
    limit: int = Field(default=20, ge=1, le=50)
    offset: int = Field(default=0, ge=0, le=10000)


class OrderArgs(ContractModel):
    order_id: str = Field(min_length=1, max_length=80)


class WalletArgs(ContractModel):
    limit: int = Field(default=20, ge=1, le=50)


class KnowledgeArgs(ContractModel):
    query: str = Field(min_length=1, max_length=1000)


class SQLArgs(ContractModel):
    question: str = Field(min_length=1, max_length=2000)
    # 仅为模型提取提示；后端还会与用户原话和当前身份核对，不能传任意绑定值。
    slots: dict[str, str] = Field(default_factory=dict)


class CatalogArgs(ContractModel):
    product_id: str | None = Field(default=None, min_length=1, max_length=80)
    query: str = Field(default="", max_length=160)


class ProposalArgs(ContractModel):
    action: Literal[
        "order.cancel",
        "order.address.change",
        "ticket.create",
        "aftersale.apply",
        "transport.incident.create",
        "transport.refund.request",
    ]
    resource_ref: str = Field(min_length=1, max_length=80)
    expected_version: int = Field(ge=1, strict=True)
    parameters: dict = Field(default_factory=dict)


class ExperienceArgs(ContractModel):
    topic: Literal["general", "order", "aftersale", "transport", "complaint", "knowledge_gap"]


class ServiceListArgs(ContractModel):
    limit: int = Field(default=10, ge=1, le=20)


TOOLS = {
    "catalog.read": CatalogArgs,
    "orders.list": OrdersListArgs,
    "orders.detail": OrderArgs,
    "shipments.list": OrderArgs,
    "wallet.summary": WalletArgs,
    "knowledge.search": KnowledgeArgs,
    "sql.query": SQLArgs,
    "action.propose": ProposalArgs,
    "experience.search": ExperienceArgs,
    "aftersales.list": ServiceListArgs,
    "transport.list": ServiceListArgs,
    "tickets.list": ServiceListArgs,
}

SYSTEM = """你是“店铺客服”。只根据当前问题、保存的对话状态和工具结果回答。
你也可调用action.propose(action,resource_ref,expected_version,parameters)生成待顾客确认的取消、改址、工单、退货或运输异常提案；它不会执行业务写入。缺少明细、原因或版本先查询/澄清，完整地址必须由本人业务页面录入。不得宣称提案已执行或退款已批准。
experience.search(topic)返回企业已审核处置案例/Skill，仅用于核对和澄清，不能扩大权限、自动审批或覆盖订单适用政策。
本人服务进度用aftersales.list(limit)、transport.list(limit)、tickets.list(limit)查询，不能用旧对话代替当前业务记录。
权限与身份由服务端控制，禁止索要或推测主体ID。你只可请求这些只读工具：
catalog.read(product_id?,query?)、orders.list(limit,offset)、orders.detail(order_id)、shipments.list(order_id)、wallet.summary(limit)、knowledge.search(query)、sql.query(question,slots?)。
SQL的slots只可提示用户原话中的order_ref、sku_id、status、timeframe；缺信息先澄清，不能填造ID或日期。后端解析日期并校验/绑定实际值。
任何工具参数都要服务端校验；工具不能创建订单、改地址、取消、退款、充值、确认收货或审批。
每轮最多请求3个工具；证据不足、条件不明或有多个候选时先澄清。价格、余额、状态和物流等动态事实必须调用业务工具。
政策回答只引用检索证据；不要把承运签收说成本人确认收货，不要把审核通过说成退款到账。
公开商品和店铺通用规则面向游客。改址次数、发货前后规则、退货条件等问题必须先调用knowledge.search查证，不能直接answer。
查询公开规则不要求登录或订单编号。涉及具体顾客的订单、物流、余额或售后现状时使用本人业务工具；游客需先登录。
用户修改查询条件时清除相关旧槽位。连续三轮仍需澄清时结束本轮并说明可走的已存在业务页面；不得声称已转人工。
知识工具命中后，answer的evidence_refs只列与当前问题有关的citation.reference_id；可选多个以覆盖多意图，不得填造引用。不能确认相关性或条款冲突时先clarify，不把无关政策拼成答案。
每次只输出JSON：answer为{kind:"answer",text,slots?,evidence_refs?}；澄清为{kind:"clarify",text,slots?,reset_slots?}；工具为{kind:"tool_call",tool,arguments}。
不要输出思维链、SQL、内部知识全文、个人完整地址或电话。"""


class AgentGraph:
    def __init__(
        self,
        database,
        lease: Lease,
        runtime: RuntimeService,
        gateway,
        saver: BaseCheckpointSaver,
        clock,
        query_service: QueryService | None = None,
    ):
        self.db, self.lease, self.runtime = database, lease, runtime
        self.gateway, self.saver, self.clock = gateway, saver, clock
        self.query_service = query_service or QueryService()
        self.knowledge = KnowledgeService()
        self.deadline = time.monotonic() + 20

    def _guard(self):
        return self.runtime.guard(self.db, self.lease, self.clock)

    def _assistant(self, state: AgentState):
        dialog = dict(state.get("dialog_state", {}))
        latest = next((str(item.content) for item in reversed(state.get("messages", []))
                       if isinstance(item, HumanMessage)), "")
        with self._guard() as (session, (_, _, principal)):
            if not state.get("tool_results"):
                if cancelled(latest):
                    return {"answer": "已取消本次查询。", "waiting": False,
                            "actions": [], "dialog_state": {}}
                saved = dialog.get("sql_binding", {})
                if saved and (principal.context is None or saved.get("scope") != scope_key(principal.context)):
                    clear_sql_state(dialog)
                pending = dialog.get("sql_clarification")
                if pending and continues_query(latest, pending):
                    return {"actions": [{"name": "sql.query", "arguments": {"question": latest}}],
                            "dialog_state": dialog}
                clear_sql_state(dialog)
                latest = next(
                    (
                        str(item.content)
                        for item in reversed(state.get("messages", []))
                        if isinstance(item, HumanMessage)
                    ),
                    "",
                )
                product_ids = self.knowledge._resolve_product(session, principal, latest)
                if len(product_ids) <= 1:
                    visibility = ["public"] if principal.kind == "guest" else ["public", "customer"]
                    cached = self.knowledge._faq_match(
                        session,
                        principal,
                        latest,
                        visibility,
                        self.clock.now(),
                        next(iter(product_ids), None),
                    )
                    if cached:
                        results = [{"name": "knowledge.search", "result": cached}]
                        return {
                            "answer": render_facts(results),
                            "actions": [],
                            "waiting": False,
                            "tool_results": results,
                            "citations": [item["citation"] for item in cached["items"]],
                            "dialog_state": {"slots": {}, "clarification_rounds": 0},
                        }
        if time.monotonic() >= self.deadline:
            raise AppError("turn_budget_exceeded", 503, "本轮处理时间已到")
        slots = state.get("dialog_state", {}).get("slots", {})
        # SQL澄清由后端直接交付，避免再经聊天模型时丢失槽位/选项或变成最终回答。
        for item in reversed(state.get("tool_results", [])):
            if item["name"] == "sql.query" and item["result"].get("kind") in {"unsupported", "error"}:
                response = clean_clarification(item["result"])
                clear_sql_state(dialog)
                dialog["sql_response"] = response
                return {"answer": response["message"], "waiting": False, "actions": [],
                        "dialog_state": dialog}
            if item["name"] == "sql.query" and item["result"].get("kind") == "clarify":
                clarify = clean_clarification(item["result"])
                rounds = dialog.get("clarification_rounds", 0) + 1
                if rounds >= 3:
                    return {"answer":"请从本人订单页面核对必要条件后再查询。当前查询尚未执行。",
                            "waiting":False,"actions":[],"dialog_state":{}}
                dialog.update(sql_clarification=clarify, clarification_rounds=rounds)
                return {"answer":clarify["message"],"waiting":True,"actions":[],
                        "dialog_state":dialog}
        for result in state.get("tool_results", []):
            if result["name"] == "knowledge.search" and not result["result"].get("items"):
                return {
                    "answer": "目前没有可用的已发布资料支持这个回答，暂时不能确认。请补充商品或政策条件后再试。",
                    "waiting": False,
                    "actions": [],
                    "dialog_state": {"slots": slots, "clarification_rounds": 0},
                }
        message = self.gateway.invoke(
            "chat",
            {
                "messages": [
                    {
                        "role": "system",
                        "content": SYSTEM
                        + (
                            "\n当前访问身份：游客。可查公开商品和政策；本人订单、物流、余额、售后须先登录。不要向游客索要订单编号来替代登录。"
                            if principal.kind == "guest"
                            else "\n当前访问身份：已登录顾客。本人业务范围由后端确定，不得推测或索要主体ID。"
                        )
                        + "\n已保存的澄清条件："
                        + json.dumps(slots, ensure_ascii=False),
                    },
                    *[
                        {
                            # JSON工具协议的返回值是后端数据，不冒充模型先前的回答，
                            # 也不提升为可覆盖系统规则的指令。
                            "role": "user"
                            if isinstance(item, (HumanMessage, SystemMessage))
                            else "assistant",
                            "content": str(item.content),
                        }
                        for item in state.get("messages", [])
                    ],
                ]
            },
            remaining=max(0.1, self.deadline - time.monotonic()),
        )
        action = self._parse(message)
        if action["kind"] == "tool":
            if state.get("tool_rounds", 0) + len(action["calls"]) > 3:
                return {
                    "answer": "本轮查询步骤已达上限。请缩小查询范围后再试。",
                    "waiting": False,
                    "actions": [],
                    "dialog_state": {"slots": slots, "clarification_rounds": 0},
                }
            return {"actions": action["calls"], "dialog_state": dialog}
        text = self._safe_answer(action["text"])
        next_slots = self._slots(slots, action.get("slots", {}), action.get("reset_slots", False))
        rounds = state.get("dialog_state", {}).get("clarification_rounds", 0)
        if action["kind"] == "clarify":
            rounds += 1
            if rounds >= 3:
                text = "为避免猜测，本轮先到这里。请补充必要信息，或从本人订单页面使用对应业务入口。当前没有完成转人工。"
                return {
                    "answer": text,
                    "waiting": False,
                    "actions": [],
                    "dialog_state": {"slots": {}, "clarification_rounds": 0},
                }
            text = (
                "请补充商品或政策条件；查询本人订单、物流、余额或售后请先登录。"
                if principal.kind == "guest"
                else "请补充商品名称、订单编号或查询时间范围，帮助我确定本次查询。"
            )
            return {
                "answer": text,
                "waiting": True,
                "actions": [],
                "dialog_state": {"slots": next_slots, "clarification_rounds": rounds},
            }
        chosen, citations = select_policy_evidence(
            state.get("tool_results", []), action.get("evidence_refs")
        )
        if chosen is None:
            rounds += 1
            completed = render_facts([tool for tool in state.get("tool_results", [])
                                      if tool["name"] != "knowledge.search"])
            prompt = "现有证据不足以确定适用条款，请明确商品或政策主题；当前未确认资格或承诺退款。"
            body = (completed + "\n" if completed else "") + prompt
            if len(body) > 4000:
                body = "部分业务结果较长，请缩小查询范围或从本人业务页面核对。" + prompt
            return {
                "answer": body,
                "waiting": rounds < 3, "actions": [], "citations": [],
                "dialog_state": {"slots": next_slots if rounds < 3 else {},
                                 "clarification_rounds": rounds if rounds < 3 else 0},
            }
        facts = render_facts(chosen)
        # 未读取事实时，模型只能给出澄清/引导，不能自行承诺商品政策或账户状态。
        text = facts or "您好，请告诉我商品名称、订单或需要咨询的政策；查询本人信息请先登录。"
        return {
            "answer": text,
            "waiting": False,
            "actions": [],
            "citations": self._unique_citations(citations),
            "dialog_state": {"slots": next_slots, "clarification_rounds": 0},
        }

    @staticmethod
    def _parse(message):
        if getattr(message, "tool_calls", None):
            calls = message.tool_calls
            if any(
                call.get("name") not in TOOLS or not isinstance(call.get("args"), dict)
                for call in calls
            ):
                raise AppError("agent_protocol_error", 503, "客服流程响应不符合契约")
            return {
                "kind": "tool",
                "calls": [
                    {
                        "name": call["name"],
                        "arguments": call["args"],
                        "id": call.get("id") or f"call_{index}",
                    }
                    for index, call in enumerate(calls)
                ],
            }
        try:
            payload = json.loads(message.content)
            kind = payload.get("kind")
            if kind in {"answer", "clarify"} and isinstance(payload.get("text"), str):
                return {
                    "kind": kind,
                    "text": payload["text"],
                    "slots": payload.get("slots", {}),
                    "reset_slots": payload.get("reset_slots", False),
                    "evidence_refs": payload.get("evidence_refs"),
                }
            if (
                kind == "tool_call"
                and payload.get("tool") in TOOLS
                and isinstance(payload.get("arguments"), dict)
            ):
                return {
                    "kind": "tool",
                    "calls": [
                        {"name": payload["tool"], "arguments": payload["arguments"], "id": "call_0"}
                    ],
                }
        except Exception:
            pass
        raise AppError("agent_protocol_error", 503, "客服流程响应不符合契约")

    @staticmethod
    def _safe_answer(value):
        if not value.strip() or len(value) > 4000:
            raise AppError("agent_protocol_error", 503, "客服流程响应不符合契约")
        from cs_agent.privacy.redaction import external_text

        return external_text(value).strip()

    @staticmethod
    def _slots(previous, updates, reset):
        if not isinstance(updates, dict) or len(updates) > 8 or not isinstance(reset, bool):
            raise AppError("agent_protocol_error", 503, "客服流程响应不符合契约")
        allowed = {"intent", "order_ref", "timeframe", "product_name", "sku_id", "status"}
        if set(updates) - allowed or any(
            not isinstance(value, (str, int, bool)) for value in updates.values()
        ):
            raise AppError("agent_protocol_error", 503, "客服流程响应不符合契约")
        base = {} if reset else dict(previous)
        for key, value in updates.items():
            base[key] = AgentGraph._safe_answer(str(value))[:120]
        return base

    def _execute(self, state: AgentState):
        results, citations = [], list(state.get("citations", []))
        dialog = dict(state.get("dialog_state", {}))
        for call in state.get("actions", []):
            name = call["name"]
            try:
                args = TOOLS[name].model_validate(call["arguments"]).model_dump()
                if name == "knowledge.search":
                    with self._guard() as (_, (_, _, principal)):
                        pass
                    result = self.knowledge.retrieve(
                        self.db,
                        principal,
                        args["query"],
                        self.gateway,
                        self.clock.now(),
                        clock=self.clock,
                    )
                    with self._guard():
                        pass
                    citations.extend(item["citation"] for item in result.get("items", []))
                elif name == "sql.query":
                    result = self._sql_query(args["question"], state=state,
                                             hints=args["slots"], dialog=dialog)
                else:
                    with self._guard() as (session, (_, _, principal)):
                        if name == "catalog.read":
                            items = CatalogService().read(
                                session, principal.store_id, limit=50, product_id=args["product_id"],
                                now=self.clock.now(),
                            )
                            query = args["query"].casefold()
                            items = [
                                item
                                for item in items
                                if not query
                                or any(
                                    query in value.casefold()
                                    for value in [
                                        item["name"],
                                        *(item["aliases"] or []),
                                        *[sku["sku_id"] for sku in item["skus"]],
                                    ]
                                )
                            ]
                            result = {"items": items[:10]}
                        elif principal.context is None:
                            result = {"error": "customer_login_required"}
                        elif name == "orders.list":
                            result = self.query_service.read(
                                session, principal.context, name, args, self.clock.now()
                            )
                        elif name == "orders.detail":
                            result = self.query_service.read(
                                session, principal.context, name, args, self.clock.now()
                            )
                        elif name == "shipments.list":
                            result = self.query_service.read(
                                session, principal.context, name, args, self.clock.now()
                            )
                        elif name == "wallet.summary":
                            result = self.query_service.read(
                                session, principal.context, name, args, self.clock.now()
                            )
                        elif name == "action.propose":
                            from cs_agent.actions.origin import propose_for_run

                            result = propose_for_run(
                                session, principal.context, self.lease, args, self.clock.now()
                            )
                        elif name in {"aftersales.list", "transport.list", "tickets.list"}:
                            if name == "aftersales.list":
                                from cs_agent.aftersales.service import AftersaleService

                                rows = AftersaleService().list(
                                    session, principal.context, limit=args["limit"]
                                )
                            elif name == "transport.list":
                                from cs_agent.transport.service import TransportService

                                rows = TransportService().list(
                                    session, principal.context, limit=args["limit"]
                                )
                            else:
                                from cs_agent.support.service import TicketService

                                rows = TicketService().list_owned(
                                    session, principal.context, self.clock.now()
                                )[: args["limit"]]
                            keys = {
                                "claim_id",
                                "incident_id",
                                "ticket_id",
                                "order_id",
                                "status",
                                "version",
                                "goods_minor",
                                "original_freight_minor",
                                "refund_claim",
                            }
                            result = {
                                "items": [
                                    {key: value for key, value in row.items() if key in keys}
                                    for row in rows
                                ]
                            }
                        elif name == "experience.search":
                            from cs_agent.experience.service import ExperienceService

                            result = ExperienceService().retrieve(
                                session, principal.context, args["topic"], self.clock.now()
                            )
                        else:
                            result = {"error": "unsupported_tool"}
            except ValidationError:
                result = {"error": "invalid_tool_arguments"}
                if name == "sql.query":
                    dialog.pop("sql_binding", None)
                    dialog.pop("sql_clarification", None)
            except AppError as exc:
                if exc.code in {"unauthenticated", "stale_execution"}:
                    raise
                result = {"error": exc.code}
                if name == "sql.query":
                    dialog.pop("sql_binding", None)
                    dialog.pop("sql_clarification", None)
            results.append({"tool_call_id": call.get("id", ""), "name": name, "result": result})
        messages = [
            SystemMessage(
                content="只讀工具結果："
                + json.dumps(item["result"], ensure_ascii=False, default=str)
            )
            for item in results
        ]
        return {
            "messages": messages,
            "tool_results": [*state.get("tool_results", []), *results],
            "citations": self._unique_citations(citations),
            "tool_rounds": state.get("tool_rounds", 0) + len(results),
            "actions": [],
            "dialog_state":dialog,
        }

    def _sql_query(self, question, *, state=None, hints=None, dialog=None):
        from langchain_core.messages import HumanMessage

        dialog = dialog if dialog is not None else {}
        user_text = next((str(m.content) for m in reversed((state or {}).get("messages", []))
                          if isinstance(m, HumanMessage)), question)
        if cancelled(user_text):
            clear_sql_state(dialog)
            return {"kind": "unsupported", "reason_code": "query_cancelled", "message": "已取消本次查询。"}
        now = self.clock.now()
        with self._guard() as (session, (_, _, principal)):
            ctx = principal.context
            if ctx is None or ctx.role != Role.CUSTOMER:
                clear_sql_state(dialog)
                return {"error": "customer_login_required"}
            values, clarify, original = prepare_slots(
                session, ctx, user_text, hints or {}, dialog, now
            )
        binding = dialog["sql_binding"]
        def contextual(response):
            response = clean_clarification(response)
            response.update(query_context_id=binding["context_id"], query_context_version=binding["version"])
            return response
        if clarify:
            clarify = contextual(clarify)
            dialog["sql_clarification"] = clarify
            return clarify
        candidate = self.gateway.invoke(
            "sql", {"question": "\n用户补充：".join([original, *binding["supplements"]]),
                    "parameter_types":PARAMETER_TYPES,
                    "query_context":{"reference_now":now.isoformat(),
                                     "query_context_id":binding["context_id"],
                                     "query_context_version":binding["version"],
                                     "business_timezone":"Asia/Shanghai",
                                     "confirmed_parameters":sorted(set(values)&PARAMETER_TYPES.keys()),
                                     "explicit_time_window": {k:values[k] for k in ("start_at","end_at") if k in values}}},
            remaining=max(0.1, self.deadline - time.monotonic())
        )
        if candidate["kind"] != "query":
            response = contextual(candidate)
            if candidate["kind"] == "clarify":
                dialog["sql_clarification"] = response
            else:
                clear_sql_state(dialog)
            return response
        parameters, clarify = bind_candidate(candidate,values,user_text,now)
        prepared = validate_query(candidate["sql"])
        if set(candidate["parameter_names"]) != prepared.parameters:
            raise rejected("invalid_parameters")
        if clarify:
            clarify = contextual(clarify)
            dialog["sql_clarification"] = clarify
            return clarify
        validate_parameters(prepared,parameters,now)
        with self._guard() as (session, (_, _, current)):
            require_role(current.context, Role.CUSTOMER)
            if scope_key(ctx) != scope_key(current.context):
                raise AppError("stale_execution",409,"查询身份已变化，请重新查询")
            # 等待模型期间订单/SKU可能已失效，执行前重新验证当前数据范围。
            current_values, changed, _ = prepare_slots(
                session,current.context,user_text,hints or {},dialog,now
            )
            rebound, missing = bind_candidate(candidate,current_values,user_text,now)
            if changed or missing or rebound != parameters:
                raise AppError("stale_execution",409,"查询条件已变化，请重新确认")
            result = self.query_service.execute_candidate(
                current.context,
                candidate["sql"],
                parameters,
                now,
                schema_revision=REVISION,
                schema_digest=DIGEST,
            )
        clear_sql_state(dialog)
        return result

    @staticmethod
    def _unique_citations(items):
        result, seen = [], set()
        for item in items:
            key = (item.get("kind"), item.get("reference_id"), item.get("chunk_id"))
            if key not in seen:
                result.append(item)
                seen.add(key)
        return result[:5]

    def compile(self):
        graph = StateGraph(AgentState)
        graph.add_node("assistant", self._assistant)
        graph.add_node("tools", self._execute)
        graph.set_entry_point("assistant")
        graph.add_conditional_edges(
            "assistant", lambda state: "tools" if state.get("actions") else END
        )
        graph.add_edge("tools", "assistant")
        return graph.compile(checkpointer=self.saver)


def history_messages(rows):
    output = []
    for row in rows:
        if row["source_role"] == "customer":
            output.append(HumanMessage(content=row["text"]))
        elif row["source_role"] == "assistant":
            output.append(AIMessage(content=row["text"]))
    return output[-24:]
