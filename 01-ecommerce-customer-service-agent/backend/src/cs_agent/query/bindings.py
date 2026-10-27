"""customer_self 的小型绑定适配：模型给提示，后端解析、验权并持有值。"""
import json
import re
from datetime import UTC, datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import or_, select

from cs_agent.actions.repository import fingerprint
from cs_agent.core.errors import AppError, not_found
from cs_agent.db.business_tables import orders, products, skus
from cs_agent.privacy.redaction import external_text
from cs_agent.query.validator import date_range, rejected

SHANGHAI = timezone(timedelta(hours=8), "Asia/Shanghai")
PARAMETER_TYPES = {"start_at":"TIMESTAMPTZ", "end_at":"TIMESTAMPTZ", "order_id":"TEXT", "sku_id":"TEXT", "status":"TEXT"}
STATUS_ALIASES = {"待付款":"awaiting_payment", "未支付":"awaiting_payment", "已付款":"paid", "已支付":"paid", "已完成":"completed", "未发货":"unshipped", "运输中":"in_transit", "本人已收货":"received_by_customer", "承运签收":"carrier_signed"}
STATUSES = {"awaiting_payment", "paid", "completed", "cancelled_unpaid", "cancelled_refunded", "closed_refunded", "unshipped", "in_transit", "received_by_customer", "carrier_signed", "active", "frozen", "initialization", "topup", "payment", "refund"}
_TIME_HINT = re.compile(
    r"\d{4}[-年/]|\d+[月日号天]|季度|半年|最近|时间范围"
    r"|(?:上|本|下|这)(?:周|个月|月|年)|前天|后天|明天|昨天|今天|前年|去年|今年|明年|后年"
    r"|月初|月中|月底|月末|年初|年底|年末|周末|工作日"
    r"|上午|下午|中午|凌晨|晚上|傍晚|清晨|早上|春节|国庆"
)
# 有限的时间短语边界：先剥离中性连接词，再要求查询/业务词边界。
# 不能把中文 \b 当词边界，也不能在任意前后缀中消费“昨天/上月”。
_TIME_LINK = re.compile(r"(?:在|于|的|(?:创建|下单|支付|发生|订单)?(?:时间|日期)|为|是)\s*$")
_QUERY_WORD = r"查询|查看|查一下|查|统计|看看|看|列出|筛选|显示"
_BUSINESS_WORD = (
    r"订单|交易|退款|流水|商品|金额|数量|状态|余额"
    r"|未支付|已支付|未付款|已付款|已完成|未发货|已发货|取消"
)
_TIME_LEFT = re.compile(r"(?:" + _QUERY_WORD + "|" + _BUSINESS_WORD + r"|[，,。；;：:！？!?（(])$")
_TIME_RIGHT = re.compile(
    r"^(?:" + _QUERY_WORD + "|(?:不是|并非|排除|不包括)?(?:" + _BUSINESS_WORD + ")"
    r"|创建|下单|支付|发生|发货|收到|sku_|吗|呢|[，,。；;：:！？!?）)])"
)


def _time_boundaries(question, spans):
    """只接受无修饰的时间原子；未知邻接词不自动降格为已知原子。"""
    left = question[:spans[0].start()].rstrip()
    while match := _TIME_LINK.search(left):
        left = left[:match.start()].rstrip()
    if left and not _TIME_LEFT.search(left):
        raise ValueError
    right = re.sub(r"^\s*(?:的\s*)*", "", question[spans[-1].end():])
    if right and not _TIME_RIGHT.search(right):
        raise ValueError
    if len(spans) == 2 and not re.fullmatch(
        r"\s*(?:到|至|~|～)\s*", question[spans[0].end():spans[1].start()]
    ):
        raise ValueError


def clarification(missing, message="请补充查询条件", *, reason="missing_parameters"):
    return {"kind":"clarify", "message":message, "reason_code":reason,
            "missing_slots":sorted(missing), "clarify":{"question":message}}


