from decimal import Decimal
from unittest.mock import patch
from uuid import uuid4

from django.test import TestCase
from django_bolt.testing import TestClient

from trading.api import api
from trading.models import Account, Holding, Order
from trading.services import get_state

HEADERS = {"X-Paper-Trade": "1"}


class StopProtectionTests(TestCase):
    def setUp(self):
        self.client = TestClient(api, base_url="http://testserver")
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def buy(self, symbol="AAPL", quantity=2):
        payload = {
            "symbol": symbol,
            "side": "buy",
            "quantity": quantity,
            "client_order_id": str(uuid4()),
        }
        response = self.client.post("/api/orders", json=payload, headers=HEADERS)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def set_price(self, symbol, price, headers=HEADERS):
        return self.client.post(
            "/api/quotes", json={"symbol": symbol, "price": price}, headers=headers
        )

    def holding(self, symbol):
        state = self.client.get("/api/state").json()
        return next((item for item in state["holdings"] if item["symbol"] == symbol), None)

    def test_buy_gets_hard_stop_25_percent_below_average_cost(self):
        self.buy()
        holding = self.holding("AAPL")
        self.assertEqual(holding["stop_type"], "hard")
        self.assertEqual(holding["stop_price"], "170.64")

    def test_price_above_stop_moves_value_and_keeps_position(self):
        self.buy()
        response = self.set_price("AAPL", "171.00")
        self.assertEqual(response.status_code, 200, response.text)
        state = response.json()
        quote = next(item for item in state["quotes"] if item["symbol"] == "AAPL")
        self.assertEqual(quote["price"], "171.00")
        self.assertEqual(state["holdings"][0]["market_value"], "342.00")
        self.assertEqual(state["holdings"][0]["unrealized_pnl"], "-113.04")
        self.assertEqual(Order.objects.count(), 1)

    def test_hard_stop_sells_whole_position_at_current_price_on_gap(self):
        self.buy()
        state = self.set_price("AAPL", "150.00").json()
        self.assertEqual(state["holdings"], [])
        stop_order = state["orders"][0]
        self.assertEqual(stop_order["side"], "sell")
        self.assertEqual(stop_order["quantity"], 2)
        self.assertEqual(stop_order["price"], "150.00")
        self.assertEqual(stop_order["trigger"], "hard_stop")
        self.assertEqual(state["orders"][1]["trigger"], "")
        self.assertEqual(Account.objects.get(pk=1).cash, Decimal("99844.96"))

    def test_price_equal_to_stop_triggers(self):
        self.buy()
        self.set_price("AAPL", "170.64")
        self.assertFalse(Holding.objects.exists())

    def test_seven_percent_gain_upgrades_to_trailing_stop_that_only_ratchets_up(self):
        self.buy("TSLA", 10)
        self.set_price("TSLA", "276.08")  # +6.99%: still the hard stop
        self.assertEqual(self.holding("TSLA")["stop_type"], "hard")
        self.set_price("TSLA", "276.09")
        holding = self.holding("TSLA")
        self.assertEqual((holding["stop_type"], holding["stop_price"]), ("trailing", "242.96"))
        self.set_price("TSLA", "300.00")
        self.assertEqual(self.holding("TSLA")["stop_price"], "264.00")
        self.set_price("TSLA", "280.00")
        self.assertEqual(self.holding("TSLA")["stop_price"], "264.00")
        state = self.set_price("TSLA", "264.00").json()
        self.assertEqual(state["holdings"], [])
        self.assertEqual(state["orders"][0]["trigger"], "trailing_stop")
        self.assertEqual(state["orders"][0]["total"], "2640.00")

    def test_buy_at_moved_price_fills_there_and_reaverages_cost(self):
        self.buy()
        self.set_price("AAPL", "200.00")
        self.assertEqual(self.buy()["price"], "200.00")
        holding = self.holding("AAPL")
        self.assertEqual(holding["quantity"], 4)
        self.assertEqual(holding["average_cost"], "213.76")
        self.assertEqual(holding["stop_price"], "160.32")

    def test_set_price_requires_header_and_rejects_invalid_input(self):
        self.buy()
        before = get_state()
        self.assertEqual(self.set_price("AAPL", "100.00", headers={}).status_code, 403)
        for symbol, price in [
            ("XYZ", "100.00"),
            ("AAPL", "0"),
            ("AAPL", "-5.00"),
            ("AAPL", "1.005"),
            ("AAPL", "1000000.01"),
            ("AAPL", "abc"),
            ("AAPL", "NaN"),
            ("AAPL", "Infinity"),
        ]:
            with self.subTest(symbol=symbol, price=price):
                self.assertEqual(self.set_price(symbol, price).status_code, 400)
        self.assertNotEqual(self.set_price("AAPL", 100).status_code, 200)
        self.assertEqual(get_state(), before)

    def test_price_endpoints_refuse_foreign_host(self):
        foreign = {**HEADERS, "Host": "evil.example:8765"}
        self.assertEqual(self.set_price("AAPL", "100.00", headers=foreign).status_code, 400)
        self.assertEqual(self.client.post("/api/tick", headers=foreign).status_code, 400)
        self.assertEqual(self.client.post("/api/tick").status_code, 403)

    def test_tick_moves_every_price_within_three_percent(self):
        before = {item["symbol"]: Decimal(item["price"]) for item in get_state()["quotes"]}
        response = self.client.post("/api/tick", headers=HEADERS)
        self.assertEqual(response.status_code, 200, response.text)
        for item in response.json()["quotes"]:
            old, new = before[item["symbol"]], Decimal(item["price"])
            self.assertLessEqual(abs(new - old), old * Decimal("0.03") + Decimal("0.01"))

    def test_tick_applies_stops_and_never_drops_price_below_one_cent(self):
        self.buy()
        self.set_price("AAPL", "172.00")
        self.set_price("MSFT", "0.01")
        with patch("trading.services.random.randint", return_value=-300):
            state = self.client.post("/api/tick", headers=HEADERS).json()
        prices = {item["symbol"]: item["price"] for item in state["quotes"]}
        self.assertEqual(prices["AAPL"], "166.84")
        self.assertEqual(prices["MSFT"], "0.01")
        self.assertEqual(state["holdings"], [])
        self.assertEqual(state["orders"][0]["trigger"], "hard_stop")
