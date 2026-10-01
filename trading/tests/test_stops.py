from decimal import Decimal
from unittest.mock import patch
from uuid import uuid4

from django.test import TestCase
from django_bolt.testing import TestClient

from trading.api import api
from trading.models import Account, Holding, Order, Quote
from trading.services import MAX_CASH, get_state

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
        Holding.objects.update(ladder_level=3)  # ladder used up, so only the stop applies
        response = self.set_price("AAPL", "171.00")  # 1 cent above the 170.64 stop
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
        self.assertEqual((holding["stop_type"], holding["stop_price"]), ("trailing", "242.95"))
        self.set_price("TSLA", "300.00")
        self.assertEqual(self.holding("TSLA")["stop_price"], "264.00")
        self.set_price("TSLA", "280.00")
        self.assertEqual(self.holding("TSLA")["stop_price"], "264.00")
        state = self.set_price("TSLA", "264.00").json()
        self.assertEqual(state["holdings"], [])
        self.assertEqual(state["orders"][0]["trigger"], "trailing_stop")
        self.assertEqual(state["orders"][0]["total"], "1848.00")  # 7 left after +15% take

    def test_buy_at_moved_price_fills_there_and_reaverages_cost(self):
        self.buy()
        self.set_price("AAPL", "215.00")
        self.assertEqual(self.buy()["price"], "215.00")
        holding = self.holding("AAPL")
        self.assertEqual(holding["quantity"], 4)
        self.assertEqual(holding["average_cost"], "221.26")
        self.assertEqual(holding["stop_price"], "165.94")

    def test_adding_shares_resets_trailing_stop_to_hard_stop_on_new_average(self):
        self.set_price("AAPL", "100.00")
        self.buy("AAPL", 10)
        self.set_price("AAPL", "110.00")
        self.assertEqual(self.holding("AAPL")["stop_type"], "trailing")
        self.buy("AAPL", 400)
        holding = self.holding("AAPL")
        self.assertEqual(holding["average_cost"], "109.76")
        self.assertEqual((holding["stop_type"], holding["stop_price"]), ("hard", "82.32"))

    def test_adding_shares_still_seven_percent_up_restarts_trail_at_current_price(self):
        self.set_price("AAPL", "100.00")
        self.buy("AAPL", 10)
        self.set_price("AAPL", "114.00")
        self.set_price("AAPL", "112.00")  # trail stays at peak 114 (stop 100.32)
        self.buy("AAPL", 1)
        holding = self.holding("AAPL")
        self.assertEqual(holding["average_cost"], "101.09")
        self.assertEqual((holding["stop_type"], holding["stop_price"]), ("trailing", "98.56"))

    def test_new_position_at_one_cent_is_not_stopped_out_by_its_own_buy(self):
        self.set_price("AAPL", "0.01")
        self.buy("AAPL", 5)
        self.assertEqual(Holding.objects.get().quantity, 5)
        self.assertEqual(Order.objects.count(), 1)

    def test_profit_takes_sell_30_percent_at_15_and_25_percent_gains(self):
        self.buy("TSLA", 10)  # 258.02
        for price, shares in [("296.72", 10), ("296.73", 7), ("322.52", 7), ("322.53", 4)]:
            self.set_price("TSLA", price)
            self.assertEqual(Holding.objects.get().quantity, shares, price)
        takes = Order.objects.filter(trigger="profit_take").order_by("id")
        self.assertEqual(
            [(o.quantity, str(o.price)) for o in takes], [(3, "296.73"), (3, "322.53")]
        )
        state = self.set_price("TSLA", "400.00").json()
        self.assertEqual(Holding.objects.get().quantity, 4)
        self.assertEqual(Order.objects.filter(trigger="profit_take").count(), 2)
        self.assertEqual(state["holdings"][0]["stop_price"], "352.00")

    def test_gap_through_both_profit_levels_takes_both(self):
        self.buy("TSLA", 10)
        state = self.set_price("TSLA", "400.00").json()
        self.assertEqual(state["holdings"][0]["quantity"], 4)
        self.assertEqual(
            [(o["trigger"], o["quantity"]) for o in state["orders"][:2]],
            [("profit_take", 3), ("profit_take", 3)],
        )

    def test_profit_takes_round_down_and_skip_zero_share_sales(self):
        self.buy("AAPL", 2)
        self.set_price("AAPL", "300.00")
        self.assertEqual(Holding.objects.get().quantity, 2)
        self.assertEqual(Holding.objects.get().profit_level, 0)  # nothing sold, still open
        self.assertEqual(Order.objects.count(), 1)

    def test_profit_level_skipped_for_size_fires_after_the_position_grows(self):
        self.buy("AAPL", 2)
        self.set_price("AAPL", "300.00")  # +31.9%, but 30% of 2 shares rounds to 0
        self.buy("AAPL", 8)  # avg 285.50
        self.set_price("AAPL", "330.00")  # +15.6% on the new average
        self.assertEqual(Holding.objects.get().quantity, 7)
        self.assertEqual(Order.objects.filter(trigger="profit_take").count(), 1)

    def test_buying_never_triggers_profit_takes_they_wait_for_a_price_move(self):
        self.set_price("MSFT", "100.00")
        self.buy("MSFT", 3)
        self.set_price("MSFT", "200.00")  # +100%, but 30% of 3 shares rounds to 0
        self.buy("MSFT", 1)
        self.assertEqual(Holding.objects.get().quantity, 4)
        self.assertFalse(Order.objects.filter(trigger="profit_take").exists())
        self.set_price("MSFT", "200.00")  # the next price move takes 1, then 1
        self.assertEqual(Holding.objects.get().quantity, 2)

    def test_profit_levels_reset_after_the_position_closes(self):
        self.buy("TSLA", 10)
        self.set_price("TSLA", "400.00")  # both takes; trailing stop 352.00
        self.set_price("TSLA", "352.00")  # trailing stop closes the last 4
        self.assertFalse(Holding.objects.exists())
        self.buy("TSLA", 10)  # at 352.00
        self.set_price("TSLA", "404.80")  # +15% on the new position
        self.assertEqual(Holding.objects.get().quantity, 7)

    def test_equity_peak_follows_price_rises_and_sets_the_halt_threshold(self):
        risk = self.client.get("/api/state").json()["risk"]
        self.assertEqual(
            risk, {"equity_peak": "100000.00", "halt_below": "85000.00", "buys_halted": False}
        )
        self.buy("AAPL", 100)  # 22,752.00
        self.set_price("AAPL", "240.00")  # equity 101,248.00
        self.set_price("AAPL", "230.00")  # the peak does not fall
        risk = self.client.get("/api/state").json()["risk"]
        self.assertEqual((risk["equity_peak"], risk["halt_below"]), ("101248.00", "86060.80"))

    def test_kill_switch_refuses_buys_until_equity_recovers_and_allows_sells(self):
        first = self.buy("AAPL", 100)
        Account.objects.filter(pk=1).update(equity_peak=Decimal("120000.00"))  # halt < 102,000
        self.assertTrue(self.client.get("/api/state").json()["risk"]["buys_halted"])
        order = {"symbol": "MSFT", "side": "buy", "quantity": 1, "client_order_id": str(uuid4())}
        refused = self.client.post("/api/orders", json=order, headers=HEADERS)
        self.assertEqual(refused.status_code, 400)
        self.assertIn("Kill switch", refused.json()["detail"])
        self.assertIn("or after you reset the kill switch", refused.json()["detail"])
        replay = {key: first[key] for key in ("symbol", "side", "quantity", "client_order_id")}
        self.assertEqual(
            self.client.post("/api/orders", json=replay, headers=HEADERS).json(), first
        )
        sell = {**order, "symbol": "AAPL", "side": "sell", "client_order_id": str(uuid4())}
        self.assertEqual(
            self.client.post("/api/orders", json=sell, headers=HEADERS).status_code, 200
        )
        self.set_price("AAPL", "250.00")  # equity 102,225.52: recovered
        self.assertFalse(self.client.get("/api/state").json()["risk"]["buys_halted"])
        self.buy("MSFT", 1)

    def test_kill_switch_stays_on_when_flat_until_reset(self):
        self.buy("AAPL", 439)  # 99,881.28
        self.set_price("AAPL", "190.00")  # equity 83,528.72: halted
        state = self.set_price("AAPL", "170.00").json()  # the hard stop sells all 439
        self.assertEqual(state["holdings"], [])
        self.assertTrue(state["risk"]["buys_halted"])
        order = {"symbol": "MSFT", "side": "buy", "quantity": 1, "client_order_id": str(uuid4())}
        self.assertEqual(
            self.client.post("/api/orders", json=order, headers=HEADERS).status_code, 400
        )
        response = self.client.post("/api/kill-switch/reset", headers=HEADERS)
        self.assertEqual(response.status_code, 200, response.text)
        risk = response.json()["risk"]
        self.assertEqual((risk["equity_peak"], risk["buys_halted"]), ("74748.72", False))
        self.assertEqual(
            self.client.post("/api/orders", json=order, headers=HEADERS).status_code, 200
        )

    def test_kill_switch_reset_is_guarded_and_only_works_while_halted(self):
        self.buy("AAPL", 100)
        not_halted = self.client.post("/api/kill-switch/reset", headers=HEADERS)
        self.assertEqual(not_halted.status_code, 400)
        self.assertIn("not on", not_halted.json()["detail"])
        self.assertEqual(self.client.post("/api/kill-switch/reset").status_code, 403)
        foreign = {**HEADERS, "Host": "evil.example:8765"}
        self.assertEqual(
            self.client.post("/api/kill-switch/reset", headers=foreign).status_code, 400
        )
        Account.objects.filter(pk=1).update(equity_peak=Decimal("120000.00"))
        sell = {"symbol": "AAPL", "side": "sell", "quantity": 100, "client_order_id": str(uuid4())}
        self.client.post("/api/orders", json=sell, headers=HEADERS)  # flat: still halted
        self.assertTrue(self.client.get("/api/state").json()["risk"]["buys_halted"])
        self.assertEqual(Account.objects.get(pk=1).equity_peak, Decimal("120000.00"))

    def test_stops_round_down_so_cent_positions_are_not_at_their_stop(self):
        for symbol, price, stop in [("AAPL", "0.01", "0.00"), ("MSFT", "0.02", "0.01")]:
            self.set_price(symbol, price)
            self.buy(symbol, 5)
            self.buy(symbol, 5)  # an add re-runs the stop rules at the same price
            holding = self.holding(symbol)
            self.assertEqual((holding["quantity"], holding["stop_price"]), (10, stop))
        self.assertFalse(Order.objects.filter(side="sell").exists())

    def ladder_buys(self):
        return [
            (o.quantity, str(o.price))
            for o in Order.objects.filter(trigger="ladder").order_by("id")
        ]

    def test_ladder_buys_10_15_15_at_7_14_21_percent_below_entry(self):
        self.buy("AAPL", 10)  # entry 227.52
        for price, shares in [
            ("211.60", 10),
            ("211.59", 20),
            ("195.67", 20),
            ("195.66", 35),
            ("179.74", 50),
        ]:
            self.set_price("AAPL", price)
            self.assertEqual(Holding.objects.get().quantity, shares, price)
        self.assertEqual(self.ladder_buys(), [(10, "211.59"), (15, "195.66"), (15, "179.74")])
        self.set_price("AAPL", "175.00")
        self.assertEqual(len(self.ladder_buys()), 3)  # each level once per position

    def test_ladder_tiers_follow_volatility(self):
        self.buy("NVDA", 10)  # medium: -10%
        self.buy("TSLA", 10)  # high: -15%
        self.set_price("NVDA", "109.26")  # 121.40 x 0.90
        self.set_price("TSLA", "220.00")  # -14.7%: not yet
        self.set_price("TSLA", "219.31")  # 258.02 x 0.85 = 219.317
        self.assertEqual(
            sorted((o.symbol, o.quantity) for o in Order.objects.filter(trigger="ladder")),
            [("NVDA", 10), ("TSLA", 10)],
        )

    def test_gap_through_all_ladder_levels_buys_them_in_order(self):
        self.buy("AAPL", 10)
        state = self.set_price("AAPL", "179.74").json()
        self.assertEqual(state["holdings"][0]["quantity"], 50)
        self.assertEqual(self.ladder_buys(), [(10, "179.74"), (15, "179.74"), (15, "179.74")])
        self.assertEqual(state["holdings"][0]["stop_type"], "hard")

    def test_ladder_buy_shrinks_to_the_14k_cost_cap(self):
        self.buy("NVDA", 100)  # 12,140.00
        self.set_price("NVDA", "109.26")  # L1: 10 shares, cost 13,232.60
        self.set_price("NVDA", "97.12")  # L2: room 767.40 / 97.12 = 7 shares
        self.assertEqual(self.ladder_buys(), [(10, "109.26"), (7, "97.12")])

    def test_stop_wins_over_ladder_on_a_gap_down(self):
        self.buy("TSLA", 10)  # hard stop 193.51; L1 at 219.31
        self.set_price("TSLA", "190.00")
        self.assertFalse(Holding.objects.exists())
        self.assertEqual(self.ladder_buys(), [])

    def test_ladder_waits_while_price_is_above_average_cost(self):
        self.client.get("/api/state")  # seed quotes
        Holding.objects.create(
            symbol="AAPL",
            quantity=200,
            average_cost=Decimal("60.00"),
            entry_price=Decimal("100.00"),
        )
        self.set_price("AAPL", "85.00")  # -15% from entry but +41.7% on the average
        self.assertEqual(self.ladder_buys(), [])
        self.assertEqual(Order.objects.filter(trigger="profit_take").count(), 2)

    def test_ladders_pause_while_kill_switch_is_on(self):
        self.buy("AAPL", 10)
        Account.objects.filter(pk=1).update(equity_peak=Decimal("200000.00"))
        self.set_price("AAPL", "211.59")
        self.assertEqual(self.ladder_buys(), [])
        self.client.post("/api/kill-switch/reset", headers=HEADERS)
        self.set_price("AAPL", "211.59")
        self.assertEqual(self.ladder_buys(), [(10, "211.59")])

    def test_ladders_wait_for_cash_and_never_fire_inside_a_buy(self):
        self.buy("AAPL", 10)
        Account.objects.filter(pk=1).update(cash=Decimal("100.00"), equity_peak=Decimal("2300.00"))
        self.set_price("AAPL", "211.59")  # L1 due, but no cash for even one share
        self.assertEqual(self.ladder_buys(), [])
        Account.objects.filter(pk=1).update(cash=Decimal("5000.00"))
        self.buy("AAPL", 1)  # a manual buy does not run the ladder
        self.assertEqual((Holding.objects.get().quantity, self.ladder_buys()), (11, []))
        self.set_price("AAPL", "211.59")
        self.assertEqual(self.ladder_buys(), [(10, "211.59")])

    def test_ladder_levels_and_entry_reset_after_the_position_closes(self):
        self.buy("AAPL", 10)
        self.set_price("AAPL", "211.59")  # L1
        self.set_price("AAPL", "150.00")  # hard stop closes all 20
        self.assertFalse(Holding.objects.exists())
        self.buy("AAPL", 10)  # new entry 150.00
        self.set_price("AAPL", "139.50")  # 150 x 0.93
        self.assertEqual(self.ladder_buys()[-1], (10, "139.50"))

    def reentry_limit(self, symbol="AAPL"):
        state = self.client.get("/api/state").json()
        return next(q["reentry_limit"] for q in state["quotes"] if q["symbol"] == symbol)

    def stop_out_and_place_reentry(self):
        self.buy("AAPL", 10)
        self.set_price("AAPL", "150.00")  # hard stop sells all 10 (change 1)
        self.set_price("AAPL", "220.00")  # above the 10-change EMA: limit at 5% below
        self.assertEqual(self.reentry_limit(), "209.00")

    def test_reentry_buys_10_at_the_limit_after_a_stop_out(self):
        self.stop_out_and_place_reentry()
        self.set_price("AAPL", "210.00")  # above the limit: still pending
        self.assertFalse(Holding.objects.exists())
        state = self.set_price("AAPL", "209.00").json()
        order = state["orders"][0]
        self.assertEqual(
            (order["trigger"], order["side"], order["quantity"], order["price"]),
            ("reentry", "buy", 10, "209.00"),
        )
        self.assertEqual(Holding.objects.get().entry_price, Decimal("209.00"))
        self.assertIsNone(self.reentry_limit())

    def test_reentry_waits_until_price_is_above_its_ema(self):
        self.buy("AAPL", 10)
        self.set_price("AAPL", "150.00")  # stop-out; EMA 213.43
        self.set_price("AAPL", "160.00")  # EMA 203.71: below it, nothing placed
        self.assertIsNone(self.reentry_limit())
        self.set_price("AAPL", "230.00")  # EMA 208.49
        self.assertEqual(self.reentry_limit(), "218.50")

    def test_reentry_waits_one_price_change_after_the_stop_out(self):
        self.buy("AAPL", 10)
        Quote.objects.filter(symbol="AAPL").update(ema=Decimal("100.0000"))
        self.set_price("AAPL", "150.00")  # stop-out; price is above the EMA already
        self.assertIsNone(self.reentry_limit())
        self.set_price("AAPL", "150.00")
        self.assertEqual(self.reentry_limit(), "142.50")

    def test_unfilled_reentry_expires_after_5_changes_and_reprices(self):
        self.stop_out_and_place_reentry()  # placed on change 2
        for _ in range(5):  # changes 3-7
            self.set_price("AAPL", "220.00")
        self.assertEqual(self.reentry_limit(), "209.00")
        self.set_price("AAPL", "230.00")  # change 8: expired, re-priced
        self.assertEqual(self.reentry_limit(), "218.50")

    def test_kill_switch_cancels_pending_reentry_until_reset(self):
        self.stop_out_and_place_reentry()
        Account.objects.filter(pk=1).update(equity_peak=Decimal("200000.00"))
        self.set_price("AAPL", "215.00")
        self.assertIsNone(self.reentry_limit())
        self.client.post("/api/kill-switch/reset", headers=HEADERS)
        self.set_price("AAPL", "220.00")
        self.assertEqual(self.reentry_limit(), "209.00")

    def test_manual_buy_cancels_pending_reentry(self):
        self.stop_out_and_place_reentry()
        self.buy("AAPL", 1)
        self.assertIsNone(self.reentry_limit())
        self.set_price("AAPL", "209.00")
        self.assertEqual(Holding.objects.get().quantity, 1)
        self.assertFalse(Order.objects.filter(trigger="reentry").exists())

    def test_manual_sell_is_not_a_stop_out(self):
        self.buy("AAPL", 10)
        sell = {"symbol": "AAPL", "side": "sell", "quantity": 10, "client_order_id": str(uuid4())}
        self.client.post("/api/orders", json=sell, headers=HEADERS)
        self.set_price("AAPL", "230.00")
        self.set_price("AAPL", "240.00")
        self.assertIsNone(self.reentry_limit())

    def test_reentry_waits_for_cash(self):
        self.stop_out_and_place_reentry()
        Account.objects.filter(pk=1).update(cash=Decimal("100.00"), equity_peak=Decimal("100.00"))
        self.set_price("AAPL", "209.00")  # no cash for a share: stays pending
        self.assertEqual(self.reentry_limit(), "209.00")
        Account.objects.filter(pk=1).update(cash=Decimal("5000.00"))
        self.set_price("AAPL", "209.00")
        self.assertEqual(Holding.objects.get().quantity, 10)

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

    def test_cash_cap_refuses_sales_and_rolls_back_price_moves(self):
        self.buy()
        Holding.objects.update(ladder_level=3)  # ladder used up, so the stop is reachable
        self.set_price("AAPL", "172.00")  # just above the 170.64 hard stop
        Account.objects.filter(pk=1).update(cash=MAX_CASH - Decimal("100.00"))
        before = get_state()
        sell = {"symbol": "AAPL", "side": "sell", "quantity": 1, "client_order_id": str(uuid4())}
        refused = [
            self.client.post("/api/orders", json=sell, headers=HEADERS),
            self.set_price("AAPL", "170.00"),  # hard stop would sell 2 x 170.00
        ]
        with patch("trading.services.random.randint", return_value=-300):
            refused.append(self.client.post("/api/tick", headers=HEADERS))
        for response in refused:
            self.assertEqual(response.status_code, 400, response.text)
            self.assertIn("cash above", response.json()["detail"])
        self.assertEqual(get_state(), before)

    def test_tick_moves_small_prices_by_at_least_one_cent(self):
        self.set_price("NVDA", "0.10")
        for bps, expected in [(-300, "0.09"), (300, "0.10"), (1, "0.11"), (0, "0.11")]:
            with patch("trading.services.random.randint", return_value=bps):
                state = self.client.post("/api/tick", headers=HEADERS).json()
            price = next(item["price"] for item in state["quotes"] if item["symbol"] == "NVDA")
            self.assertEqual(price, expected, bps)

    def test_tick_applies_stops_and_never_drops_price_below_one_cent(self):
        self.buy()
        Holding.objects.update(ladder_level=3)  # ladder used up, so the stop is reachable
        self.set_price("AAPL", "172.00")
        self.set_price("MSFT", "0.01")
        with patch("trading.services.random.randint", return_value=-300):
            state = self.client.post("/api/tick", headers=HEADERS).json()
        prices = {item["symbol"]: item["price"] for item in state["quotes"]}
        self.assertEqual(prices["AAPL"], "166.84")
        self.assertEqual(prices["MSFT"], "0.01")
        self.assertEqual(state["holdings"], [])
        self.assertEqual(state["orders"][0]["trigger"], "hard_stop")