def clean_clarification(candidate):
    """仅透传非执行字段，限制体积；绝不把候选SQL/模型参数值变成会话绑定。"""
    metadata = {"kind", "reason_code", "query_context_id", "contract_version", "schema_revision",
                "schema_digest", "metric_revision", "request_id", "code", "error_code",
                "model_identifier", "revision"}
    def clean(value, depth=0, key=None):
        if depth > 5:
            raise rejected("invalid_clarification")
        if isinstance(value, str):
            if key in metadata and re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value):
                return value
            return external_text(value)[:500]
        if isinstance(value, list) and len(value) <= 20:
            return [clean(item, depth+1) for item in value]
        if isinstance(value, dict) and len(value) <= 16:
            return {str(k)[:50]:clean(v, depth+1, str(k)) for k,v in value.items()}
        if value is None or type(value) in {bool,int}:
            return value
        raise rejected("invalid_clarification")
    result = {key:clean(candidate[key], key=key) for key in ("kind", "message", "reason_code", "missing", "missing_slots", "clarify",
                "questions", "options", "query_context_id", "query_context_version",
                "contract_version", "schema_revision", "schema_digest", "metric_revision",
                "request_id", "code", "error_code", "error", "retryable", "service") if key in candidate}
    # 03候选使用missing；保留原字段，并归一为本地多轮绑定读取的missing_slots。
    if result.get("kind") == "clarify" and "missing" in result:
        missing = result["missing"]
        if (not isinstance(missing, list)
                or any(not isinstance(slot, str) for slot in missing)
                or ("missing_slots" in result and result["missing_slots"] != missing)):
            raise rejected("invalid_clarification")
        result.setdefault("missing_slots", missing)
    if len(json.dumps(result,ensure_ascii=False)) > 4000:
        raise rejected("invalid_clarification")
    fallback = {"clarify": "请补充查询的时间范围或业务条件",
                "unsupported": "暂不支持此类查询", "error": "查询服务暂时无法完成请求"}
    result.setdefault("message", fallback.get(result.get("kind"), "查询尚未执行"))
    return result


def scope_key(ctx):
    return fingerprint({key:getattr(ctx,key) for key in ("subject_id","store_id","role","session_ref","auth_version","data_batch_id","batch_generation")})


def clear_sql_state(dialog):
    for key in ("sql_binding", "sql_clarification", "sql_response"):
        dialog.pop(key, None)


def cancelled(question):
    return bool(re.fullmatch(r"(?:取消|算了|不查了|不用查了|停止查询|取消查询)[。！!\s]*", question.strip()))


def continues_query(question, clarification):
    """只有可识别的补槽/选项回答才能继承旧条件；新问题默认重新开始。"""
    if re.match(r"(?:改为|改成|换成|只看|仅看|再加|补充|还是查)", question.strip()):
        return True
    if (re.fullmatch(r"[A-Za-z0-9_-]{1,80}", question.strip())
            and (re.search(r"[0-9_-]", question) or question.strip() in STATUSES)):
        return True
    # 日期和状态的纯回答不等同于带有新查询谓词的新主题。
    if not re.search(r"查询|统计|帮我|看看|查一下|查我的|查订单|查商品|查退款|查余额", question):
        if re.search(r"\d{4}[-年]|\d+月|本月|上月|今天|昨天|近\d+天|订单|sku_", question, re.I):
            return True
        if any(alias in question for alias in STATUS_ALIASES):
            return True
    containers = [clarification, clarification.get("clarify", {})]
    for container in containers:
        if isinstance(container, dict):
            for option in container.get("options", []):
                if isinstance(option, dict) and question.strip() in {option.get("value"), option.get("label")}:
                    return True
    return False


