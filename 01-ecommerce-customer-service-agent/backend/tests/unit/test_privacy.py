import json

import pytest

from cs_agent.core.errors import AppError
from cs_agent.privacy.projections import PrivateContact, support_contact
from cs_agent.privacy.redaction import external_text, safe_summary

CONTACT = PrivateContact(
    "input_a", "合成顾客甲", "13800123456", "广东省", "深圳市", "南山区", "测试路88号"
)


def test_support_projection_is_allowlist_not_raw_object_serialization():
    result = support_contact(CONTACT)
    assert result == {
        "ref": "input_a",
        "phone_masked": "*******3456",
        "address_masked": "广东省深圳市****",
    }
    assert "district" not in result
    assert "recipient" not in result


def test_text_masks_phone_before_any_external_sink():
    assert external_text("请联系13800123456查物流") == "请联系*******3456查物流"


def test_address_policy_question_and_iso_dates_are_not_private_contact_input():
    assert external_text("订单可以修改地址吗？") == "订单可以修改地址吗?"
    assert external_text("查询2026-10-01到2026-10-22的订单") == "查询2026-10-01到2026-10-22的订单"


@pytest.mark.parametrize(
    "text",
    [
        "地址是深圳市南山区测试路88号",
        "送到工业园A栋三楼",
        "收货地址就在原来那里",
        "电话138 0012 3456",
    ],
)
def test_uncertain_address_or_obfuscated_phone_cannot_enter_model_or_log(text):
    with pytest.raises(AppError) as exc:
        external_text(text)
    assert exc.value.code == "private_input_required"


def test_audit_summary_is_recursive_allowlist_and_never_copies_payload():
    output = safe_summary(
        {
            "reason_code": "role_switched",
            "phone": CONTACT.phone,
            "body": {"address": CONTACT.street},
            "csrf": "SECRET",
            "version": 2,
        }
    )
    assert output == {"reason_code": "role_switched", "version": 2}
    assert "13800123456" not in json.dumps(output)


def test_one_valid_phone_does_not_hide_a_second_obfuscated_phone():
    with pytest.raises(AppError):
        external_text("联系13800123456，另一个139 0012 3457")


def test_bad_structured_region_or_short_phone_fails_closed_in_projection():
    from dataclasses import replace

    contact = replace(CONTACT, city="深圳市南山区测试路88号", phone="123")
    result = support_contact(contact)
    assert result["phone_masked"] == "****"
    assert result["address_masked"] == "****"
