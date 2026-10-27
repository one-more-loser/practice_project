from typing import Annotated, Literal

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import JSONResponse
from pydantic import Field
from sqlalchemy import select

from cs_agent.actions.service import ActionService
from cs_agent.catalog.service import CatalogService
from cs_agent.commerce.checkout import CheckoutService
from cs_agent.commerce.fulfillment import FulfillmentService
from cs_agent.commerce.query import OrderQuery
from cs_agent.core.context import Role
from cs_agent.core.errors import not_found
from cs_agent.core.types import ContractModel, MoneyMinor
from cs_agent.db.business_tables import operations
from cs_agent.identity.api import DB
from cs_agent.identity.policies import require_role
from cs_agent.identity.service import authenticate, require_csrf
from cs_agent.inventory.service import InventoryService
from cs_agent.main import envelope
from cs_agent.wallet.service import WalletService

router = APIRouter(prefix="/api/v1")
KEY = Annotated[str, Header(alias="Idempotency-Key")]
LIMIT = Annotated[int, Query(ge=1, le=50)]
OFFSET = Annotated[int, Query(ge=0)]


def context(session, request, write=False):
    session.info["clock"] = request.app.state.clock
    ctx, current, _, _ = authenticate(session, request)
    if write:
        require_csrf(request, current)
    return ctx


def operation_response(request, result, status):
    if status >= 400:
        return JSONResponse(
            {
                "contract_version": "API-CS-v0.1",
                "request_id": request.state.request_id,
                "error": {**result["error"], "operation_id": result["operation_id"]},
            },
            status_code=status,
        )
    return JSONResponse(envelope(request, result), status_code=status)


@router.get("/products")
def products(request: Request, session: DB, limit: LIMIT = 20, offset: OFFSET = 0):
    return envelope(
        request,
        {
            "items": CatalogService().read(session, limit=limit, offset=offset, now=request.app.state.clock.now()),
            "limit": limit,
            "offset": offset,
        },
    )


@router.get("/products/{product_id}")
def product(product_id: str, request: Request, session: DB):
    result = CatalogService().read(session, limit=1, product_id=product_id, now=request.app.state.clock.now())
    if not result:
        raise not_found()
    return envelope(request, result[0])


class TopupInput(MoneyMinor):
    pass


@router.get("/me/wallet")
def wallet(request: Request, session: DB):
    return envelope(request, WalletService().read(session, context(session, request)))


@router.get("/me/wallet/entries")
def entries(request: Request, session: DB, limit: LIMIT = 20, offset: OFFSET = 0):
    return envelope(
        request,
        {"items": WalletService().entries(session, context(session, request), limit, offset)},
    )


@router.post("/me/topups")
def topup(body: TopupInput, request: Request, session: DB, key: KEY):
    result, status = WalletService().topup(
        session,
        context(session, request, True),
        body.amount_minor,
        key,
        request.app.state.clock.now(),
    )
    return operation_response(request, result, status)


