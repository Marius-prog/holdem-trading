from typing import Annotated
from uuid import UUID

import msgspec
from asgiref.sync import sync_to_async
from django.conf import settings
from django.http.request import split_domain_port, validate_host
from django_bolt import JSON, BoltAPI, Request
from django_bolt.param_functions import Header
from django_bolt.shortcuts import render

from .services import (
    TradeError,
    get_state,
    place_order,
    reset_kill_switch,
    set_dca,
    set_price,
    tick,
)

api = BoltAPI()
HostHeader = Annotated[str, Header(alias="host")]
PaperTradeHeader = Annotated[str, Header(alias="x-paper-trade")]


def refuse_foreign_host(host: str) -> JSON | None:
    """Bolt routes skip Django's ALLOWED_HOSTS check; enforce it (DNS rebinding)."""
    domain, _ = split_domain_port(host)
    if validate_host(domain, settings.ALLOWED_HOSTS):
        return None
    return JSON({"detail": "Host not allowed."}, status_code=400)


def refuse_write(host: str, paper_trade: str) -> JSON | None:
    if refused := refuse_foreign_host(host):
        return refused
    if paper_trade != "1":
        return JSON({"detail": "X-Paper-Trade: 1 is required."}, status_code=403)
    return None


class OrderRequest(msgspec.Struct, forbid_unknown_fields=True):
    symbol: str
    side: str
    quantity: int
    client_order_id: UUID


class PriceRequest(msgspec.Struct, forbid_unknown_fields=True):
    symbol: str
    price: str


class DcaRequest(msgspec.Struct, forbid_unknown_fields=True):
    enabled: bool


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
    paper_trade: PaperTradeHeader = "",
    host: HostHeader = "",
):
    if refused := refuse_write(host, paper_trade):
        return refused
    try:
        return await sync_to_async(place_order)(
            order.symbol, order.side, order.quantity, order.client_order_id
        )
    except TradeError as error:
        return JSON({"detail": str(error)}, status_code=error.status)


@api.post("/api/quotes")
async def update_quote(
    quote: PriceRequest, paper_trade: PaperTradeHeader = "", host: HostHeader = ""
):
    if refused := refuse_write(host, paper_trade):
        return refused
    try:
        return await sync_to_async(set_price)(quote.symbol, quote.price)
    except TradeError as error:
        return JSON({"detail": str(error)}, status_code=error.status)


@api.post("/api/dca")
async def dca_toggle(dca: DcaRequest, paper_trade: PaperTradeHeader = "", host: HostHeader = ""):
    if refused := refuse_write(host, paper_trade):
        return refused
    return await sync_to_async(set_dca)(dca.enabled)


@api.post("/api/kill-switch/reset")
async def kill_switch_reset(paper_trade: PaperTradeHeader = "", host: HostHeader = ""):
    if refused := refuse_write(host, paper_trade):
        return refused
    try:
        return await sync_to_async(reset_kill_switch)()
    except TradeError as error:
        return JSON({"detail": str(error)}, status_code=error.status)


@api.post("/api/tick")
async def market_tick(paper_trade: PaperTradeHeader = "", host: HostHeader = ""):
    if refused := refuse_write(host, paper_trade):
        return refused
    try:
        return await sync_to_async(tick)()
    except TradeError as error:
        return JSON({"detail": str(error)}, status_code=error.status)
