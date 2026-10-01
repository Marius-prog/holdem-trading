import random
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from uuid import UUID, uuid4

from django.db import transaction
from django.db.models import F

from .alerts import alert, order_alert, recent
from .models import Account, DcaPlan, Holding, Order, Quote

# Starting prices. Current prices live in Quote and move via set_price and tick.
QUOTES = {
    "AAPL": ("Apple", Decimal("227.52")),
    "MSFT": ("Microsoft", Decimal("428.76")),
    "NVDA": ("NVIDIA", Decimal("121.40")),
    "GOOGL": ("Alphabet", Decimal("165.85")),
    "AMZN": ("Amazon", Decimal("186.51")),
    "TSLA": ("Tesla", Decimal("258.02")),
    "SPY": ("SPDR S&P 500 ETF", Decimal("656.77")),
    "GLD": ("SPDR Gold Shares", Decimal("400.00")),
}
# Market regime: QQQ is a non-tradable index that moves like the quotes. Ladder buys
# pause while it is below the average of its last 20 prices (one per change).
REGIME_SYMBOL = "QQQ"
INDEXES = {REGIME_SYMBOL: ("Invesco QQQ", Decimal("480.00"))}
REGIME_WINDOW = 20
MAX_QUANTITY = 1_000_000
CENT = Decimal("0.01")
MAX_PRICE = Decimal("1000000.00")
MAX_TICK_BPS = 300  # Random tick step: up to 3% either way (at least a cent if nonzero).
# SQLite stores decimals as REAL (~15 significant digits); this keeps cash exact to the cent.
MAX_CASH = Decimal("1000000000000.00")

# Stop protection per symbol: (hard stop below average cost, gain that upgrades it to a
# trailing stop, trailing distance). The trailing stop only ratchets up.
DEFAULT_STOPS = (Decimal("0.25"), Decimal("0.07"), Decimal("0.12"))
DCA_STOPS = (Decimal("0.12"), Decimal("0.07"), Decimal("0.05"))  # Strategy 3
STOPS = {"SPY": DCA_STOPS, "GLD": DCA_STOPS}
# Strategy 3: dollar-cost average into SPY and a GLD sleeve. Each buys its shares once per
# 7 of its own price changes (a "week"), up to a target or a value cap, then holds. No
# ladders, profit takes or re-entry for these symbols.
DCA_PLANS = {  # symbol: (shares per buy, target shares, max position value)
    "SPY": (4, 39, Decimal("30000.00")),
    "GLD": (2, 37, Decimal("15000.00")),
}
DCA_EVERY = 7
# Profit-taking levels: (gain over average cost, sell numerator, denominator of current
# shares), rounded down. 3/7 of what is left after the first take is another 30% of the
# position, leaving 40% to ride the trailing stop.
PROFIT_TAKES = ((Decimal("0.15"), 3, 10), (Decimal("0.25"), 3, 7))
# Ladder buys: (drop below the position's entry price, shares) by volatility tier,
# bought in order on price drops. Each automatic buy shrinks to fit the per-symbol
# cost cap and the cash on hand.
LOW_VOL = ((Decimal("0.07"), 10), (Decimal("0.14"), 15), (Decimal("0.21"), 15))
MEDIUM_VOL = ((Decimal("0.10"), 10), (Decimal("0.20"), 15), (Decimal("0.30"), 15))
HIGH_VOL = ((Decimal("0.15"), 10), (Decimal("0.25"), 15), (Decimal("0.35"), 15))
LADDERS = {
    "AAPL": LOW_VOL,
    "MSFT": LOW_VOL,
    "GOOGL": LOW_VOL,
    "AMZN": MEDIUM_VOL,
    "NVDA": MEDIUM_VOL,
    "TSLA": HIGH_VOL,
}
POSITION_COST_CAP = Decimal("14000.00")
# Re-entry after a stop-out. Each price change of a symbol counts as one trading day:
# wait one change, then while the price is above its 10-change EMA place a limit buy
# 5% below it; re-price a limit left unfilled for more than 5 changes.
EMA_ALPHA = Decimal(2) / (10 + 1)
REENTRY_SHARES = 10
REENTRY_DISCOUNT = Decimal("0.05")
REENTRY_EXPIRY = 5
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
    missing = [
        Quote(symbol=s, price=p, history=[str(p)] if s in INDEXES else [])
        for s, (_, p) in (QUOTES | INDEXES).items()
        if s not in prices
    ]
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


