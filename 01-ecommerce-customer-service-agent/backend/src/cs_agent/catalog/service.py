from datetime import UTC, datetime

from sqlalchemy import select, update

from cs_agent.actions.repository import execute_once, fingerprint, new_id, provenance
from cs_agent.audit.service import record
from cs_agent.core.context import Role
from cs_agent.core.errors import AppError, not_found
from cs_agent.db.business_tables import inventory, products, skus
from cs_agent.identity.policies import require_role
from cs_agent.wallet.service import event, parse_amount


def customer_price(session, store_id, row, now):
    from cs_agent.activities.pricing import resolve_price

    value = resolve_price(session, store_id, row, now)
    return {"price_minor": str(value["effective_price_minor"]),
            "base_price_minor": str(value["base_price_minor"]),
            "activity_id": value["activity_id"], "activity_revision": value["activity_revision"]}


def sku_row(session, sku_id, store_id, *, lock=False):
    stmt = (
        select(
            skus,
            inventory.c.stock_qty,
            inventory.c.reserved_qty,
            inventory.c.version.label("inventory_version"),
            products.c.name,
            products.c.status.label("product_status"),
            products.c.version.label("product_version"),
            products.c.image_slug,
        )
        .join(inventory, inventory.c.sku_id == skus.c.sku_id)
        .join(products, products.c.product_id == skus.c.product_id)
        .where(skus.c.sku_id == sku_id, skus.c.store_id == store_id)
    )
    row = (
        session.execute(stmt.with_for_update(of=(skus, inventory)) if lock else stmt)
        .mappings()
        .first()
    )
    if row is None:
        raise not_found()
    return row


