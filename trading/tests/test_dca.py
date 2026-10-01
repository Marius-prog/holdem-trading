from decimal import Decimal

from trading.models import Account, Holding, Order
from trading.tests.base import HEADERS, ApiTestCase


class SpyDcaTests(ApiTestCase):
    def dca(self):
        return self.client.get("/api/state").json()["dca"]

    def toggle(self, enabled, headers=HEADERS):
        return self.client.post("/api/dca", json={"enabled": enabled}, headers=headers)

    def spy_shares(self):
        holding = Holding.objects.filter(symbol="SPY").first()
        return holding.quantity if holding else 0

    def tick_spy(self, times, price="656.77"):
        for _ in range(times):
            self.set_price("SPY", price)

    def test_spy_is_a_quote_and_dca_starts_off(self):
        quotes = {q["symbol"]: q["price"] for q in self.client.get("/api/state").json()["quotes"]}
        self.assertEqual(quotes["SPY"], "656.77")
        self.assertEqual(
            self.dca(),
            {
                "enabled": False,
                "phase": "dca",
                "shares": 0,
                "target": 39,
                "next_buy_in": None,
                "cycle": 1,
            },
        )
        self.tick_spy(8)
        self.assertEqual(self.spy_shares(), 0)

    def test_starting_buys_4_now_then_4_every_7_spy_changes(self):
        response = self.toggle(True)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.spy_shares(), 4)
        self.assertEqual(Order.objects.get().trigger, "dca")
        self.assertEqual(self.dca()["next_buy_in"], 7)
        self.tick_spy(6)
        self.assertEqual((self.spy_shares(), self.dca()["next_buy_in"]), (4, 1))
        self.tick_spy(1)
        self.assertEqual(self.spy_shares(), 8)

    def test_dca_stops_at_39_shares_and_holds(self):
        self.toggle(True)
        self.tick_spy(7 * 9)  # 4 + 8 x 4 + 3
        self.assertEqual(self.spy_shares(), 39)
        self.assertEqual(self.dca()["phase"], "holding")
        self.tick_spy(7)
        self.assertEqual(self.spy_shares(), 39)

    def test_dca_holds_at_the_30k_value_cap(self):
        self.set_price("SPY", "1000.00")
        Holding.objects.create(
            symbol="SPY",
            quantity=28,
            average_cost=Decimal("1000.00"),
            entry_price=Decimal("1000.00"),
        )
        self.toggle(True)  # room for 2 more shares under $30,000
        self.assertEqual((self.spy_shares(), self.dca()["phase"]), (30, "holding"))

    def test_spy_uses_12_percent_hard_and_5_percent_trailing_stops(self):
        self.toggle(True)
        self.assertEqual(self.holding("SPY")["stop_price"], "577.95")
        self.set_price("SPY", "702.74")  # +6.99%
        self.assertEqual(self.holding("SPY")["stop_type"], "hard")
        self.set_price("SPY", "702.75")
        self.assertEqual(self.holding("SPY")["stop_price"], "667.61")

    def test_spy_gets_no_ladders_profit_takes_or_reentry(self):
        self.toggle(True)
        self.set_price("SPY", "591.09")  # -10%
        self.set_price("SPY", "860.00")  # +31%
        self.assertEqual(Order.objects.exclude(trigger="dca").count(), 0)
        self.set_price("SPY", "800.00")  # trailing stop 817.00 sells
        quote = next(
            q for q in self.client.get("/api/state").json()["quotes"] if q["symbol"] == "SPY"
        )
        self.assertIsNone(quote["reentry_limit"])

    def test_stop_out_restarts_dca_one_week_later_in_a_new_cycle(self):
        self.toggle(True)
        self.set_price("SPY", "577.95")  # 12% hard stop sells all 4
        self.assertEqual(self.spy_shares(), 0)
        self.assertEqual((self.dca()["phase"], self.dca()["cycle"]), ("dca", 2))
        self.tick_spy(6, "577.95")
        self.assertEqual(self.spy_shares(), 0)
        self.tick_spy(1, "577.95")
        self.assertEqual(self.spy_shares(), 4)

    def test_qqq_crossing_above_its_average_buys_the_rest_at_once(self):
        self.toggle(True)
        self.set_price("QQQ", "490.00")  # already bullish: no burst
        self.assertEqual(self.spy_shares(), 4)
        self.set_price("QQQ", "470.00")  # bearish
        self.set_price("QQQ", "500.00")  # crosses above its average
        self.assertEqual(self.spy_shares(), 39)
        self.assertEqual(Order.objects.order_by("-id").first().trigger, "dca_burst")
        self.assertEqual(self.dca()["phase"], "holding")

    def test_kill_switch_pauses_dca_buys(self):
        self.client.get("/api/state")  # create the account
        Account.objects.filter(pk=1).update(equity_peak=Decimal("200000.00"))
        self.toggle(True)
        self.tick_spy(7)
        self.assertEqual(self.spy_shares(), 0)
        self.assertTrue(self.dca()["enabled"])

    def test_stopping_dca_ends_its_buys_and_keeps_the_position(self):
        self.toggle(True)
        self.assertEqual(self.toggle(False).status_code, 200)
        self.tick_spy(14)
        self.assertEqual(self.spy_shares(), 4)
        self.assertFalse(self.dca()["enabled"])

    def test_dca_toggle_is_guarded(self):
        self.assertEqual(self.toggle(True, headers={}).status_code, 403)
        foreign = {**HEADERS, "Host": "evil.example:8765"}
        self.assertEqual(self.toggle(True, headers=foreign).status_code, 400)
        self.assertFalse(self.dca()["enabled"])
