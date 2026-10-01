from decimal import Decimal

from django.db import models


class Account(models.Model):
    cash = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal("100000.00"))

    class Meta:
        constraints = [
            models.CheckConstraint(condition=models.Q(cash__gte=0), name="cash_nonnegative")
        ]


class Quote(models.Model):
    symbol = models.CharField(max_length=5, unique=True)
    price = models.DecimalField(max_digits=12, decimal_places=2)


class Holding(models.Model):
    symbol = models.CharField(max_length=5, unique=True)
    quantity = models.PositiveIntegerField()
    average_cost = models.DecimalField(max_digits=12, decimal_places=2)
    # Highest price since the trailing stop took over; null while the hard stop applies.
    trail_peak = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    # Profit-taking levels already taken (0-2); resets when the position closes.
    profit_level = models.PositiveSmallIntegerField(default=0)


class Order(models.Model):
    client_order_id = models.UUIDField(unique=True)
    symbol = models.CharField(max_length=5)
    side = models.CharField(max_length=4)
    quantity = models.PositiveIntegerField()
    price = models.DecimalField(max_digits=12, decimal_places=2)
    total = models.DecimalField(max_digits=18, decimal_places=2)
    # "" for manual orders; "hard_stop", "trailing_stop" or "profit_take" for automatic sells.
    trigger = models.CharField(max_length=13, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-id"]