def capped_equity(account: Account) -> Decimal:
    # ponytail: capped like cash so SQLite keeps it exact; the switch is moot at that size.
    return min(equity(account, load_prices()), MAX_CASH)


def record_equity_peak() -> None:
    account, _ = Account.objects.get_or_create(pk=1)
    if (peak := capped_equity(account)) > account.equity_peak:
        account.equity_peak = peak
        account.save(update_fields=["equity_peak"])


def market_regime() -> tuple[Decimal, Decimal, bool]:
    """QQQ price, the average of its recent prices, and whether it is below it."""
    quote = Quote.objects.get(symbol=REGIME_SYMBOL)
    history = [Decimal(p) for p in quote.history] or [quote.price]
    average = sum(history) / len(history)
    return quote.price, average, quote.price < average


def stop_price(holding: Holding) -> Decimal:
    """Rounded down, so a stop never rounds up to the price (e.g. a $0.01 position)."""
    hard, _, trail = STOPS.get(holding.symbol, DEFAULT_STOPS)
    if holding.trail_peak is None:
        stop = holding.average_cost * (1 - hard)
    else:
        stop = holding.trail_peak * (1 - trail)
    return stop.quantize(CENT, rounding=ROUND_DOWN)


def dca_state(symbol: str) -> dict:
    plan = dca_plan(symbol)
    holding = Holding.objects.filter(symbol=symbol).first()
    next_buy_in = None
    if plan.enabled and plan.phase == "dca":
        moves = Quote.objects.get(symbol=symbol).moves
        last = plan.last_buy_move
        next_buy_in = 0 if last is None else max(DCA_EVERY - (moves - last), 0)
    return {
        "enabled": plan.enabled,
        "phase": plan.phase,
        "shares": holding.quantity if holding else 0,
        "target": DCA_PLANS[symbol][1],
        "next_buy_in": next_buy_in,
        "cycle": plan.cycle,
    }


@transaction.atomic
def get_state() -> dict:
    account, _ = Account.objects.get_or_create(pk=1)
    prices = load_prices()
    regime_price, regime_average, bearish = market_regime()
    reentries = dict(
        Quote.objects.filter(reentry_limit__isnull=False).values_list("symbol", "reentry_limit")
    )
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
            {
                "symbol": symbol,
                "name": name,
                "price": money(prices[symbol]),
                "reentry_limit": money(reentries[symbol]) if symbol in reentries else None,
            }
            for symbol, (name, _) in QUOTES.items()
        ],
        "dca": {symbol: dca_state(symbol) for symbol in DCA_PLANS},
        "market": {
            "symbol": REGIME_SYMBOL,
            "price": money(regime_price),
            "average": money(regime_average.quantize(CENT)),
            "bearish": bearish,
        },
        "holdings": holdings,
        "orders": [order_data(order) for order in Order.objects.all()[:100]],
        "alerts": recent(),
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
    order = Order.objects.create(
        client_order_id=client_order_id,
        symbol=holding.symbol,
        side="sell",
        quantity=quantity,
        price=price,
        total=total,
        trigger=trigger,
    )
    order_alert(order)
    return order


