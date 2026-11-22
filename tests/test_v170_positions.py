"""Pure holdings valuations and synthetic close/history regression coverage."""
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from core import ValidationError
from position_analytics import enrich_position


class PositionAnalyticsTests(unittest.TestCase):
    def setUp(self):
        self.position = dict(conid=42, quantity=3, multiplier=100, avg_cost=201)
        self.quote = dict(bid=2.4, ask=2.6, last=2.45, market_data_type=1,
                          bid_age=1, ask_age=2, last_age=1)

    def test_long_average_cost_and_percent_include_entry_cost(self):
        result = enrich_position(self.position, self.quote)
        self.assertEqual(result["avg_option_price"], 2.01)
        self.assertEqual(result["entry_cost"], 603)
        self.assertEqual(result["market_value"], 750)
        self.assertEqual(result["unrealized_pnl"], 147)
        self.assertAlmostEqual(result["unrealized_pnl_pct"], 147 / 603 * 100)
        self.assertTrue(result["price_fresh"])

    def test_signed_short_pnl_and_invalid_data(self):
        result = enrich_position({**self.position, "quantity": -3}, self.quote)
        self.assertEqual(result["market_value"], -750)
        self.assertEqual(result["unrealized_pnl"], -147)
        for bad in (float("nan"), float("inf"), None):
            result = enrich_position({**self.position, "avg_cost": bad}, self.quote)
            self.assertIsNone(result["unrealized_pnl"])
            self.assertIsNone(result["unrealized_pnl_pct"])

    def test_stale_book_does_not_become_live_and_portfolio_is_reference(self):
        quote = {**self.quote, "bid_age": 60, "ask_age": 60, "last_age": 60}
        result = enrich_position(self.position, quote, {"market_price": 2.7})
        self.assertEqual(result["current_price"], 2.7)
        self.assertFalse(result["price_fresh"])
        self.assertIn("组合估值", result["quote_status"])
        result = enrich_position(self.position, {**self.quote, "market_data_type": 3})
        self.assertFalse(result["price_fresh"])
        self.assertIn("延迟", result["quote_status"])
        result = enrich_position(self.position, {})
        self.assertIsNone(result["unrealized_pnl"])

    def test_stop_and_profit_orders_show_separate_quantity_projections_and_status(self):
        orders = [dict(conid=42, action="SELL", order_type="STP", stop_price=1.5, remaining=2, status="Submitted"),
                  dict(conid=42, action="SELL", order_type="LMT", limit_price=3, remaining=1, status="PendingCancel"),
                  dict(conid=42, action="SELL", order_type="STP", stop_price=1.2, remaining=1, status="PendingSubmit"),
                  dict(conid=42, action="SELL", order_type="STP", stop_price=1, remaining=1, status="Cancelled")]
        result = enrich_position(self.position, self.quote, orders=orders)
        self.assertEqual(len(result["active_exit_orders"]), 3)
        stop, profit, unconfirmed = result["active_exit_orders"]
        self.assertEqual(stop["projected_pnl"], -102)
        self.assertTrue(stop["working"])
        self.assertEqual(profit["projected_pnl"], 99)
        self.assertFalse(profit["working"])
        self.assertFalse(unconfirmed["working"])
        self.assertIn("撤单中", result["take_profit_summary"])
        self.assertNotIn("OCA", result["stop_loss_summary"])

    def test_loss_limit_is_visible_but_not_mislabeled_take_profit(self):
        order = dict(conid=42, action="SELL", order_type="LMT", limit_price=1.5, remaining=1, status="Submitted")
        result = enrich_position(self.position, self.quote, orders=[order])
        self.assertIn("非止盈", result["take_profit_summary"])
        self.assertIn("-51.00", result["take_profit_summary"])

    def test_trailing_summary_exposes_percent_distance_stop_and_offset(self):
        orders = [dict(conid=42, action="SELL", order_type="TRAIL", remaining=1, status="Submitted",
                       trail_mode="percent", trail_value=5, stop_price=2.3),
                  dict(conid=42, action="SELL", order_type="TRAIL LIMIT", remaining=1, status="PendingSubmit",
                       trail_mode="amount", trail_value=.25, stop_price=2.1, limit_offset=.05)]
        result = enrich_position(self.position, self.quote, orders=orders)
        summary = result["stop_loss_summary"]
        for expected in ("移动 5%", "当前触发 2.3", "移动价差 $0.25/份", "限价偏移 $0.05/份", "待确认"):
            self.assertIn(expected, summary)