class CatalogService:
    def patch_product(self, session, ctx, product_id, changes, expected_version, key, now):
        require_role(ctx, Role.SHOP_ADMIN)
        if not changes or set(changes) - {"name", "description", "category", "status"}:
            raise AppError("invalid_input", 422, "商品字段不符合契约")

        def execute(op_id):
            row = (
                session.execute(
                    select(products)
                    .where(
                        products.c.product_id == product_id,
                        products.c.store_id == ctx.store_id,
                        products.c.data_batch_id == ctx.data_batch_id,
                        products.c.batch_generation == ctx.batch_generation,
                    )
                    .with_for_update()
                )
                .mappings()
                .first()
            )
            if row is None:
                raise not_found()
            if row["version"] != expected_version:
                raise AppError("version_conflict", 409, "商品已更新，请刷新后重试")
            session.execute(
                update(products)
                .where(products.c.product_id == product_id)
                .values(
                    **changes, version=row["version"] + 1, updated_at=now, source_kind="derived"
                )
            )
            event(
                session,
                ctx,
                "product.updated",
                product_id,
                row["version"] + 1,
                op_id,
                {"product_id": product_id},
                now,
            )
            record(
                session,
                request_id=ctx.request_id,
                action="product.updated",
                outcome="succeeded",
                ctx=ctx,
                resource_ref=product_id,
                summary={"version": row["version"] + 1},
            )
            return {"business_ref": product_id, "version": row["version"] + 1}

        return execute_once(
            session,
            ctx,
            "product.update",
            key,
            {"product_id": product_id, "changes": changes, "expected_version": expected_version},
            now,
            execute,
            resource_ref=product_id,
        )

    def read(
        self,
        session,
        store_id="store_sim",
        limit=20,
        offset=0,
        *,
        include_inactive=False,
        product_id=None,
        now=None,
    ):
        now = now or datetime.now(UTC)
        stmt = select(products).where(products.c.store_id == store_id)
        if product_id is not None:
            stmt = stmt.where(products.c.product_id == product_id)
        if not include_inactive:
            stmt = stmt.where(products.c.status == "active")
        result = []
        for product in session.execute(
            stmt.order_by(products.c.created_at, products.c.product_id).limit(limit).offset(offset)
        ).mappings():
            rows = session.execute(
                select(
                    skus,
                    inventory.c.stock_qty,
                    inventory.c.reserved_qty,
                    inventory.c.version.label("inventory_version"),
                )
                .join(inventory, inventory.c.sku_id == skus.c.sku_id)
                .where(skus.c.product_id == product["product_id"])
                .order_by(skus.c.sku_id)
            ).mappings()
            visible = [
                {
                    "sku_id": row["sku_id"],
                    "specifications": row["specifications"],
                    **customer_price(session, store_id, row, now),
                    "currency": "CNY",
                    "status": row["status"],
                    "price_version": row["price_version"],
                    "version": row["version"],
                    "available_qty": row["stock_qty"] - row["reserved_qty"],
                    **(
                        {
                            "stock_qty": row["stock_qty"],
                            "reserved_qty": row["reserved_qty"],
                            "inventory_version": row["inventory_version"],
                        }
                        if include_inactive
                        else {}
                    ),
                }
                for row in rows
                if include_inactive or row["status"] == "active"
            ]
            result.append(
                {
                    key: product[key]
                    for key in (
                        "product_id",
                        "name",
                        "category",
                        "description",
                        "aliases",
                        "image_slug",
                        "status",
                        "version",
                    )
                }
                | {
                    "skus": visible,
                    "source_kind": product["source_kind"],
                    "as_of": product["updated_at"].isoformat(),
                }
            )
        return result

    def patch_sku(self, session, ctx, sku_id, changes, expected_version, key, now):
        require_role(ctx, Role.SHOP_ADMIN)
        normalized = dict(changes)
        if "price_minor" in normalized:
            normalized["price_minor"] = parse_amount(normalized["price_minor"])
        if set(normalized) - {"price_minor", "status", "specifications"} or not normalized:
            raise AppError("invalid_input", 422, "商品字段不符合契约")
        if normalized.get("status", "active") not in {"active", "inactive"}:
            raise AppError("invalid_input", 422, "商品状态不符合契约")

        def execute(op_id):
            row = sku_row(session, sku_id, ctx.store_id, lock=True)
            if row["version"] != expected_version:
                raise AppError("version_conflict", 409, "商品已更新，请刷新后重试")
            if "price_minor" in normalized and normalized["price_minor"] != row["price_minor"]:
                from cs_agent.activities.pricing import assert_price_change_allowed
                assert_price_change_allowed(session, ctx.store_id, sku_id, now)
            version = row["version"] + 1
            price_version = row["price_version"] + (
                "price_minor" in normalized and normalized["price_minor"] != row["price_minor"]
            )
            session.execute(
                update(skus)
                .where(skus.c.sku_id == sku_id)
                .values(
                    **normalized,
                    version=version,
                    price_version=price_version,
                    updated_at=now,
                    source_kind="derived",
                )
            )
            record(
                session,
                request_id=ctx.request_id,
                action="sku.updated",
                outcome="succeeded",
                ctx=ctx,
                resource_ref=sku_id,
                summary={"version": version},
            )
            event(
                session,
                ctx,
                "sku.updated",
                sku_id,
                version,
                op_id,
                {"sku_id": sku_id, "price_version": price_version},
                now,
            )
            return {"business_ref": sku_id, "version": version, "price_version": price_version}

        return execute_once(
            session,
            ctx,
            "sku.update",
            key,
            {"sku_id": sku_id, "changes": normalized, "expected_version": expected_version},
            now,
            execute,
            resource_ref=sku_id,
        )

    def create(self, session, ctx, fields, key, now):
        require_role(ctx, Role.SHOP_ADMIN)

        def execute(op_id):
            product_id, sku_id = new_id("product"), new_id("sku")
            session.execute(
                products.insert().values(
                    product_id=product_id,
                    **{key: fields[key] for key in ("name", "category", "description")},
                    aliases=[],
                    image_slug="cup",
                    status="active",
                    source_revision="business:" + op_id,
                    source_fingerprint=fingerprint(fields),
                    external_id=None,
                    supplier_ref=None,
                    **provenance(ctx, now),
                )
            )
            session.execute(
                skus.insert().values(
                    sku_id=sku_id,
                    product_id=product_id,
                    specifications=fields["specifications"],
                    price_minor=parse_amount(fields["price_minor"]),
                    currency="CNY",
                    cost_minor=None,
                    price_version=1,
                    status="active",
                    **provenance(ctx, now),
                )
            )
            session.execute(
                inventory.insert().values(
                    sku_id=sku_id, stock_qty=0, reserved_qty=0, **provenance(ctx, now)
                )
            )
            record(
                session,
                request_id=ctx.request_id,
                action="product.created",
                outcome="succeeded",
                ctx=ctx,
                resource_ref=product_id,
                summary={"version": 1},
            )
            return {"business_ref": product_id, "sku_id": sku_id, "version": 1}

        return execute_once(session, ctx, "product.create", key, fields, now, execute)
