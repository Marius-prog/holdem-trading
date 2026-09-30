from decimal import Decimal
from uuid import UUID

from django.db import transaction

from .models import Account, Holding, Order

QUOTES = {
    "AAPL": ("Apple", Decimal("227.52")),
    "MSFT": ("Microsoft", Decimal("428.76")),
    "NVDA": ("NVIDIA", Decimal("121.40")),
    "GOOGL": ("Alphabet", Decimal("165.85")),
    "AMZN": ("Amazon", Decimal("186.51")),
    "TSLA": ("Tesla", Decimal("258.02")),
}
MAX_QUANTITY = 1_000_000


class TradeError(ValueError):
    def __init__(self, detail: str, status: int = 400):
        super().__init__(detail)
        self.status = status


def money(value: Decimal) -> str:
    return format(value, ".2f")


def order_data(order: Order) -> dict:
    return {
        "id": order.pk,
        "client_order_id": str(order.client_order_id),
        "symbol": order.symbol,
        "side": order.side,
        "quantity": order.quantity,
        "price": money(order.price),
        "total": money(order.total),
        "created_at": order.created_at.isoformat(),
    }


@transaction.atomic
def get_state() -> dict:
    account, _ = Account.objects.get_or_create(pk=1)
    holdings = []
    holdings_value = Decimal("0.00")
    for holding in Holding.objects.order_by("symbol"):
        price = QUOTES[holding.symbol][1]
        market_value = price * holding.quantity
        holdings_value += market_value
        holdings.append({
            "symbol": holding.symbol,
            "quantity": holding.quantity,
            "average_cost": money(holding.average_cost),
            "price": money(price),
            "market_value": money(market_value),
            "unrealized_pnl": money((price - holding.average_cost) * holding.quantity),
        })
    return {
        "account": {
            "cash": money(account.cash),
            "holdings_value": money(holdings_value),
            "total_value": money(account.cash + holdings_value),
        },
        "quotes": [
            {"symbol": symbol, "name": name, "price": money(price)}
            for symbol, (name, price) in QUOTES.items()
        ],
        "holdings": holdings,
        "orders": [order_data(order) for order in Order.objects.all()[:100]],
    }


@transaction.atomic
def place_order(symbol: str, side: str, quantity: int, client_order_id: UUID) -> dict:
    if symbol not in QUOTES:
        raise TradeError("Choose a supported symbol.")
    if side not in {"buy", "sell"}:
        raise TradeError("Side must be buy or sell.")
    if type(quantity) is not int or not 1 <= quantity <= MAX_QUANTITY:
        raise TradeError(f"Quantity must be a whole number from 1 to {MAX_QUANTITY}.")

    existing = Order.objects.filter(client_order_id=client_order_id).first()
    if existing:
        if (existing.symbol, existing.side, existing.quantity) != (symbol, side, quantity):
            raise TradeError("This order ID was already used for a different order.", 409)
        return order_data(existing)

    account, _ = Account.objects.get_or_create(pk=1)
    holding = Holding.objects.filter(symbol=symbol).first()
    price = QUOTES[symbol][1]
    total = price * quantity
    if side == "buy":
        if total > account.cash:
            raise TradeError("Insufficient paper cash for this order.")
        account.cash -= total
        if holding:
            # Quotes are fixed; every fill for this symbol has the same cost.
            holding.quantity += quantity
            holding.save(update_fields=["quantity"])
        else:
            Holding.objects.create(symbol=symbol, quantity=quantity, average_cost=price)
    else:
        if not holding or quantity > holding.quantity:
            raise TradeError("Insufficient shares to sell; short selling is not supported.")
        account.cash += total
        holding.quantity -= quantity
        if holding.quantity:
            holding.save(update_fields=["quantity"])
        else:
            holding.delete()
    account.save(update_fields=["cash"])
    order = Order.objects.create(
        client_order_id=client_order_id,
        symbol=symbol,
        side=side,
        quantity=quantity,
        price=price,
        total=total,
    )
    return order_data(order)