class DemoPositionTests(unittest.TestCase):
    def setUp(self):
        from demo_broker import DemoBroker
        self.b = DemoBroker()
        self.b.connect()
        self.settings = dict(symbol="SPY", expiry=self.b.expirations("SPY")[0], right="C",
                             trading_class="SPY", depth=4, percent=50)

    def open_position(self, short=False):
        settings = {**self.settings, "open_action": "SELL", "seller_quantity": 1, "percent": 100} if short else self.settings
        preview = self.b.preview(settings)
        self.b.buy(preview["token"])
        return self.b.positions()[0]

    def test_refresh_positions_has_cost_quote_pnl_and_no_fictional_stops(self):
        position = self.open_position()
        current = self.b.refresh_positions()["positions"][0]
        self.assertEqual(current["conid"], position["conid"])
        self.assertAlmostEqual(current["avg_option_price"], .22)
        self.assertEqual(current["current_price"], .2)
        self.assertIn("合成", current["price_source"])
        self.assertFalse(current["active_exit_orders"])

    def test_frozen_midpoint_close_keeps_contract_price_and_is_one_use(self):
        position = self.open_position()
        preview = self.b.preview_close(position["conid"], 50, "MID")
        self.assertEqual(preview["action"], "SELL")
        self.assertEqual(preview["limit_price"], .2)
        self.assertEqual(preview["position_quantity"], position["quantity"])
        order = self.b.close_confirm(preview["token"])
        self.assertEqual(order["limit_price"], .2)
        self.assertEqual(order["price_mode"], "MID")
        self.assertTrue(self.b.positions()[0]["close_pending"])
        with self.assertRaises(ValidationError):
            self.b.close_confirm(preview["token"])
        with self.assertRaises(ValidationError):
            self.b.preview_close(position["conid"])

    def test_changed_position_and_expired_preview_never_close(self):
        position = self.open_position()
        preview = self.b.preview_close(position["conid"])
        self.b._positions[position["conid"]]["quantity"] -= 1
        with self.assertRaises(ValidationError):
            self.b.close_confirm(preview["token"])
        preview = self.b.preview_close(position["conid"])
        self.b._close_previews[preview["token"]][0]["created_at"] -= 11
        with self.assertRaises(ValidationError):
            self.b.close_confirm(preview["token"])

    def test_short_midpoint_close_waits_then_buys_back_without_creating_long(self):
        position = self.open_position(short=True)
        preview = self.b.preview_close(position["conid"], order_mode="LMT")
        self.assertTrue(preview["limit_seeded"])
        self.assertEqual(preview["action"], "BUY")
        self.assertEqual(preview["estimated_pnl"], -3)
        order = self.b.close_confirm(preview["token"])
        self.assertEqual(order["remaining"], 1)
        self.assertTrue(self.b.positions()[0]["close_pending"])
        with self.assertRaises(ValidationError):
            self.b.preview_close(position["conid"])
        self.b._quote = lambda *args: (Decimal("600.4"), Decimal(".17"), Decimal(".19"))
        self.b.pump()
        self.assertEqual(self.b.positions(), [])
        self.assertEqual(self.b.orders()[-1]["action"], "BUY")
        self.assertEqual(self.b.orders()[-1]["remaining"], 0)

    def test_cancel_short_cover_releases_reservation_and_preserves_short(self):
        position = self.open_position(short=True)
        preview = self.b.preview_close(position["conid"], order_mode="LMT", limit_price=.1)
        order = self.b.close_confirm(preview["token"])
        self.b.cancel_order(order["order_id"])
        self.assertEqual(self.b.positions()[0]["quantity"], -1)
        self.assertFalse(self.b.positions()[0]["close_pending"])

    def test_long_and_short_history_net_pnl_includes_both_commissions_once(self):
        for short in (False, True):
            with self.subTest(short=short):
                self.setUp()
                position = self.open_position(short)
                quantity = abs(position["quantity"])
                preview = self.b.preview_close(position["conid"], order_mode="MKT")
                self.b.close_confirm(preview["token"])
                for period in ("today", "7d", "30d"):
                    history = self.b.history(period)
                    self.assertEqual(len(history["rows"]), 2)
                    totals = history["totals_by_currency"]["USD"]
                    self.assertEqual(totals["realized_pnl"], -4 * quantity)
                    self.assertEqual(totals["commission"], 2 * quantity)
                    self.assertEqual(totals["net_cash"], -4 * quantity)
                    self.assertEqual({r["source"] for r in history["rows"]}, {"DEMO"})

    def test_demo_history_survives_new_instance_without_duplicating_execution_ids(self):
        from demo_broker import DemoBroker
        with TemporaryDirectory() as folder:
            path = str(Path(folder) / "history.sqlite3")
            self.b = DemoBroker(history_path=path)
            self.b.connect()
            self.open_position()
            other = DemoBroker(history_path=path)
            other.connect()
            self.assertEqual(len(other.history()["rows"]), 1)
            other.buy(other.preview(self.settings)["token"])
            self.assertEqual(len(other.history()["rows"]), 2)
            other._history.close()
            self.b._history.close()

    def test_history_write_failure_does_not_turn_confirmed_fill_into_failed_order(self):
        with patch.object(self.b._history, "record_execution", side_effect=OSError("disk unavailable")):
            position = self.open_position()
            self.assertGreater(position["quantity"], 0)
            self.assertEqual(self.b.orders()[0]["remaining"], 0)
        self.assertEqual(len(self.b.history()["rows"]), 1)
        self.assertFalse(self.b._history_pending)


if __name__ == "__main__":
    unittest.main()
