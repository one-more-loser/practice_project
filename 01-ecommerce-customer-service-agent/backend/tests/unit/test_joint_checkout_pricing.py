from cs_agent.commerce.pricing import line_amounts, quote_amounts


def test_gold_direct_discount_is_applied_once_and_refund_allocation_matches():
    line = line_amounts(10000, 9200, 2)
    assert line == {"goods_minor": "20000", "discount_minor": "1600", "allocated_minor": "18400"}
    assert quote_amounts([line]) == (20000, 1600, 18400)
    assert divmod(int(line["allocated_minor"]), 2) == (9200, 0)


def test_regular_and_activity_lines_reconcile_to_single_payment():
    assert quote_amounts([line_amounts(10000, 9200, 2), line_amounts(3000, 3000, 1)]) == (23000, 1600, 21400)


def test_invalid_price_and_overflow_cannot_produce_a_charge():
    import pytest

    from cs_agent.core.errors import AppError

    for values in [(10000, 10001, 1), (10000, 0, 1), (10000, 9200, True), (2**63-1, 1, 2)]:
        with pytest.raises(AppError):
            line_amounts(*values)
