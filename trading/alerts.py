from .models import Alert, Order

MAX_ALERTS = 500
ORDER_ALERTS = {  # trigger: (severity, label)
    "hard_stop": ("warning", "Hard stop"),
    "trailing_stop": ("warning", "Trailing stop"),
    "profit_take": ("info", "Profit take"),
    "ladder": ("info", "Ladder"),
    "reentry": ("info", "Re-entry"),
    "dca": ("info", "DCA"),
    "dca_burst": ("info", "DCA burst"),
    "copy": ("info", "Copy trade"),
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
        severity, label = ORDER_ALERTS[order.trigger]
        verb = "bought" if order.side == "buy" else "sold"
        message = f"{label} {verb} {order.quantity} {order.symbol} at ${order.price:,}."
        alert(severity, message, order.symbol)


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