def buy(
    account: Account,
    holding: Holding | None,
    symbol: str,
    quantity: int,
    price: Decimal,
    client_order_id: UUID,
    trigger: str = "",
) -> Order:
    total = price * quantity
    account.cash -= total
    account.save(update_fields=["cash"])
    was_trailing = holding is not None and holding.trail_peak is not None
    if holding:
        cost = holding.average_cost * holding.quantity + total
        holding.quantity += quantity
        holding.average_cost = (cost / holding.quantity).quantize(CENT)
        holding.trail_peak = None  # Added shares restart at the hard stop on the new average.
        holding.save(update_fields=["quantity", "average_cost", "trail_peak"])
    else:
        Holding.objects.create(
            symbol=symbol, quantity=quantity, average_cost=price, entry_price=price
        )
    order = Order.objects.create(
        client_order_id=client_order_id,
        symbol=symbol,
        side="buy",
        quantity=quantity,
        price=price,
        total=total,
        trigger=trigger,
    )
    order_alert(order)
    if was_trailing:
        stop = stop_price(holding)
        alert(
            "info", f"{symbol} stop reset to a hard stop at ${stop:,} after adding shares.", symbol
        )
    return order


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
    prices = load_prices()
    price = prices[symbol]
    if side == "sell":
        if not holding or quantity > holding.quantity:
            raise TradeError("Insufficient shares to sell; short selling is not supported.")
        return order_data(sell(account, holding, quantity, price, client_order_id))

    if equity(account, prices) < halt_below(account):
        raise TradeError(
            f"Kill switch: portfolio value is more than 15% below its "
            f"${account.equity_peak:,} peak. Buys resume at ${halt_below(account):,}, "
            f"or after you reset the kill switch."
        )
    if price * quantity > account.cash:
        raise TradeError("Insufficient paper cash for this order.")
    order = buy(account, holding, symbol, quantity, price, client_order_id)
    clear_reentry(symbol)  # buying by hand replaces any pending re-entry
    if holding:
        # Re-upgrades at once if already 7% above the new average. Ladders and profit
        # takes wait for a price move.
        apply_rules(symbol, price, stops_only=True)
    return order_data(order)


