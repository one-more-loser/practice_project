import hashlib
import json
import secrets
from datetime import datetime

from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from cs_agent.config import PROJECT_ROOT, Settings
from cs_agent.db.models import RoleGrant, ShippingInput, SourceBatch, Store, Subject
from cs_agent.db.session import verify_target
from cs_agent.identity.security import hash_password

BATCH = "sim_b0_20261022"
STORE = "store_sim"
SOURCE_TIME = datetime.fromisoformat("2026-10-22T00:00:00+00:00")


def seed(url: str):
    engine = create_engine(url, hide_parameters=True)
    verify_target(engine, Settings(expected_database=make_url(url).database), migration=True)
    manifest = json.loads(
        (PROJECT_ROOT / "data/synthetic/manifest.json").read_text(encoding="utf-8")
    )
    contacts = [
        {
            "input_id": "shipping_sim_a",
            "owner_id": "customer_a",
            "recipient": "合成顾客甲",
            "phone": "13800123456",
            "province": "广东省",
            "city": "深圳市",
            "district": "南山区",
            "street": "合成测试路88号",
            "postal_code": "518000",
        },
        {
            "input_id": "shipping_sim_b",
            "owner_id": "customer_b",
            "recipient": "合成顾客乙",
            "phone": "13900123457",
            "province": "浙江省",
            "city": "杭州市",
            "district": "西湖区",
            "street": "合成测试路99号",
            "postal_code": None,
        },
    ]
    fingerprint = hashlib.sha256(
        json.dumps(
            {"manifest": manifest, "contacts": contacts},
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    account_file = PROJECT_ROOT / ".runtime/demo-accounts.json"
    if not account_file.exists():
        account_file.write_text(
            json.dumps(
                {name: secrets.token_urlsafe(18) for name in manifest["identity_accounts"]},
                indent=2,
            ),
            encoding="utf-8",
        )
    passwords = json.loads(account_file.read_text(encoding="utf-8"))
    with Session(engine) as session, session.begin():
        existing = session.get(SourceBatch, BATCH)
        if existing:
            if existing.fingerprint != fingerprint or existing.generation != manifest["generation"]:
                raise RuntimeError(
                    "seed fingerprint/generation changed; explicit new batch required"
                )
            engine.dispose()
            return fingerprint
        session.add(
            SourceBatch(
                batch_id=BATCH,
                generation=1,
                source_kind="synthetic",
                seed=manifest["seed"],
                fingerprint=fingerprint,
                manifest_revision=manifest["revision"],
                status="active",
                created_at=SOURCE_TIME,
            )
        )
        session.flush()
        common = {
            "source_kind": "synthetic",
            "data_batch_id": BATCH,
            "batch_generation": 1,
            "created_at": SOURCE_TIME,
            "updated_at": SOURCE_TIME,
            "version": 1,
        }
        session.add(
            Store(
                store_id=STORE,
                name="模拟店铺",
                status="active",
                currency="CNY",
                business_timezone="Asia/Shanghai",
                **common,
            )
        )
        session.flush()
        for name in manifest["identity_accounts"]:
            customer = name.startswith("customer_")
            session.add(
                Subject(
                    subject_id=name,
                    store_id=STORE,
                    customer_id="cus_" + name if customer else None,
                    subject_type="customer" if customer else "staff",
                    login_name=name,
                    password_digest=hash_password(passwords[name]),
                    account_status="active",
                    auth_version=1,
                    **common,
                )
            )
        session.flush()
        for name in manifest["identity_accounts"]:
            roles = (
                ["support", "knowledge_admin"]
                if name == "multi_role"
                else ["support"]
                if name.startswith("support_")
                else []
                if name.startswith("customer_")
                else [name]
            )
            for role in roles:
                session.add(
                    RoleGrant(
                        grant_id=f"grant_{name}_{role}",
                        staff_id=name,
                        store_id=STORE,
                        role=role,
                        grant_status="active",
                        valid_from=SOURCE_TIME,
                        **common,
                    )
                )
        for contact in contacts:
            session.add(ShippingInput(**contact, store_id=STORE, purpose="checkout", **common))
    engine.dispose()
    return fingerprint
