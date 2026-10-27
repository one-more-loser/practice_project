"""声明性处置流程只指导读取/澄清；业务授权不在Skill中配置。"""

from pydantic import Field

from cs_agent.core.types import ContractModel

POLICY_REVISION = "REQ-CS-v0.2"
SUITE_REVISION = "CS-SKILL-GUARD-v1"
TOOLS = {
    "catalog.read",
    "orders.list",
    "orders.detail",
    "shipments.list",
    "wallet.summary",
    "knowledge.search",
    "sql.query",
}
TOPICS = {"general", "order", "aftersale", "transport", "complaint", "knowledge_gap"}


class Procedure(ContractModel):
    conditions: list[str] = Field(min_length=1, max_length=10)
    exclusions: list[str] = Field(min_length=1, max_length=10)
    verification_steps: list[str] = Field(min_length=1, max_length=12)
    resolution_summary: str = Field(min_length=1, max_length=1000)
    allowed_tools: list[str] = Field(default_factory=list, max_length=7)
    handoff_conditions: list[str] = Field(min_length=1, max_length=8)


def eligible(*, role, published, source_active, current_policy, expires, now):
    return (
        role == "agent"
        and published
        and source_active
        and current_policy == POLICY_REVISION
        and (expires is None or expires > now)
    )


def tool_permitted(name):
    # 处置Skill不能授予改址/支付/退款/审批，提案工具也不得由Skill新增。
    return name in TOOLS


def run_guards(definition, now):
    """独立反例集由服务端执行，不接受前端上传passed=true作为回归证据。"""
    trials = [
        (
            "active_agent",
            dict(
                role="agent",
                published=True,
                source_active=True,
                current_policy=POLICY_REVISION,
                expires=None,
                now=now,
            ),
            True,
        ),
        (
            "customer_cannot_read_internal",
            dict(
                role="customer",
                published=True,
                source_active=True,
                current_policy=POLICY_REVISION,
                expires=None,
                now=now,
            ),
            False,
        ),
        (
            "candidate_is_not_active",
            dict(
                role="agent",
                published=False,
                source_active=True,
                current_policy=POLICY_REVISION,
                expires=None,
                now=now,
            ),
            False,
        ),
        (
            "revoked_source",
            dict(
                role="agent",
                published=True,
                source_active=False,
                current_policy=POLICY_REVISION,
                expires=None,
                now=now,
            ),
            False,
        ),
        (
            "obsolete_policy",
            dict(
                role="agent",
                published=True,
                source_active=True,
                current_policy="old",
                expires=None,
                now=now,
            ),
            False,
        ),
        (
            "expired",
            dict(
                role="agent",
                published=True,
                source_active=True,
                current_policy=POLICY_REVISION,
                expires=now,
                now=now,
            ),
            False,
        ),
    ]
    results = [
        {"id": name, "passed": eligible(**params) is expected} for name, params, expected in trials
    ]
    for tool in ["refund.execute", "wallet.topup", "identity.switch", "shell.execute"]:
        results.append({"id": "deny_" + tool, "passed": not tool_permitted(tool)})
    results.append(
        {
            "id": "candidate_tool_subset",
            "passed": all(tool_permitted(tool) for tool in definition["allowed_tools"]),
        }
    )
    return results