def explicit_window(question, now):
    """有日期意图但不在支持语法内时澄清，绝不悄悄采用最近30天。"""
    local = now.astimezone(SHANGHAI)
    def month(year, number):
        start = datetime(year,number,1,tzinfo=SHANGHAI)
        end = datetime(year+(number==12),number%12+1,1,tzinfo=SHANGHAI)
        return start,end
    try:
        if re.search(r"之前|以前|之后|以后|截至|截止|早于|晚于|以来", question) and re.search(r"\d+月|\d{4}[-年/]|本月|上月|今天|昨天", question):
            raise ValueError
        relative = list(re.finditer(r"本月|上月|今天|昨天|(?:最近|近)\d+天", question))
        if len(relative) > 1 or (relative and re.search(r"20\d{2}[-年/]", question)):
            raise ValueError
        consumed = relative
        if re.search(r"20\d{2}/\d{1,2}/\d|不是.*月|排除.*月", question):
            raise ValueError
        if re.search(r"\d{1,2}:\d{2}|\d+[点时]|UTC|GMT|本月.*上月|上月.*本月",question,re.I):
            raise ValueError
        days = list(re.finditer(r"(?<!\d)(20\d{2})[-年](\d{1,2})[-月](\d{1,2})(?:日|号)?(?!\d)",question))
        months = list(re.finditer(r"(?<!\d)(20\d{2})(?:年|[-/])(\d{1,2})(?:月)?(?![\d-])",question))
        if days:
            consumed = days
            if len(days)>2 or (len(days)==2 and not re.search(r"到|至|~|～",question[days[0].end():days[1].start()])):
                raise ValueError
            dates = [datetime(*map(int,m.groups()),tzinfo=SHANGHAI) for m in days]
            start,end = dates[0],dates[-1]+timedelta(days=1)
        elif len(months)==1:
            consumed = months
            start,end = month(*map(int,months[0].groups()))
            # 年月区间等未实现语法不能只取第一个月。
            if re.search(r"到|至|~|～", question[months[0].end():]):
                raise ValueError
        elif len(months)>1:
            raise ValueError
        elif "本月" in question or "上月" in question:
            year,number = local.year,local.month
            if "上月" in question:
                year,number = year-(number==1),(number-2)%12+1
            start,end = month(year,number)
        elif "今天" in question or "昨天" in question:
            start = local.replace(hour=0,minute=0,second=0,microsecond=0)
            start -= timedelta(days=int("昨天" in question))
            end = start+timedelta(days=1)
        elif match := re.search(r"(?:最近|近)(\d+)天",question):
            start,end = now-timedelta(days=int(match[1])),now
        elif _TIME_HINT.search(question):
            raise ValueError
        else:
            return None
        _time_boundaries(question, consumed)
        # 各分支只消费实际解析的时间片段，再统一检查残余；识别了一个日期
        # 不代表可以忽略“前天”等第二范围，也不能吞掉未识别的区间端点。
        remainder = question
        for match in reversed(consumed):
            remainder = remainder[:match.start()] + remainder[match.end():]
        if _TIME_HINT.search(remainder):
            raise ValueError
        if len(consumed) == 1 and re.match(
            r"\s*(?:到(?!账|货|达)|至|~|～)", question[consumed[0].end():]
        ):
            raise ValueError
        window = date_range(now,start,end)
        return {key:value.astimezone(UTC) for key,value in window.items()}
    except (ValueError, OverflowError, AppError):
        return clarification(["start_at","end_at"],"请给出明确年月、日期或起止日期，范围不超过366天",reason="invalid_or_ambiguous_time")


