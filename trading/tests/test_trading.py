from decimal import Decimal
from unittest.mock import patch
from uuid import uuid4

from django.test import TestCase
from django_bolt.testing import TestClient

from trading.api import api
from trading.models import Account, Holding, Order
from trading.services import get_state, place_order


class TradingTests(TestCase):
    def setUp(self):
        self.client = TestClient(api, base_url="http://testserver")
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def trade(self, **overrides):
        payload = {
            "symbol": "AAPL",
            "side": "buy",
            "quantity": 2,
            "client_order_id": str(uuid4()),
        }
        payload.update(overrides)
        return self.client.post("/api/orders", json=payload, headers={"X-Paper-Trade": "1"})

    def test_initial_state(self):
        response = self.client.get("/api/state")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(
            data["account"],
            {
                "cash": "100000.00",
                "holdings_value": "0.00",
                "total_value": "100000.00",
            },
        )
        self.assertEqual(len(data["quotes"]), 11)
        self.assertEqual(data["holdings"], [])
        self.assertEqual(data["orders"], [])

    def test_buy_sell_and_full_close(self):
        bought = self.trade()
        self.assertEqual(bought.status_code, 200, bought.text)
        self.assertEqual(bought.json()["total"], "455.04")
        state = self.client.get("/api/state").json()
        self.assertEqual(state["account"]["cash"], "99544.96")
        self.assertEqual(state["holdings"][0]["quantity"], 2)
        self.assertEqual(state["holdings"][0]["average_cost"], "227.52")
        self.assertEqual(state["holdings"][0]["unrealized_pnl"], "0.00")
        self.assertEqual(self.trade(side="sell", quantity=1).status_code, 200)
        self.assertEqual(Holding.objects.get().quantity, 1)
        self.assertEqual(self.trade(side="sell", quantity=1).status_code, 200)
        self.assertFalse(Holding.objects.exists())
        self.assertEqual(Account.objects.get(pk=1).cash, Decimal("100000.00"))
        self.assertEqual(Order.objects.count(), 3)

    def test_replay_does_not_execute_twice_and_changed_payload_conflicts(self):
        key = str(uuid4())
        original = self.trade(client_order_id=key)
        repeated = self.trade(client_order_id=key)
        self.assertEqual(original.json(), repeated.json())
        conflict = self.trade(client_order_id=key, quantity=3)
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(Order.objects.count(), 1)
        self.assertEqual(Holding.objects.get().quantity, 2)

    def test_invalid_orders_preserve_all_state(self):
        before = get_state()
        invalid = [
            {"quantity": 0},
            {"quantity": -1},
            {"quantity": 1.5},
            {"quantity": True},
            {"quantity": "2"},
            {"quantity": 1_000_001},
            {"quantity": 1_000_000},
            {"side": "sell"},
            {"side": "hold"},
            {"symbol": "UNKNOWN"},
            {"client_order_id": "invalid"},
        ]
        for payload in invalid:
            with self.subTest(payload=payload):
                response = self.trade(**payload)
                self.assertIn(response.status_code, [400, 422], response.text)
                self.assertEqual(get_state(), before)

    def test_oversell_preserves_state(self):
        self.trade(quantity=2)
        before = get_state()
        response = self.trade(side="sell", quantity=3)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(get_state(), before)

    def test_header_required_and_cors_not_enabled(self):
        payload = {"symbol": "AAPL", "side": "buy", "quantity": 1, "client_order_id": str(uuid4())}
        response = self.client.post("/api/orders", json=payload)
        self.assertEqual(response.status_code, 403)
        self.assertFalse(Order.objects.exists())
        response = self.client.options(
            "/api/orders",
            headers={
                "Origin": "https://example.com",
                "Access-Control-Request-Headers": "X-Paper-Trade",
                "Access-Control-Request-Method": "POST",
            },
        )
        self.assertNotIn("access-control-allow-origin", response.headers)

    def test_foreign_host_is_refused(self):
        # DNS rebinding: a page on evil.example resolving to 127.0.0.1 is same-origin.
        foreign = {"Host": "evil.example:8765"}
        payload = {"symbol": "AAPL", "side": "buy", "quantity": 1, "client_order_id": str(uuid4())}
        response = self.client.post(
            "/api/orders", json=payload, headers={**foreign, "X-Paper-Trade": "1"}
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Order.objects.exists())
        self.assertEqual(self.client.get("/api/state", headers=foreign).status_code, 400)
        local = {"Host": "127.0.0.1:8765"}
        self.assertEqual(self.client.get("/api/state", headers=local).status_code, 200)

    def test_failed_order_write_rolls_back_cash_and_holdings(self):
        before = get_state()
        with (
            patch.object(Order.objects, "create", side_effect=RuntimeError("write failed")),
            self.assertRaises(RuntimeError),
        ):
            place_order("AAPL", "buy", 2, uuid4())
        self.assertEqual(get_state(), before)

    def test_orders_are_newest_first_and_limited(self):
        for _ in range(101):
            place_order("NVDA", "buy", 1, uuid4())
        orders = get_state()["orders"]
        self.assertEqual(len(orders), 100)
        self.assertGreater(orders[0]["id"], orders[-1]["id"])

    def test_dashboard_renders(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response.headers["content-type"])
