import re
from dataclasses import dataclass


@dataclass(frozen=True)
class PrivateContact:
    ref: str
    recipient: str
    phone: str
    province: str
    city: str
    district: str
    street: str
    postal_code: str | None = None


def support_contact(contact: PrivateContact) -> dict:
    phone_valid = re.fullmatch(r"1[3-9][0-9]{9}", contact.phone)
    province_valid = re.fullmatch(
        r"[\u4e00-\u9fff]{2,12}(省|自治区|特别行政区|市)", contact.province
    )
    city_valid = re.fullmatch(r"[\u4e00-\u9fff]{2,12}(市|自治州|地区|盟)", contact.city)
    region_valid = (
        province_valid
        and city_valid
        and contact.city.count("市") <= 1
        and not re.search(r"街|路|巷|县|号|栋|楼|\d", contact.province + contact.city)
    )
    return {
        "ref": contact.ref,
        "phone_masked": "*******" + contact.phone[-4:] if phone_valid else "****",
        "address_masked": contact.province + contact.city + "****" if region_valid else "****",
    }


def personal_contact(contact: PrivateContact) -> dict:
    # 只允许专用本人接口在完成owner鉴权后调用。
    return vars(contact).copy()
