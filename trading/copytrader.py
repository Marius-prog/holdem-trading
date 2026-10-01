"""Strategy 2: mirror congressional trade filings entered by the user."""

import re
from datetime import date, timedelta
from decimal import Decimal
from uuid import uuid4

from django.db import transaction
from django.utils import timezone

from .models import Account, Filing, Holding
from .services import (
    COPY_BUCKETS,
    COPY_SYMBOLS,
    TradeError,
    apply_rules,
    buy,
    buys_halted,
    get_state,
    load_prices,
    sell,
)

COPY_MAX_PER_STOCK = Decimal("10000.00")
COPY_LAG_DAYS = 45
COPY_SELL_DIVISOR = 3  # each sell filing sells a third of the position
SYMBOL = re.compile(r"[A-Z]{1,5}")
KILL_SWITCH_SKIP = "Not mirrored: the kill switch is on."


def mirror(symbol: str, side: str, bucket: int, traded_on: date) -> str:
    """Place the copy order for a filing and say what happened."""
    if traded_on < timezone.localdate() - timedelta(days=COPY_LAG_DAYS):
        return f"Not mirrored: traded more than {COPY_LAG_DAYS} days ago."
    if symbol not in COPY_SYMBOLS:
        return f"Not mirrored: {symbol} is not in the copy universe ({', '.join(COPY_SYMBOLS)})."
    account, _ = Account.objects.get_or_create(pk=1)
    holding = Holding.objects.filter(symbol=symbol).first()
    price = load_prices()[symbol]
    if side == "sell":
        if not holding:
            return f"Not mirrored: no {symbol} position to sell."
        shares = holding.quantity // COPY_SELL_DIVISOR
        if not shares:
            return (
                f"Not mirrored: a third of {holding.quantity} {symbol} shares rounds down to zero."
            )
        sell(account, holding, shares, price, uuid4(), "copy")
        return f"Sold {shares} {symbol} at ${price:,}."
    if buys_halted():
        return KILL_SWITCH_SKIP
    cost = holding.average_cost * holding.quantity if holding else Decimal(0)
    size = COPY_BUCKETS[bucket][1]
    spend, reason = min(  # the tightest limit decides, and explains a skipped buy
        (size, f"the ${size:,} mirror size is below one {symbol} share (${price:,})"),
        (COPY_MAX_PER_STOCK - cost, f"{symbol} is at its ${COPY_MAX_PER_STOCK:,.0f} copy limit"),
        (account.cash, "not enough cash"),
        key=lambda limit: limit[0],
    )
    shares = int(max(spend, 0) // price)
    if not shares:
        return f"Not mirrored: {reason}."
    buy(account, holding, symbol, shares, price, uuid4(), "copy")
    if holding:  # restart the trail on the full position if it is still up enough
        apply_rules(symbol, price, stops_only=True)
    return f"Bought {shares} {symbol} at ${price:,}."


@transaction.atomic
def record_filing(filer: str, symbol: str, side: str, bucket: int, traded_on: date) -> dict:
    filer, symbol = filer.strip(), symbol.strip().upper()
    if not 1 <= len(filer) <= 80 or not any(char.isalnum() for char in filer):
        raise TradeError("Enter the filer's name (up to 80 characters).")
    if not SYMBOL.fullmatch(symbol):
        raise TradeError("Enter a ticker of 1 to 5 letters.")
    if side not in {"buy", "sell"}:
        raise TradeError("Side must be buy or sell.")
    if not 0 <= bucket < len(COPY_BUCKETS):
        raise TradeError("Choose one of the trade-size ranges.")
    if traded_on > timezone.localdate():
        raise TradeError("The trade date cannot be in the future.")
    # The same trade by the same filer (in any capitalisation) counts once, unless the
    # kill switch stopped it; then entering it again replaces it and mirrors it again.
    trade = {"symbol": symbol, "side": side, "bucket": bucket, "traded_on": traded_on}
    earlier = [f for f in Filing.objects.filter(**trade) if f.filer.casefold() == filer.casefold()]
    if any(f.outcome != KILL_SWITCH_SKIP for f in earlier):
        raise TradeError("This filing was already recorded.", 409)
    Filing.objects.filter(pk__in=[f.pk for f in earlier]).delete()
    Filing.objects.create(filer=filer, **trade, outcome=mirror(symbol, side, bucket, traded_on))
    return get_state()
