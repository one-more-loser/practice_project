import re
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class MoneyMinor(ContractModel):
    currency: Literal["CNY"]
    amount_minor: str

    @field_validator("amount_minor")
    @classmethod
    def exact_minor(cls, value):
        if not re.fullmatch(r"0|[1-9][0-9]{0,18}", value) or int(value) > 2**63 - 1:
            raise ValueError("amount_minor must be a canonical nonnegative int64 string")
        return value


class OperationStatus(StrEnum):
    ACCEPTED = "accepted"
    PROCESSING = "processing"
    AWAITING_AGENT_REVIEW = "awaiting_agent_review"
    SUCCEEDED = "succeeded"
    REJECTED = "rejected"
    FAILED = "failed"
    RESULT_UNKNOWN = "result_unknown"


class OperationResult(ContractModel):
    operation_id: str
    status: OperationStatus
    business_ref: str | None = None
    version: int = 1