def prepare_slots(session, ctx, question, hints, dialog, now):
    if not isinstance(hints,dict) or set(hints)-{"order_ref","sku_id","status","timeframe"} or any(not isinstance(v,str) or not 0<len(v)<=120 for v in hints.values()):
        raise rejected("invalid_parameters")
    key = scope_key(ctx)
    saved = dialog.get("sql_binding",{})
    pending = dialog.get("sql_clarification")
    if (not pending or saved.get("scope") != key
            or not continues_query(question, pending)):
        saved = {}
        clear_sql_state(dialog)
    values = dict(saved.get("values",{}))
    original = saved.get("question",question)
    history = list(saved.get("supplements", []))
    if saved and question != saved.get("last_input") and question != original:
        history = [*history, question[:1000]][-6:]
    dialog["sql_binding"] = {
        "scope": key, "values": values, "question": original[:2000],
        "supplements": history, "last_input": question,
        "context_id": saved.get("context_id") or "qc_" + uuid4().hex,
        "version": saved.get("version", 0) + int(question != saved.get("last_input")),
        "reference_now": now.isoformat(),
    }
    window = explicit_window(question,now)
    if window and window.get("kind") == "clarify":
        # 本轮明确否定/修改了时间，旧窗口不能留给下轮补其他槽时复用。
        # 保留原题、补充记录和非时间槽；重新确认合法时间后才能执行。
        values.pop("start_at", None)
        values.pop("end_at", None)
        return values, window, original
    if window:
        values.update({k:v.isoformat() for k,v in window.items()})
    # 订单/SKU提示必须出现在本次用户原话；后端保存的已核验值可在待澄清轮继续用。
    for slot, pattern in (("order_ref",r"(?:订单|单号)\s*[:：#]?\s*([A-Za-z0-9_-]+)"),("sku_id",r"(?<![A-Za-z0-9_])(sku_[A-Za-z0-9_-]+)")):
        matches = list(re.finditer(pattern,question,re.I))
        if len(matches)>1:
            return values,clarification(["order_id" if slot=="order_ref" else slot],"请确认本次查询的单个订单或SKU"),original
        match = matches[0] if matches else None
        hint = hints.get(slot)
        grounded = hint and re.search(r"(?<![A-Za-z0-9_-])"+re.escape(hint)+r"(?![A-Za-z0-9_-])",question)
        raw = hint if grounded else match[1] if match else None
        pending = dialog.get("sql_clarification",{}).get("missing_slots",[])
        if not raw and saved and slot=="order_ref" and "order_id" in pending and question.strip() not in STATUSES and not question.lower().startswith("sku_") and re.fullmatch(r"[A-Za-z0-9_-]{1,80}",question.strip()):
            raw = question.strip()
        if raw:
            if values.get(slot) and values[slot] != raw:
                # 改选业务资源后旧筛选条件作废，只保留本轮明确给出的时间。
                values.clear()
                if window:
                    values.update({k: v.isoformat() for k, v in window.items()})
            values[slot] = raw
    status_values = {value for alias,value in STATUS_ALIASES.items() if alias in question}
    status_values |= {value for value in STATUSES if re.search(r"(?<![A-Za-z_])"+value+r"(?![A-Za-z_])",question)}
    if len(status_values)>1:
        return values, clarification(["status"],"请确认要查询的单个状态"),original
    # “已支付订单数/金额”是四个已付状态的注册cohort，不等于trade_status=paid。
    # paid仅在用户明确声明状态时成为单值筛选；模型不能把cohort缩成单状态。
    if "paid" in status_values and not ("status" in dialog.get("sql_clarification", {}).get("missing_slots", []) and question.strip() == "paid") and not re.search(r"状态\s*(?:为|是|[:：=])\s*(?:已付款|已支付|paid)(?![A-Za-z_])", question):
        status_values.remove("paid")
    if status_values:
        if re.search(r"不是|并非|排除|不包括|除.*外",question):
            return values,clarification(["status"],"请明确要包含的状态，当前不自动推断否定条件"),original
        values["status"] = next(iter(status_values))
    # 每轮重新验权；不信任模型旧slots，也不信任上轮通过后已失效的业务记录。
    if reference := values.get("order_ref"):
        matches = session.execute(select(orders.c.order_id).where(
            or_(orders.c.order_id==reference,orders.c.display_no==reference),
            orders.c.owner_id==ctx.subject_id,orders.c.store_id==ctx.store_id,
            orders.c.data_batch_id==ctx.data_batch_id,orders.c.batch_generation==ctx.batch_generation,
        ).limit(2)).scalars().all()
        if len(matches)!=1:
            raise not_found()
        values["order_id"] = matches[0]
    if sku := values.get("sku_id"):
        match = session.execute(select(skus.c.sku_id).join(products,products.c.product_id==skus.c.product_id).where(
            skus.c.sku_id==sku,skus.c.store_id==ctx.store_id,products.c.store_id==ctx.store_id,
            skus.c.data_batch_id==ctx.data_batch_id,products.c.data_batch_id==ctx.data_batch_id,
            skus.c.batch_generation==ctx.batch_generation,products.c.batch_generation==ctx.batch_generation,
            skus.c.status=="active",products.c.status=="active",
        )).scalar_one_or_none()
        if match is None:
            raise not_found()
    return values,None,original


def bind_candidate(candidate, values, question, now):
    names = candidate.get("parameter_names")
    if not isinstance(names,list) or any(not isinstance(n,str) for n in names) or len(names)!=len(set(names)) or set(names)-PARAMETER_TYPES.keys():
        raise rejected("invalid_parameters")
    names = set(names)
    if {"start_at","end_at"}&names and not {"start_at","end_at"}<=names:
        raise rejected("invalid_range")
    ignored = (set(values) & {"order_id", "sku_id", "status"}) - names
    if ignored:
        return None, clarification(ignored, "已确认的条件尚未落实到查询，请保留这些条件后重试", reason="candidate_ignored_conditions")
    if "start_at" in values and not {"start_at","end_at"}<=names:
        return None,clarification(["start_at","end_at"],"本次指定的日期尚未落实到查询，请保留该日期范围后重试",reason="candidate_ignored_time")
    bound = {key:values[key] for key in names if key in values}
    if {"start_at", "end_at"} <= names and not {"start_at", "end_at"} <= values.keys():
        return None, clarification(names - bound.keys(), "请确认查询时间范围及缺少的条件，例如本月或最近30天")
    if {"start_at","end_at"}<=names:
        window = date_range(now,datetime.fromisoformat(values["start_at"]) if "start_at" in values else None,datetime.fromisoformat(values["end_at"]) if "end_at" in values else None)
        bound.update(window)
    missing = names-bound.keys()
    if missing:
        return None,clarification(missing,"请补充并确认订单编号、SKU或状态")
    return bound,None