@router.get("/operations/{operation_id}")
def operation(operation_id: str, request: Request, session: DB):
    ctx = context(session, request)
    row = (
        session.execute(
            select(operations).where(
                operations.c.operation_id == operation_id,
                operations.c.actor_id == ctx.subject_id,
                operations.c.actor_role == ctx.role.value,
                operations.c.store_id == ctx.store_id,
                operations.c.data_batch_id == ctx.data_batch_id,
                operations.c.batch_generation == ctx.batch_generation,
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise not_found()
    return envelope(request, row["result"])


class Proposal(ContractModel):
    action: Literal[
        "order.create",
        "order.pay",
        "order.address.change",
        "order.receipt.confirm",
        "order.cancel",
        "ticket.create",
        "aftersale.apply",
        "transport.incident.create",
        "transport.refund.request",
    ]
    resource_ref: str = Field(min_length=1, max_length=80)
    parameters: dict = Field(default_factory=dict)
    expected_version: int = Field(ge=1, strict=True)


class Decision(ContractModel):
    decision: Literal["confirm", "cancel"]


@router.post("/action-proposals")
def proposal(body: Proposal, request: Request, session: DB):
    return envelope(
        request,
        ActionService().propose(
            session,
            context(session, request, True),
            body.action,
            body.resource_ref,
            body.parameters,
            body.expected_version,
            request.app.state.clock.now(),
        ),
    )


@router.post("/confirmations/{confirmation_id}/submit")
def submit(confirmation_id: str, body: Decision, request: Request, session: DB, key: KEY):
    result, status = ActionService().submit(
        session,
        context(session, request, True),
        confirmation_id,
        body.decision,
        key,
        request.app.state.clock.now(),
    )
    return operation_response(request, result, status)


class CartInput(ContractModel):
    qty: int = Field(gt=0, le=2147483647, strict=True)
    selected: bool = True
    expected_version: int = Field(ge=0, strict=True)


@router.get("/me/cart")
def cart(request: Request, session: DB):
    return envelope(
        request,
        CheckoutService().cart(session, context(session, request), request.app.state.clock.now()),
    )


@router.put("/me/cart/items/{sku_id}")
def put_cart(sku_id: str, body: CartInput, request: Request, session: DB, key: KEY):
    result, status = CheckoutService().put_cart_item(
        session,
        context(session, request, True),
        sku_id,
        body.qty,
        body.selected,
        body.expected_version,
        key,
        request.app.state.clock.now(),
    )
    return operation_response(request, result, status)


@router.delete("/me/cart/items/{sku_id}")
def delete_cart(
    sku_id: str, request: Request, session: DB, key: KEY, expected_version: int = Query(ge=1)
):
    result, status = CheckoutService().remove_cart_item(
        session,
        context(session, request, True),
        sku_id,
        expected_version,
        key,
        request.app.state.clock.now(),
    )
    return operation_response(request, result, status)


class ShippingInput(ContractModel):
    recipient: str = Field(min_length=1, max_length=100)
    phone: str = Field(pattern=r"^1[3-9][0-9]{9}$")
    province: str = Field(min_length=2, max_length=80)
    city: str = Field(min_length=2, max_length=80)
    district: str = Field(min_length=1, max_length=80)
    street: str = Field(min_length=1, max_length=300)
    postal_code: str | None = Field(default=None, max_length=16)
    purpose: Literal["checkout", "address_change"] = "checkout"


@router.post("/me/shipping-inputs")
def shipping_input(body: ShippingInput, request: Request, session: DB, key: KEY):
    result, status = CheckoutService().create_shipping_input(
        session,
        context(session, request, True),
        body.model_dump(),
        key,
        request.app.state.clock.now(),
    )
    return operation_response(request, result, status)


class QuoteLine(ContractModel):
    sku_id: str = Field(min_length=1, max_length=80)
    qty: int = Field(gt=0, le=2147483647, strict=True)


class QuoteInput(ContractModel):
    items: list[QuoteLine] = Field(default_factory=list, max_length=50)
    cart_item_ids: list[str] = Field(default_factory=list, max_length=50)
    shipping_input_ref: str = Field(min_length=1, max_length=80)


@router.post("/me/quotes")
def quote(body: QuoteInput, request: Request, session: DB, key: KEY):
    result, status = CheckoutService().quote(
        session,
        context(session, request, True),
        [row.model_dump() for row in body.items],
        body.shipping_input_ref,
        key,
        request.app.state.clock.now(),
        body.cart_item_ids,
    )
    return operation_response(request, result, status)


@router.get("/me/orders")
def orders(request: Request, session: DB, limit: LIMIT = 20, offset: OFFSET = 0):
    return envelope(
        request,
        {
            "items": OrderQuery().list(
                session,
                context(session, request),
                request.app.state.clock.now(),
                limit=limit,
                offset=offset,
            )
        },
    )


@router.get("/me/orders/{order_id}")
def order(order_id: str, request: Request, session: DB):
    return envelope(
        request,
        OrderQuery().get(
            session, context(session, request), order_id, request.app.state.clock.now()
        ),
    )


@router.get("/me/orders/{order_id}/shipments")
def order_shipments(order_id: str, request: Request, session: DB):
    result = OrderQuery().get(
        session, context(session, request), order_id, request.app.state.clock.now()
    )
    return envelope(request, {"items": result["shipments"]})


@router.get("/shop/products")
def shop_products(request: Request, session: DB):
    ctx = context(session, request)
    require_role(ctx, Role.SHOP_ADMIN)
    return envelope(
        request,
        {"items": CatalogService().read(
            session, ctx.store_id, 50, include_inactive=True, now=request.app.state.clock.now()
        )},
    )


class SkuPatch(ContractModel):
    price_minor: str | None = None
    status: Literal["active", "inactive"] | None = None
    specifications: dict[str, str] | None = None
    expected_version: int = Field(ge=1, strict=True)


class ProductPatch(ContractModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    category: str | None = Field(default=None, min_length=1, max_length=80)
    description: str | None = Field(default=None, min_length=1, max_length=2000)
    status: Literal["active", "inactive", "draft"] | None = None
    expected_version: int = Field(ge=1, strict=True)


@router.patch("/shop/products/{product_id}")
def patch_product(product_id: str, body: ProductPatch, request: Request, session: DB, key: KEY):
    result, status = CatalogService().patch_product(
        session,
        context(session, request, True),
        product_id,
        body.model_dump(exclude_none=True, exclude={"expected_version"}),
        body.expected_version,
        key,
        request.app.state.clock.now(),
    )
    return operation_response(request, result, status)


@router.patch("/shop/skus/{sku_id}")
def patch_sku(sku_id: str, body: SkuPatch, request: Request, session: DB, key: KEY):
    result, status = CatalogService().patch_sku(
        session,
        context(session, request, True),
        sku_id,
        body.model_dump(exclude_none=True, exclude={"expected_version"}),
        body.expected_version,
        key,
        request.app.state.clock.now(),
    )
    return operation_response(request, result, status)


class ProductInput(ContractModel):
    name: str = Field(min_length=1, max_length=160)
    category: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=1, max_length=2000)
    specifications: dict[str, str]
    price_minor: str


@router.post("/shop/products")
def create_product(body: ProductInput, request: Request, session: DB, key: KEY):
    result, status = CatalogService().create(
        session,
        context(session, request, True),
        body.model_dump(),
        key,
        request.app.state.clock.now(),
    )
    return operation_response(request, result, status)


class InventoryInput(ContractModel):
    delta: int = Field(strict=True, ge=-2147483647, le=2147483647)
    expected_version: int = Field(ge=1, strict=True)
    reason: str = Field(min_length=3, max_length=160)


@router.post("/shop/skus/{sku_id}/inventory-adjustments")
def inventory_adjustment(
    sku_id: str, body: InventoryInput, request: Request, session: DB, key: KEY
):
    result, status = InventoryService().adjust(
        session,
        context(session, request, True),
        sku_id,
        body.delta,
        body.expected_version,
        key,
        request.app.state.clock.now(),
        body.reason,
    )
    return operation_response(request, result, status)


@router.get("/shop/orders")
def shop_orders(request: Request, session: DB, limit: LIMIT = 20, offset: OFFSET = 0):
    return envelope(
        request,
        {
            "items": OrderQuery().list(
                session,
                context(session, request),
                request.app.state.clock.now(),
                shop=True,
                limit=limit,
                offset=offset,
            )
        },
    )


@router.get("/shop/orders/{order_id}")
def shop_order(order_id: str, request: Request, session: DB):
    return envelope(
        request,
        OrderQuery().get(
            session, context(session, request), order_id, request.app.state.clock.now(), shop=True
        ),
    )


class ShipmentInput(ContractModel):
    quantities: dict[str, Annotated[int, Field(strict=True, gt=0)]]
    expected_version: int = Field(ge=1, strict=True)


@router.post("/shop/orders/{order_id}/shipments")
def ship(order_id: str, body: ShipmentInput, request: Request, session: DB, key: KEY):
    result, status = FulfillmentService().ship(
        session,
        context(session, request, True),
        order_id,
        body.quantities,
        body.expected_version,
        key,
        request.app.state.clock.now(),
    )
    return operation_response(request, result, status)


class TrackingInput(ContractModel):
    status: Literal["carrier_signed"]
    expected_version: int = Field(ge=1, strict=True)


@router.post("/shop/orders/{order_id}/shipments/{shipment_id}/tracking")
def tracking(
    order_id: str, shipment_id: str, body: TrackingInput, request: Request, session: DB, key: KEY
):
    result, status = FulfillmentService().update_tracking(
        session,
        context(session, request, True),
        order_id,
        shipment_id,
        body.status,
        body.expected_version,
        key,
        request.app.state.clock.now(),
    )
    return operation_response(request, result, status)
