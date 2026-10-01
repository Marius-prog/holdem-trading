import random
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from uuid import UUID, uuid4

from django.db import transaction

from .models import Account, Holding, Order, Quote

# Starting prices. Current prices live in Quote and move via set_price and tick.
QUOTES = {
    "AAPL": ("Apple", Decimal("227.52")),
    "MSFT": ("Microsoft", Decimal("428.76")),
    "NVDA": ("NVIDIA", Decimal("121.40")),
    "GOOGL": ("Alphabet", Decimal("165.85")),
    "AMZN": ("Amazon", Decimal("186.51")),
    "TSLA": ("Tesla", Decimal("258.02")),
}
MAX_QUANTITY = 1_000_000
CENT = Decimal("0.01")
MAX_PRICE = Decimal("1000000.00")
MAX_TICK_BPS = 300  # Random tick step: up to 3% either way (at least a cent if nonzero).
# SQLite stores decimals as REAL (~15 significant digits); this keeps cash exact to the cent.
MAX_CASH = Decimal("1000000000000.00")

# Stop protection: a hard stop below average cost, upgraded to a trailing stop
# once the position gains enough. The trailing stop only ratchets up.
HARD_STOP = Decimal("0.25")
TRAIL_UPGRADE = Decimal("0.07")
TRAIL = Decimal("0.12")
# Profit-taking levels: (gain over average cost, sell numerator, denominator of current
# shares), rounded down. 3/7 of what is left after the first take is another 30% of the
# position, leaving 40% to ride the trailing stop.
PROFIT_TAKES = ((Decimal("0.15"), 3, 10), (Decimal("0.25"), 3, 7))
# Kill switch: new buys are refused while equity is more than 15% below its peak.
KILL_SWITCH_DRAWDOWN = Decimal("0.15")


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
        "trigger": order.trigger,
        "created_at": order.created_at.isoformat(),
    }


def load_prices() -> dict[str, Decimal]:
    prices = dict(Quote.objects.values_list("symbol", "price"))
    missing = [Quote(symbol=s, price=p) for s, (_, p) in QUOTES.items() if s not in prices]
    if missing:
        Quote.objects.bulk_create(missing)
        prices |= {quote.symbol: quote.price for quote in missing}
    return prices


def equity(account: Account, prices: dict[str, Decimal]) -> Decimal:
    return account.cash + sum(
        (prices[h.symbol] * h.quantity for h in Holding.objects.all()), Decimal("0.00")
    )


def halt_below(account: Account) -> Decimal:
    return (account.equity_peak * (1 - KILL_SWITCH_DRAWDOWN)).quantize(CENT)


def record_equity_peak() -> None:
    """Raise the peak to current equity. A flat account restarts it at its cash, so a
    halt cannot outlive the positions that caused it."""
    account, _ = Account.objects.get_or_create(pk=1)
    # ponytail: capped like cash so SQLite keeps it exact; the switch is moot at that size.
    peak = min(equity(account, load_prices()), MAX_CASH)
    if peak > account.equity_peak or not Holding.objects.exists():
        account.equity_peak = peak
        account.save(update_fields=["equity_peak"])


def stop_price(holding: Holding) -> Decimal:
    """Rounded down, so a stop never rounds up to the price (e.g. a $0.01 position)."""
    if holding.trail_peak is None:
        stop = holding.average_cost * (1 - HARD_STOP)
    else:
        stop = holding.trail_peak * (1 - TRAIL)
    return stop.quantize(CENT, rounding=ROUND_DOWN)


@transaction.atomic
def get_state() -> dict:
    account, _ = Account.objects.get_or_create(pk=1)
    prices = load_prices()
    holdings = []
    holdings_value = Decimal("0.00")
    for holding in Holding.objects.order_by("symbol"):
        price = prices[holding.symbol]
        market_value = price * holding.quantity
        holdings_value += market_value
        holdings.append(
            {
                "symbol": holding.symbol,
                "quantity": holding.quantity,
                "average_cost": money(holding.average_cost),
                "price": money(price),
                "market_value": money(market_value),
                "unrealized_pnl": money((price - holding.average_cost) * holding.quantity),
                "stop_price": money(stop_price(holding)),
                "stop_type": "hard" if holding.trail_peak is None else "trailing",
            }
        )
    return {
        "account": {
            "cash": money(account.cash),
            "holdings_value": money(holdings_value),
            "total_value": money(account.cash + holdings_value),
        },
        "risk": {
            "equity_peak": money(account.equity_peak),
            "halt_below": money(halt_below(account)),
            "buys_halted": account.cash + holdings_value < halt_below(account),
        },
        "quotes": [
            {"symbol": symbol, "name": name, "price": money(prices[symbol])}
            for symbol, (name, _) in QUOTES.items()
        ],
        "holdings": holdings,
        "orders": [order_data(order) for order in Order.objects.all()[:100]],
    }


