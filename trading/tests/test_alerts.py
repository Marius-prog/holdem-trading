from uuid import uuid4

from trading import alerts
from trading.models import Alert
from trading.tests.base import HEADERS, ApiTestCase


class AlertTests(ApiTestCase):
    def messages(self):
        return [
            (a["severity"], a["message"]) for a in self.client.get("/api/state").json()["alerts"]
        ]

    def test_manual_orders_raise_no_alerts(self):
        self.buy("AAPL", 2)
        sell = {"symbol": "AAPL", "side": "sell", "quantity": 2, "client_order_id": str(uuid4())}
        self.client.post("/api/orders", json=sell, headers=HEADERS)
        self.assertEqual(self.messages(), [])

    def test_stop_sale_is_a_warning(self):
        self.buy("AAPL", 2)
        self.set_price("AAPL", "150.00")
        self.assertEqual(self.messages(), [("warning", "Hard stop sold 2 AAPL at $150.00.")])

    def test_trailing_upgrade_alerts_once_not_on_each_ratchet(self):
        self.buy("TSLA", 10)
        self.set_price("TSLA", "276.09")
        self.set_price("TSLA", "280.00")
        self.assertEqual(
            self.messages(),
            [("info", "TSLA stop now trails 12% below its peak (stop $242.95).")],
        )

    def test_automatic_buys_and_takes_are_info(self):
        self.buy("AAPL", 10)
        self.set_price("AAPL", "211.59")  # ladder
        self.client.post("/api/dca", json={"symbol": "SPY", "enabled": True}, headers=HEADERS)
        self.assertEqual(
            self.messages(),
            [
                ("info", "DCA bought 4 SPY at $656.77."),
                ("info", "Ladder bought 10 AAPL at $211.59."),
            ],
        )

    def test_reentry_limit_placement_is_announced(self):
        self.buy("AAPL", 10)
        self.set_price("AAPL", "150.00")  # stop-out
        self.set_price("AAPL", "220.00")
        self.assertEqual(
            self.messages()[0],
            ("info", "Re-entry limit for AAPL: buy 10 at $209.00 or lower."),
        )

    def test_kill_switch_on_off_and_reset(self):
        self.buy("AAPL", 439)  # 99,881.28
        self.set_price("AAPL", "190.00")
        self.assertEqual(
            self.messages()[0],
            (
                "critical",
                (
                    "Kill switch on: portfolio value $83,528.72 is below $85,000.00 "
                    "(85% of its $100,000.00 peak). New buys are paused."
                ),
            ),
        )
        self.set_price("AAPL", "230.00")
        self.assertEqual(
            self.messages()[0], ("info", "Kill switch off: portfolio value recovered; buys resume.")
        )
        self.set_price("AAPL", "190.00")
        self.client.post("/api/kill-switch/reset", headers=HEADERS)
        self.assertEqual(
            self.messages()[0], ("info", "Kill switch reset: peak restarted at $83,528.72.")
        )

    def test_state_lists_the_latest_20_newest_first(self):
        for n in range(25):
            alerts.alert("info", f"note {n}")
        listed = self.client.get("/api/state").json()["alerts"]
        self.assertEqual(len(listed), 20)
        self.assertEqual(listed[0]["message"], "note 24")
        self.assertEqual(set(listed[0]), {"created_at", "severity", "symbol", "message"})

    def test_only_the_last_500_alerts_are_kept(self):
        for n in range(505):
            alerts.alert("info", f"note {n}")
        self.assertEqual(Alert.objects.count(), 500)
        self.assertEqual(Alert.objects.order_by("id").first().message, "note 5")
