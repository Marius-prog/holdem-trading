from .models import Alert, Order

MAX_ALERTS = 500
ORDER_ALERTS = {  # trigger: (severity, verb)
    "hard_stop": ("warning", "Hard stop sold"),
    "trailing_stop": ("warning", "Trailing stop sold"),
    "profit_take": ("info", "Profit take sold"),
    "ladder": ("info", "Ladder bought"),
    "reentry": ("info", "Re-entry bought"),
    "dca": ("info", "DCA bought"),
    "dca_burst": ("info", "DCA burst bought"),
}


def alert(severity: str, message: str, symbol: str = "") -> None:
    Alert.objects.create(severity=severity, message=message, symbol=symbol)
    beyond = list(
        Alert.objects.order_by("-id").values_list("id", flat=True)[MAX_ALERTS : MAX_ALERTS + 1]
    )
    if beyond:
        Alert.objects.filter(id__lte=beyond[0]).delete()


def order_alert(order: Order) -> None:
    """Announce an automatic order; manual orders (no trigger) stay quiet."""
    if order.trigger:
        severity, verb = ORDER_ALERTS[order.trigger]
        alert(
            severity, f"{verb} {order.quantity} {order.symbol} at ${order.price:,}.", order.symbol
        )


def recent(limit: int = 20) -> list[dict]:
    return [
        {
            "created_at": a.created_at.isoformat(),
            "severity": a.severity,
            "symbol": a.symbol,
            "message": a.message,
        }
        for a in Alert.objects.order_by("-id")[:limit]
    ]