def sell(
    account: Account,
    holding: Holding,
    quantity: int,
    price: Decimal,
    client_order_id: UUID,
    trigger: str = "",
) -> Order:
    total = price * quantity
    if account.cash + total > MAX_CASH:
        raise TradeError(f"This sale would take paper cash above ${MAX_CASH:,}.")
    account.cash += total
    account.save(update_fields=["cash"])
    holding.quantity -= quantity
    if holding.quantity:
        holding.save(update_fields=["quantity"])
    else:
        holding.delete()
    return Order.objects.create(
        client_order_id=client_order_id,
        symbol=holding.symbol,
        side="sell",
        quantity=quantity,
        price=price,
        total=total,
        trigger=trigger,
    )


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

    record_equity_peak()
    account, _ = Account.objects.get_or_create(pk=1)
    holding = Holding.objects.filter(symbol=symbol).first()
    prices = load_prices()
    price = prices[symbol]
    if side == "sell":
        if not holding or quantity > holding.quantity:
            raise TradeError("Insufficient shares to sell; short selling is not supported.")
        order = sell(account, holding, quantity, price, client_order_id)
        record_equity_peak()
        return order_data(order)

    if equity(account, prices) < halt_below(account):
        raise TradeError(
            f"Kill switch: portfolio value is more than 15% below its "
            f"${account.equity_peak:,} peak. Buys resume at ${halt_below(account):,}."
        )
    total = price * quantity
    if total > account.cash:
        raise TradeError("Insufficient paper cash for this order.")
    account.cash -= total
    account.save(update_fields=["cash"])
    if holding:
        cost = holding.average_cost * holding.quantity + total
        holding.quantity += quantity
        holding.average_cost = (cost / holding.quantity).quantize(CENT)
        holding.trail_peak = None  # Added shares restart at the hard stop on the new average.
        holding.save(update_fields=["quantity", "average_cost", "trail_peak"])
    else:
        Holding.objects.create(symbol=symbol, quantity=quantity, average_cost=price)
    order = Order.objects.create(
        client_order_id=client_order_id,
        symbol=symbol,
        side=side,
        quantity=quantity,
        price=price,
        total=total,
    )
    if holding:
        apply_rules(symbol, price)  # Re-upgrades at once if already 7% above the new average.
    return order_data(order)


def apply_rules(symbol: str, price: Decimal) -> None:
    """Upgrade or ratchet the stop at the new price, sell everything if it is hit,
    otherwise take any profit level reached.

    Automatic sells fill at the new price, so a gap below the stop fills below it.
    """
    holding = Holding.objects.filter(symbol=symbol).first()
    if not holding:
        return
    account, _ = Account.objects.get_or_create(pk=1)
    if holding.trail_peak is None:
        raise_stop_at = holding.average_cost * (1 + TRAIL_UPGRADE)
    else:
        raise_stop_at = holding.trail_peak
    if price >= raise_stop_at:
        holding.trail_peak = price
        holding.save(update_fields=["trail_peak"])
    if price <= stop_price(holding):
        trigger = "hard_stop" if holding.trail_peak is None else "trailing_stop"
        sell(account, holding, holding.quantity, price, uuid4(), trigger)
        return
    for level, (gain, numerator, denominator) in enumerate(PROFIT_TAKES, start=1):
        if holding.profit_level >= level:
            continue
        shares = holding.quantity * numerator // denominator
        if price < holding.average_cost * (1 + gain) or not shares:
            break  # Levels go in order; one that would sell nothing stays open.
        holding.profit_level = level
        holding.save(update_fields=["profit_level"])
        sell(account, holding, shares, price, uuid4(), "profit_take")


def parse_price(raw: str) -> Decimal:
    error = TradeError(f"Price must be from $0.01 to ${MAX_PRICE:,} in whole cents.")
    try:
        price = Decimal(raw)
    except InvalidOperation:
        raise error from None
    if not price.is_finite() or not CENT <= price <= MAX_PRICE or price != price.quantize(CENT):
        raise error
    return price.quantize(CENT)


def move_price(symbol: str, price: Decimal) -> None:
    Quote.objects.filter(symbol=symbol).update(price=price)
    apply_rules(symbol, price)


@transaction.atomic
def set_price(symbol: str, raw_price: str) -> dict:
    if symbol not in QUOTES:
        raise TradeError("Choose a supported symbol.")
    price = parse_price(raw_price)
    load_prices()
    move_price(symbol, price)
    record_equity_peak()
    return get_state()


@transaction.atomic
def tick() -> dict:
    for symbol, price in load_prices().items():
        bps = random.randint(-MAX_TICK_BPS, MAX_TICK_BPS)
        moved = (price * (1 + Decimal(bps) / 10_000)).quantize(CENT)
        if bps and moved == price:
            moved += CENT if bps > 0 else -CENT  # small prices still move by a cent
        move_price(symbol, min(MAX_PRICE, max(CENT, moved)))
    record_equity_peak()
    return get_state()
