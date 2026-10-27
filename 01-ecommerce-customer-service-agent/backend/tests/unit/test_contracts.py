import pytest
from pydantic import ValidationError

from cs_agent.core.types import MoneyMinor, OperationResult


@pytest.mark.parametrize("value", [1, 1.5, True, "01", "1.2", "1e2", "", "9223372036854775808"])
def test_money_rejects_noncanonical_or_overflow_minor(value):
    with pytest.raises(ValidationError):
        MoneyMinor(currency="CNY", amount_minor=value)


def test_money_keeps_large_integer_exact_for_browser():
    money = MoneyMinor(currency="CNY", amount_minor="9007199254740993")
    assert money.model_dump() == {"currency": "CNY", "amount_minor": "9007199254740993"}


def test_money_rejects_unsupported_currency_and_extra_fields():
    with pytest.raises(ValidationError):
        MoneyMinor(currency="USD", amount_minor="0")
    with pytest.raises(ValidationError):
        MoneyMinor(currency="CNY", amount_minor="0", customer_id="other")


def test_unknown_operation_status_cannot_become_success():
    with pytest.raises(ValidationError):
        OperationResult(operation_id="op_1", status="surprise")
