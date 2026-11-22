"""Synthetic exit-order state-machine tests: never imports/connects IB."""
from datetime import timedelta
from decimal import Decimal
import unittest
from unittest.mock import patch

from core import ValidationError
from demo_broker import DemoBroker, _new_york_date


class DemoExitTests(unittest.TestCase):
    def setUp(self):
        self.broker = DemoBroker()
        self.broker.connect()
        preview = self.broker.preview(dict(symbol="SPY", expiry=self.broker.expirations("SPY")[0],
                                           right="C", moneyness="OTM", depth=4, percent=50))
        self.broker.buy(preview["token"])
        self.position = self.broker.positions()[0]
        self.conid, self.quantity = self.position["conid"], self.position["quantity"]
        self.cash = self.broker._cash
        self.bid = Decimal("1.00")
        self.broker._quote = lambda *args: (Decimal("600.4"), self.bid, self.bid + Decimal(".02"))

    def close(self, kind, percent=100, **fields):
        return self.broker.close(self.conid, percent, {"type": kind, "tif": "GTC", **fields})

    def last(self):
        return self.broker.orders()[-1]

    def move(self, value):
        self.bid = Decimal(value)
        self.broker.pump()
        return self.last()

    def test_add_and_close_keep_then_cancel_original_trailing_stop(self):
        stop = self.close("TRAIL", trail_mode="amount", trail_value=.20)
        add = self.broker.preview_add(self.conid, self.quantity, {"method": "lots", "value": 1, "order_mode": "LMT", "limit_price": 1.02})
        result = self.broker.buy(add["token"])
        self.assertEqual(result["filled"], 1)
        row = self.broker.positions()[0]
        self.assertEqual((row["quantity"], row["protection_id"]), (self.quantity + 1, stop["order_id"]))
        self.assertEqual(self.broker._pending_exits[stop["order_id"]]["order"]["remaining"], self.quantity)
        close = self.broker.close(self.conid, 100)
        self.assertEqual(close["filled"], self.quantity + 1)
        self.assertEqual(self.broker.positions(), [])
        self.assertEqual(self.broker._pending_exits, {})

    def test_limit_waits_without_spending_or_deleting_position_then_fills_at_bid(self):
        order = self.close("LMT", percent=50, limit_price=1.20)
        requested = self.quantity // 2
        self.assertEqual((order["filled"], order["remaining"]), (0, requested))
        self.assertEqual(self.broker.positions()[0]["quantity"], self.quantity)
        self.assertEqual(self.broker.snapshot()["cash"], float(self.cash))
        filled = self.move("1.25")
        self.assertEqual(filled["order_id"], order["order_id"])
        self.assertEqual((filled["filled"], filled["remaining"]), (requested, 0))
        self.assertEqual(filled["avg_fill_price"], 1.25)
        self.assertEqual(self.broker.positions()[0]["quantity"], self.quantity - requested)
        self.assertEqual(self.broker._cash, self.cash + Decimal("124") * requested)

    def test_stop_is_market_like_after_trigger_and_can_fill_below_stop(self):
        self.close("STP", stop_price=.90)
        self.assertEqual(self.move(".95")["filled"], 0)
        order = self.move(".70")
        self.assertEqual(order["filled"], self.quantity)
        self.assertEqual(order["avg_fill_price"], .70)
        self.assertTrue(order["triggered"])
        self.assertEqual(self.broker.positions(), [])

    def test_stop_limit_stays_triggered_after_gap_until_bid_recovers(self):
        self.close("STP LMT", stop_price=.90, limit_price=.85)
        order = self.move(".80")
        self.assertTrue(order["triggered"])
        self.assertEqual(order["filled"], 0)
        order = self.move("1.00")
        self.assertEqual(order["filled"], self.quantity)
        self.assertEqual(order["avg_fill_price"], 1.0)

    def test_percent_trailing_stop_follows_high_water_and_never_moves_down(self):
        order = self.close("TRAIL", trail_mode="percent", trail_value=10)
        self.assertAlmostEqual(order["stop_price"], .90)
        self.assertAlmostEqual(self.move("1.40")["stop_price"], 1.26)
        waiting = self.move("1.30")
        self.assertEqual(waiting["filled"], 0)
        self.assertAlmostEqual(waiting["stop_price"], 1.26)
        self.assertEqual(self.move("1.25")["filled"], self.quantity)

    def test_amount_trailing_stop_uses_absolute_option_quote_units(self):
        self.close("TRAIL", trail_mode="amount", trail_value=.20)
        self.assertAlmostEqual(self.move("1.50")["stop_price"], 1.30)
        self.assertEqual(self.move("1.40")["filled"], 0)
        self.assertEqual(self.move("1.29")["filled"], self.quantity)

    def test_trailing_limit_freezes_trigger_stop_minus_offset_not_gap_bid(self):
        self.close("TRAIL LIMIT", stop_price=.80, trail_mode="amount", trail_value=.10, limit_offset=.05)
        self.move("1.50")
        order = self.move("1.00")
        self.assertTrue(order["triggered"])
        self.assertAlmostEqual(order["stop_price"], 1.40)
        self.assertAlmostEqual(order["limit_price"], 1.35)
        self.assertEqual(order["filled"], 0)
        order = self.move("1.36")
        self.assertEqual(order["filled"], self.quantity)
        self.assertAlmostEqual(order["limit_price"], 1.35)

    def test_trailing_limit_respects_user_initial_stop_floor(self):
        order = self.close("TRAIL LIMIT", stop_price=.95, trail_mode="amount", trail_value=.10, limit_offset=.05)
        self.assertEqual(order["stop_price"], .95)
        self.assertEqual(self.move(".96")["filled"], 0)
        order = self.move(".94")
        self.assertTrue(order["triggered"])
        self.assertEqual(order["limit_price"], .90)

    def test_pending_exit_blocks_duplicate_and_immediate_close_until_cancel(self):
        self.close("LMT", limit_price=1.20)
        with self.assertRaises(ValidationError):
            self.broker.close(self.conid, 100)
        with self.assertRaises(ValidationError):
            self.close("STP", stop_price=.90)
        self.assertEqual(self.broker.cancel_own(), 1)
        self.assertIn("Cancelled", self.last()["status"])
        self.assertEqual(self.broker.positions()[0]["quantity"], self.quantity)
        self.assertEqual(self.broker.cancel_own(), 0)
        self.assertEqual(self.broker.close(self.conid, 100)["filled"], self.quantity)

    def test_snapshot_advances_orders_but_fixed_synthetic_prices_do_not_randomly_move(self):
        self.close("LMT", limit_price=1.20)
        for _ in range(3):
            snap = self.broker.snapshot()
            self.assertEqual(snap["positions"][0]["quantity"], self.quantity)
            self.assertEqual(snap["orders"][-1]["filled"], 0)
        self.bid = Decimal("1.21")
        self.assertEqual(self.broker.snapshot()["orders"][-1]["filled"], self.quantity)

    def test_returned_order_and_input_spec_cannot_mutate_pending_order(self):
        spec = dict(type="LMT", limit_price=1.20, tif="GTC")
        returned = self.broker.close(self.conid, 100, spec)
        spec["limit_price"], returned["limit_price"], returned["quantity"] = .01, .01, 99999
        self.broker.pump()
        self.assertEqual(self.last()["filled"], 0)
        self.assertEqual(self.last()["limit_price"], 1.20)
        self.assertEqual(self.last()["quantity"], self.quantity)

    def test_day_date_roll_expires_without_fill_while_gtc_survives(self):
        day = _new_york_date()
        self.broker.close(self.conid, 100, dict(type="LMT", limit_price=1.20, tif="DAY"))
        with patch("demo_broker._new_york_date", return_value=day + timedelta(days=1)):
            self.broker.pump()
            self.assertIn("Cancelled", self.last()["status"])
        self.close("LMT", limit_price=1.20)
        with patch("demo_broker._new_york_date", return_value=day + timedelta(days=1)):
            self.broker.pump()
        self.assertIn("Submitted", self.last()["status"])

    def test_disconnected_pump_does_not_trigger_and_reconnect_resumes(self):
        self.close("LMT", limit_price=1.20)
        self.broker.disconnect()
        self.bid = Decimal("1.30")
        self.broker.pump()
        self.assertEqual(self.broker._orders[-1]["filled"], 0)
        self.assertEqual(self.broker.snapshot()["orders"], [])
        self.broker.connect()
        self.assertEqual(self.last()["filled"], self.quantity)

    def test_invalid_parameters_never_create_order_or_alter_position(self):
        count = len(self.broker.orders())
        for spec in (dict(type="STP", stop_price=-1), dict(type="TRAIL", trail_mode="percent", trail_value=0),
                     dict(type="TRAIL LIMIT", trail_mode="amount", trail_value=.1, limit_offset=.05),
                     dict(type="LMT", limit_price=1.201),
                     dict(type="TRAIL", trail_mode="amount", trail_value=2)):
            with self.subTest(spec=spec), self.assertRaises(ValidationError):
                self.broker.close(self.conid, 100, {"tif": "GTC", **spec})
        self.assertEqual(len(self.broker.orders()), count)
        self.assertEqual(self.broker.positions()[0]["quantity"], self.quantity)
        self.assertEqual(self.broker._cash, self.cash)

    def test_every_order_includes_display_exit_fields(self):
        order = self.close("STP LMT", stop_price=.9, limit_price=.85)
        fields = {"order_type", "limit_price", "stop_price", "trail_mode", "trail_value", "limit_offset", "tif"}
        self.assertTrue(fields <= order.keys())
        self.assertTrue(fields <= self.broker.orders()[0].keys())

    def test_position_reduction_before_trigger_cancels_instead_of_selling_short(self):
        self.close("LMT", limit_price=1.20)
        self.broker._positions[self.conid]["quantity"] = 1
        order = self.move("1.30")
        self.assertIn("Cancelled", order["status"])
        self.assertEqual(order["filled"], 0)
        self.assertEqual(self.broker.positions()[0]["quantity"], 1)
        self.assertEqual(self.broker._cash, self.cash)


if __name__ == "__main__":
    unittest.main()
