from typing import Annotated
from uuid import UUID

import msgspec
from asgiref.sync import sync_to_async
from django.conf import settings
from django.http.request import split_domain_port, validate_host
from django_bolt import JSON, BoltAPI, Request
from django_bolt.param_functions import Header
from django_bolt.shortcuts import render

from .services import TradeError, get_state, place_order

api = BoltAPI()
HostHeader = Annotated[str, Header(alias="host")]


def refuse_foreign_host(host: str) -> JSON | None:
    """Bolt routes skip Django's ALLOWED_HOSTS check; enforce it (DNS rebinding)."""
    domain, _ = split_domain_port(host)
    if validate_host(domain, settings.ALLOWED_HOSTS):
        return None
    return JSON({"detail": "Host not allowed."}, status_code=400)


class OrderRequest(msgspec.Struct, forbid_unknown_fields=True):
    symbol: str
    side: str
    quantity: int
    client_order_id: UUID


@api.get("/")
async def dashboard(request: Request):
    return render(request, "trading/dashboard.html")


@api.get("/api/state")
async def state(host: HostHeader = ""):
    if refused := refuse_foreign_host(host):
        return refused
    return await sync_to_async(get_state)()


@api.post("/api/orders")
async def create_order(
    order: OrderRequest,
    paper_trade: Annotated[str, Header(alias="x-paper-trade")] = "",
    host: HostHeader = "",
):
    if refused := refuse_foreign_host(host):
        return refused
    if paper_trade != "1":
        return JSON({"detail": "X-Paper-Trade: 1 is required."}, status_code=403)
    try:
        return await sync_to_async(place_order)(
            order.symbol, order.side, order.quantity, order.client_order_id
        )
    except TradeError as error:
        return JSON({"detail": str(error)}, status_code=error.status)
