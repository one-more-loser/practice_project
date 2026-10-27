import re
import unicodedata

from cs_agent.core.errors import AppError


def external_text(text: str, *, max_length: int = 4000) -> str:
    # 保守识别只是第一层：下游尚未接入模型，不宣称能识别所有自然语言地址。
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"[\u200b-\u200f\ufeff]", "", text)
    # 问改址政策、填写查询日期不是提供收货资料；声明/疑似详细地址仍拦截。
    inspection = re.sub(r"\d{4}-\d{2}-\d{2}|\d{1,2}月\d{1,2}[日号]", "<date>", text)
    private = r"(?:地址)\s*(?:是|为|在|就在|就是|:|：)|送到|寄到|发到|收件人|收货人|门牌|街|巷|栋|楼|单元|\d号|路\s*[0-9一二三四五六七八九十]"
    if (
        len(text) > max_length
        or re.search(private, inspection)
        or re.search(
            r"sk-[A-Za-z0-9_-]{8,}|Bearer\s+\S+|(?:密码|口令|密钥)\s*[:：=是]\s*\S+", text, re.I
        )
    ):
        raise AppError("private_input_required", 422, "请在个人收货表单填写联系信息")
    text = re.sub(r"(?<!\d)1[3-9]\d{9}(?!\d)", lambda m: "*******" + m[0][-4:], text)
    inspection = re.sub(r"\d{4}-\d{2}-\d{2}|\d{1,2}月\d{1,2}[日号]", "<date>", text)
    if re.search(r"\d[\d\s+()\-]{8,}\d", inspection):
        raise AppError("private_input_required", 422, "请在个人收货表单填写联系信息")
    return text


def safe_summary(value):
    # 不允许把任意业务对象递归落日志；仅收录已定义的机器原因码/整数版本。
    output = {}
    if not isinstance(value, dict):
        return output
    for key in ("version", "auth_version", "batch_generation"):
        if type(value.get(key)) is int:
            output[key] = value[key]
    for key in ("from_role", "to_role"):
        if value.get(key) in {"customer", "support", "supervisor", "shop_admin", "knowledge_admin"}:
            output[key] = value[key]
    if value.get("reason_code") in {
        "role_switched",
        "session_created",
        "session_revoked",
        "access_denied",
        "personal_read",
    }:
        output["reason_code"] = value["reason_code"]
    return output
