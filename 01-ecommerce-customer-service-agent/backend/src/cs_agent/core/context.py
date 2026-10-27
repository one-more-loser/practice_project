from dataclasses import dataclass
from enum import StrEnum


class Role(StrEnum):
    CUSTOMER = "customer"
    SUPPORT = "support"
    SUPERVISOR = "supervisor"
    SHOP_ADMIN = "shop_admin"
    KNOWLEDGE_ADMIN = "knowledge_admin"


@dataclass(frozen=True)
class TrustedContext:
    # 由服务端会话构造；不能使用body、查询参数或模型内容构造。
    subject_id: str
    store_id: str
    role: Role
    session_ref: str
    auth_version: int
    data_batch_id: str
    batch_generation: int
    request_id: str
