from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from trading.models import Account, Holding, Order
from trading.tests.base import HEADERS, ApiTestCase


class CopyTraderTests(ApiTestCase):
    def file(
        self, symbol="JPM", side="buy", bucket=1, days_ago=3, filer="A. Member", headers=HEADERS
    ):
        filing = {
            "filer": filer,
            "symbol": symbol,
            "side": side,
            "bucket": bucket,
            "traded_on": (timezone.localdate() - timedelta(days=days_ago)).isoformat(),
        }
        return self.client.post("/api/filings", json=filing, headers=headers)

    def outcome(self):
        return self.client.get("/api/state").json()["copy"]["filings"][0]["outcome"]

    def shares(self, symbol="JPM"):
        holding = Holding.objects.filter(symbol=symbol).first()
        return holding.quantity if holding else 0

    def test_buy_filing_mirrors_a_scaled_dollar_size(self):
        response = self.file(bucket=1)  # $15K-$50K: mirror $1,500
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.shares(), 5)  # 1500 // 293.88
        order = Order.objects.get()
        self.assertEqual((order.trigger, order.side, str(order.price)), ("copy", "buy", "293.88"))
        self.assertEqual(self.outcome(), "Bought 5 JPM at $293.88.")

    def test_copy_buys_stop_at_10k_cost_per_stock(self):
        for days_ago in (1, 2, 3):  # three $5,000 mirrors of GS at $858.00
            self.file("GS", bucket=4, days_ago=days_ago)
        self.assertEqual(self.shares("GS"), 11)  # 5 + 5 + 1 (room $1,420)
        self.file("GS", bucket=4, days_ago=4)
        self.assertEqual(self.outcome(), "Not mirrored: GS is at its $10,000 copy limit.")

    def test_sell_filing_sells_a_third(self):
        self.file(bucket=1)  # 5 JPM
        self.file(side="sell", days_ago=2)
        self.assertEqual(self.shares(), 4)
        self.assertEqual(self.outcome(), "Sold 1 JPM at $293.88.")

    def test_sell_without_a_position_is_recorded_only(self):
        self.file(side="sell")
        self.assertEqual(self.outcome(), "Not mirrored: no JPM position to sell.")
        self.assertFalse(Order.objects.exists())

    def test_trades_older_than_45_days_are_not_mirrored(self):
        self.file(days_ago=46)
        self.assertEqual(self.outcome(), "Not mirrored: traded more than 45 days ago.")
        self.file(days_ago=45)
        self.assertEqual(self.shares(), 5)

    def test_symbols_outside_the_copy_universe_are_recorded_only(self):
        self.assertEqual(self.file("AAPL").status_code, 200)
        self.assertEqual(
            self.outcome(), "Not mirrored: AAPL is not in the copy universe (JPM, GS, KO)."
        )
        self.file("XOM", days_ago=4)
        self.assertFalse(Order.objects.exists())

    def test_duplicate_filing_is_refused(self):
        self.file()
        self.assertEqual(self.file().status_code, 409)
        self.assertEqual(self.shares(), 5)

    def test_kill_switch_pauses_copy_buys_not_sells(self):
        self.file(bucket=1)
        Account.objects.filter(pk=1).update(equity_peak=Decimal("200000.00"))
        self.file(bucket=1, days_ago=4)
        self.assertEqual(self.outcome(), "Not mirrored: the kill switch is on.")
        self.file(side="sell", days_ago=5)
        self.assertEqual(self.shares(), 4)

    def test_copy_positions_get_a_10_percent_trail_only_after_15_percent(self):
        self.file(bucket=1)
        self.assertIsNone(self.holding("JPM")["stop_price"])
        self.set_price("JPM", "200.00")  # -32%: no hard stop, ladder or re-entry
        self.assertEqual(self.shares(), 5)
        self.set_price("JPM", "337.96")  # +14.99% on 293.88
        self.assertIsNone(self.holding("JPM")["stop_price"])
        self.set_price("JPM", "337.97")
        self.assertEqual(self.holding("JPM")["stop_price"], "304.17")
        self.set_price("JPM", "304.17")
        self.assertEqual(self.shares(), 0)
        self.assertEqual(Order.objects.exclude(trigger="copy").get().trigger, "trailing_stop")

    def alerts(self):
        return [a["message"] for a in self.client.get("/api/state").json()["alerts"]]

    def test_adding_to_a_trailing_copy_position_extends_the_trail(self):
        self.set_price("KO", "100.00")
        self.file("KO", bucket=4)  # 50 at 100.00
        self.set_price("KO", "115.00")
        self.set_price("KO", "120.00")  # trailing: stop 108.00
        self.file("KO", bucket=4, days_ago=4)  # 41 more at 120.00: avg 109.01, +10%
        self.assertEqual(self.shares("KO"), 91)
        self.assertEqual(self.holding("KO")["stop_price"], "108.00")
        self.assertEqual(self.alerts()[0], "Copy trade bought 41 KO at $120.00.")
        self.set_price("KO", "60.00")  # the trail covers all 91 shares
        self.assertEqual(self.shares("KO"), 0)

    def test_adding_to_a_copy_position_still_up_15_percent_keeps_trailing(self):
        self.file(bucket=1)  # 5 JPM at 293.88
        self.set_price("JPM", "400.00")
        self.file(bucket=0, days_ago=4)  # 1 more at 400.00: avg 311.57, still +28%
        self.assertEqual(self.holding("JPM")["stop_price"], "360.00")
        self.assertEqual(self.alerts()[0], "Copy trade bought 1 JPM at $400.00.")

    def test_state_gives_the_server_date_for_the_form(self):
        today = self.client.get("/api/state").json()["copy"]["today"]
        self.assertEqual(today, timezone.localdate().isoformat())

    def test_mirror_size_below_one_share_says_so(self):
        self.file("GS", bucket=0)  # $500 < $858.00
        self.assertEqual(
            self.outcome(), "Not mirrored: the $500.00 mirror size is below one GS share ($858.00)."
        )

    def test_sell_of_a_tiny_position_explains_the_rounding(self):
        self.set_price("KO", "200.00")
        self.file("KO", bucket=0)  # 2 shares
        self.file("KO", side="sell", days_ago=4)
        self.assertEqual(
            self.outcome(), "Not mirrored: a third of 2 KO shares rounds down to zero."
        )

    def test_filing_refused_by_the_kill_switch_can_be_entered_again(self):
        self.file(bucket=1)  # 5 JPM, creates the account
        Account.objects.filter(pk=1).update(equity_peak=Decimal("200000.00"))
        self.file(bucket=1, days_ago=4)
        self.assertEqual(self.outcome(), "Not mirrored: the kill switch is on.")
        self.assertEqual(self.file(bucket=1, days_ago=4).status_code, 200)  # still halted
        self.client.post("/api/kill-switch/reset", headers=HEADERS)
        self.assertEqual(self.file(bucket=1, days_ago=4, filer="a. member").status_code, 200)
        self.assertEqual(self.outcome(), "Bought 5 JPM at $293.88.")
        self.assertEqual(self.shares(), 10)
        self.assertEqual(len(self.client.get("/api/state").json()["copy"]["filings"]), 2)
        self.assertEqual(self.file(bucket=1, days_ago=4).status_code, 409)  # mirrored now

    def test_duplicates_ignore_filer_case(self):
        self.file(filer="A. Member")
        self.assertEqual(self.file(filer="a. member").status_code, 409)
        self.assertEqual(self.shares(), 5)
        self.file(filer="Žygis", days_ago=4)
        self.assertEqual(self.file(filer="žygis", days_ago=4).status_code, 409)

    def test_filings_are_validated_and_guarded(self):
        self.assertEqual(self.file(headers={}).status_code, 403)
        self.assertEqual(self.file(headers={**HEADERS, "Host": "evil.example"}).status_code, 400)
        for kwargs in [
            {"side": "hold"},
            {"bucket": 5},
            {"bucket": -1},
            {"days_ago": -1},
            {"filer": "  "},
            {"filer": "\u200b"},
            {"filer": "x" * 81},
            {"symbol": "TOOLONG"},
            {"symbol": "jp m"},
        ]:
            with self.subTest(**kwargs):
                self.assertEqual(self.file(**kwargs).status_code, 400)
        self.assertEqual(self.client.get("/api/state").json()["copy"]["filings"], [])
