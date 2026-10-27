"""在已核对的当前批次增量建零元账户；不修改已有余额或历史流水。"""

import hashlib
import json

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from cs_agent.config import PROJECT_ROOT
from cs_agent.db.business_tables import carts, wallet_entries, wallets
from cs_agent.db.models import Subject


def seed_accounts(session):
    for subject in session.scalars(select(Subject).where(Subject.subject_type == "customer")):
        source = {
            "store_id": subject.store_id,
            "data_batch_id": subject.data_batch_id,
            "batch_generation": subject.batch_generation,
            "source_kind": "synthetic",
            "created_at": subject.created_at,
            "updated_at": subject.created_at,
            "version": 1,
        }
        account_id = "wal_" + subject.subject_id
        session.execute(
            insert(carts)
            .values(cart_id="cart_" + subject.subject_id, owner_id=subject.subject_id, **source)
            .on_conflict_do_nothing()
        )
        created = session.execute(
            insert(wallets)
            .values(
                account_id=account_id,
                owner_id=subject.subject_id,
                currency="CNY",
                balance_minor=0,
                ledger_sequence=0,
                status="active",
                **source,
            )
            .on_conflict_do_nothing()
            .returning(wallets.c.account_id)
        ).scalar_one_or_none()
        if created:
            session.execute(
                insert(wallet_entries).values(
                    entry_id="init_" + account_id,
                    account_id=account_id,
                    owner_id=subject.subject_id,
                    operation_id=None,
                    sequence=0,
                    kind="initialization",
                    currency="CNY",
                    amount_minor=0,
                    balance_before_minor=0,
                    balance_after_minor=0,
                    business_ref=subject.subject_id,
                    occurred_at=subject.created_at,
                    recorded_at=subject.created_at,
                    **source,
                )
            )


def seed_catalog(session):
    from cs_agent.db.business_tables import inventory, products, skus, stock_movements
    from cs_agent.db.models import Store
    from cs_agent.db.seed import STORE

    manifest = json.loads(
        (PROJECT_ROOT / "data/synthetic/catalog-v1.json").read_text(encoding="utf-8")
    )
    digest = hashlib.sha256(
        json.dumps(manifest, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()
    store = session.get(Store, STORE)
    if store is None:
        raise RuntimeError("store seed must exist before catalog")
    common = {
        "store_id": STORE,
        "data_batch_id": store.data_batch_id,
        "batch_generation": store.batch_generation,
        "source_kind": "synthetic",
        "created_at": store.created_at,
        "updated_at": store.created_at,
        "version": 1,
    }
    for product in manifest["products"]:
        existing = (
            session.execute(select(products).where(products.c.product_id == product["product_id"]))
            .mappings()
            .first()
        )
        if existing:
            if existing["source_fingerprint"] != digest:
                raise RuntimeError("catalog source fingerprint changed; a new revision is required")
            continue
        session.execute(
            products.insert().values(
                **{
                    key: product[key]
                    for key in (
                        "product_id",
                        "name",
                        "category",
                        "description",
                        "aliases",
                        "image_slug",
                    )
                },
                status="active",
                source_revision=manifest["revision"],
                source_fingerprint=digest,
                external_id=None,
                supplier_ref=None,
                **common,
            )
        )
        for sku in product["skus"]:
            session.execute(
                skus.insert().values(
                    sku_id=sku["sku_id"],
                    product_id=product["product_id"],
                    specifications=sku["specifications"],
                    price_minor=int(sku["price_minor"]),
                    currency="CNY",
                    cost_minor=None,
                    price_version=1,
                    status="active",
                    **common,
                )
            )
            session.execute(
                inventory.insert().values(
                    sku_id=sku["sku_id"], stock_qty=sku["stock_qty"], reserved_qty=0, **common
                )
            )
            session.execute(
                stock_movements.insert().values(
                    movement_id="opening_" + sku["sku_id"],
                    sku_id=sku["sku_id"],
                    operation_id=None,
                    business_ref=sku["sku_id"],
                    kind="opening",
                    stock_delta=sku["stock_qty"],
                    reserved_delta=0,
                    stock_before=0,
                    stock_after=sku["stock_qty"],
                    reserved_before=0,
                    reserved_after=0,
                    reason="synthetic_initial_stock",
                    occurred_at=store.created_at,
                    recorded_at=store.created_at,
                    **common,
                )
            )