def buy_ladder(account: Account, holding: Holding, price: Decimal) -> None:
    """Buy each ladder level the price has dropped to, in order. A level that would buy
    nothing (cost cap, cash, or the kill switch) stays open with the ones after it.
    Ladders average down, so they wait while the price is at or above average cost, and
    pause while the market regime is bearish."""
    if price >= holding.average_cost or equity(account, load_prices()) < halt_below(account):
        return
    if market_regime()[2]:
        return
    entry = holding.entry_price or holding.average_cost
    for level, (drop, shares) in enumerate(LADDERS[holding.symbol], start=1):
        if holding.ladder_level >= level:
            continue
        room = POSITION_COST_CAP - holding.average_cost * holding.quantity
        shares = min(shares, int(max(room, 0) // price), int(account.cash // price))
        if price > entry * (1 - drop) or not shares:
            break
        holding.ladder_level = level
        holding.save(update_fields=["ladder_level"])
        buy(account, holding, holding.symbol, shares, price, uuid4(), "ladder")


def apply_rules(symbol: str, price: Decimal, stops_only: bool = False) -> None:
    """Upgrade or ratchet the stop at the new price and sell everything if it is hit.
    Otherwise buy any ladder level and take any profit level reached.

    Automatic orders fill at the new price, so a gap below the stop fills below it.
    """
    holding = Holding.objects.filter(symbol=symbol).first()
    if not holding:
        return
    account, _ = Account.objects.get_or_create(pk=1)
    if holding.trail_peak is None:
        raise_stop_at = holding.average_cost * (1 + STOPS.get(symbol, DEFAULT_STOPS)[1])
    else:
        raise_stop_at = holding.trail_peak
    if price >= raise_stop_at:
        upgrading = holding.trail_peak is None
        holding.trail_peak = price
        holding.save(update_fields=["trail_peak"])
        if upgrading:
            trail = STOPS.get(symbol, DEFAULT_STOPS)[2]
            stop = stop_price(holding)
            alert(
                "info",
                f"{symbol} stop now trails {trail:.0%} below its peak (stop ${stop:,}).",
                symbol,
            )
    if price <= stop_price(holding):
        trigger = "hard_stop" if holding.trail_peak is None else "trailing_stop"
        sell(account, holding, holding.quantity, price, uuid4(), trigger)
        if symbol in DCA_PLANS:  # start a new accumulation cycle a week from now
            moves = Quote.objects.get(symbol=symbol).moves
            DcaPlan.objects.filter(symbol=symbol).update(
                phase="dca", last_buy_move=moves, cycle=F("cycle") + 1
            )
        else:
            Quote.objects.filter(symbol=symbol).update(stop_move=F("moves"))
        return
    if stops_only or symbol in DCA_PLANS:
        return
    buy_ladder(account, holding, price)
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


def clear_reentry(symbol: str) -> None:
    Quote.objects.filter(symbol=symbol).update(
        stop_move=None, reentry_limit=None, reentry_move=None
    )


def re_enter(symbol: str, price: Decimal) -> None:
    """After a stop-out, fill a pending re-entry limit the price has reached, keep a
    working one, or (re)place one; cancel it while the kill switch is on."""
    quote = Quote.objects.get(symbol=symbol)
    if quote.stop_move is None:
        return
    account, _ = Account.objects.get_or_create(pk=1)
    halted = equity(account, load_prices()) < halt_below(account)
    pending = quote.reentry_limit is not None and not halted
    if pending and price <= quote.reentry_limit:
        shares = min(REENTRY_SHARES, int(POSITION_COST_CAP // price), int(account.cash // price))
        if shares:
            buy(account, None, symbol, shares, price, uuid4(), "reentry")
            clear_reentry(symbol)
            return
    if pending and quote.moves - quote.reentry_move <= REENTRY_EXPIRY:
        return  # still working
    can_place = not halted and quote.moves > quote.stop_move and price > quote.ema
    quote.reentry_limit = (price * (1 - REENTRY_DISCOUNT)).quantize(CENT) if can_place else None
    quote.reentry_move = quote.moves if can_place else None
    quote.save(update_fields=["reentry_limit", "reentry_move"])
    if can_place:
        limit = quote.reentry_limit
        alert(
            "info",
            f"Re-entry limit for {symbol}: buy {REENTRY_SHARES} at ${limit:,} or lower.",
            symbol,
        )


def dca_plan(symbol: str) -> DcaPlan:
    plan, _ = DcaPlan.objects.get_or_create(symbol=symbol)
    return plan


def dca_buy(symbol: str, burst: bool = False) -> None:
    """Buy the plan's shares once DCA_EVERY of the symbol's changes have passed since its
    last buy, or the rest up to the target at once (burst). Hold once at the target or
    the value cap; a burst cut short by cash leaves the weekly buys running."""
    per_buy, target, max_value = DCA_PLANS[symbol]
    plan = dca_plan(symbol)
    holding = Holding.objects.filter(symbol=symbol).first()
    if plan.phase == "holding" and not holding:  # sold by hand: accumulate again
        plan.phase = "dca"
        plan.save(update_fields=["phase"])
    quote = Quote.objects.get(symbol=symbol)
    account, _ = Account.objects.get_or_create(pk=1)
    last = plan.last_buy_move
    waiting = not burst and last is not None and quote.moves - last < DCA_EVERY
    halted = equity(account, load_prices()) < halt_below(account)
    if not plan.enabled or plan.phase != "dca" or waiting or halted:
        return
    held = holding.quantity if holding else 0
    room = int(max(max_value - quote.price * held, 0) // quote.price)
    want = target - held if burst else per_buy
    shares = min(want, target - held, room, int(account.cash // quote.price))
    if shares > 0:
        trigger = "dca_burst" if burst else "dca"
        buy(account, holding, symbol, shares, quote.price, uuid4(), trigger)
        plan.last_buy_move = quote.moves
    if held + shares >= target or shares == room:  # a burst short on cash keeps going
        plan.phase = "holding"
    plan.save(update_fields=["phase", "last_buy_move"])


@transaction.atomic
def set_dca(symbol: str, enabled: bool) -> dict:
    if symbol not in DCA_PLANS:
        raise TradeError("Dollar-cost averaging runs for SPY and GLD only.")
    load_prices()
    plan = dca_plan(symbol)
    plan.enabled = enabled
    plan.save(update_fields=["enabled"])
    if enabled:
        dca_buy(symbol)
    return get_state()


def move_price(symbol: str, price: Decimal) -> None:
    """One price change: one trading day for this symbol."""
    was_bearish = symbol == REGIME_SYMBOL and market_regime()[2]
    quote = Quote.objects.get(symbol=symbol)
    previous = quote.price if quote.ema is None else quote.ema
    quote.ema = (previous + EMA_ALPHA * (price - previous)).quantize(Decimal("0.0001"))
    quote.price = price
    quote.moves += 1
    if symbol == REGIME_SYMBOL:
        quote.history = (quote.history + [str(price)])[-REGIME_WINDOW:]
    quote.save(update_fields=["price", "ema", "moves", "history"])
    apply_rules(symbol, price)
    re_enter(symbol, price)
    if symbol in DCA_PLANS:
        dca_buy(symbol)
    elif was_bearish and not market_regime()[2]:  # QQQ crossed above its average
        for plan_symbol in DCA_PLANS:
            dca_buy(plan_symbol, burst=True)


def buys_halted() -> bool:
    account, _ = Account.objects.get_or_create(pk=1)
    return equity(account, load_prices()) < halt_below(account)


def alert_kill_switch(was_halted: bool) -> None:
    """Announce the kill switch turning on or off across a price change."""
    account, _ = Account.objects.get_or_create(pk=1)
    value, threshold = equity(account, load_prices()), halt_below(account)
    if value < threshold and not was_halted:
        alert(
            "critical",
            f"Kill switch on: portfolio value ${value:,} is below ${threshold:,} "
            f"({1 - KILL_SWITCH_DRAWDOWN:.0%} of its ${account.equity_peak:,} peak). "
            "New buys are paused.",
        )
    elif was_halted and value >= threshold:
        alert("info", "Kill switch off: portfolio value recovered; buys resume.")


@transaction.atomic
def set_price(symbol: str, raw_price: str) -> dict:
    if symbol not in QUOTES and symbol not in INDEXES:
        raise TradeError("Choose a supported symbol.")
    price = parse_price(raw_price)
    load_prices()
    was_halted = buys_halted()
    move_price(symbol, price)
    record_equity_peak()
    alert_kill_switch(was_halted)
    return get_state()


@transaction.atomic
def reset_kill_switch() -> dict:
    """Restart the peak at current equity; only while the kill switch is on."""
    account, _ = Account.objects.get_or_create(pk=1)
    value = capped_equity(account)
    if value >= halt_below(account):
        raise TradeError("The kill switch is not on; there is nothing to reset.")
    account.equity_peak = value
    account.save(update_fields=["equity_peak"])
    alert("info", f"Kill switch reset: peak restarted at ${value:,}.")
    return get_state()


@transaction.atomic
def tick() -> dict:
    prices = load_prices()
    was_halted = buys_halted()
    # QQQ moves first, so the rules for every quote in this tick see the new regime.
    for symbol in sorted(prices, key=lambda symbol: symbol != REGIME_SYMBOL):
        price = prices[symbol]
        bps = random.randint(-MAX_TICK_BPS, MAX_TICK_BPS)
        moved = (price * (1 + Decimal(bps) / 10_000)).quantize(CENT)
        if bps and moved == price:
            moved += CENT if bps > 0 else -CENT  # small prices still move by a cent
        move_price(symbol, min(MAX_PRICE, max(CENT, moved)))
    record_equity_peak()
    alert_kill_switch(was_halted)
    return get_state()
