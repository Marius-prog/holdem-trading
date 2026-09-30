from decimal import Decimal

from django.db import models


class Account(models.Model):
    cash = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal("100000.00"))

    class Meta:
        constraints = [
            models.CheckConstraint(condition=models.Q(cash__gte=0), name="cash_nonnegative")
        ]


class Holding(models.Model):
    symbol = models.CharField(max_length=5, unique=True)
    quantity = models.PositiveIntegerField()
    average_cost = models.DecimalField(max_digits=12, decimal_places=2)


class Order(models.Model):
    client_order_id = models.UUIDField(unique=True)
    symbol = models.CharField(max_length=5)
    side = models.CharField(max_length=4)
    quantity = models.PositiveIntegerField()
    price = models.DecimalField(max_digits=12, decimal_places=2)
    total = models.DecimalField(max_digits=18, decimal_places=2)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-id"]
