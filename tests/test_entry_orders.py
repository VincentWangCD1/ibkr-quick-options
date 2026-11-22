"""V1.5 quantity, order pricing and synthetic amendment regression tests."""
from copy import deepcopy
from decimal import Decimal
import unittest
from unittest.mock import patch

from core import ValidationError, size_order
from demo_broker import DemoBroker, _new_york_date
from entry_orders import build_entry_plan


class QuantityRoundingTests(unittest.TestCase):
    def test_half_up_not_bankers_rounding(self):
        for target, expected in [(2.5, 3), (3.5, 4), (2.49, 2), (2.51, 3)]:
            result = size_order(1000, target, .1, 100, 0, 0, 0)
            self.assertEqual(result["quantity"], expected)
            self.assertEqual(result["raw_quantity"], target)
            self.assertEqual(result["rounding_mode"], "nearest")

    def test_selected_percent_can_be_exceeded_and_is_disclosed(self):
        result = size_order(1000, 50, .23, 100)
        self.assertEqual(result["quantity"], 22)
        self.assertEqual(result["estimated_premium"], 506)
        self.assertEqual(result["actual_percent"], 50.6)
        self.assertEqual(result["budget_difference"], 6)
        self.assertAlmostEqual(result["buffered_cost"], 553.3)
        self.assertAlmostEqual(result["buffered_percent"], 55.33)
        self.assertAlmostEqual(result["buffered_budget_difference"], 53.3)

    def test_full_cash_caps_rounded_up_quantity_including_fees(self):
        result = size_order(1000, 100, .23, 100)
        self.assertEqual(result["quantity"], 39)
        self.assertLessEqual(result["buffered_cost"], 1000)
        self.assertEqual(size_order(1000, 100, .23, 100, max_contracts=7)["quantity"], 7)

    def test_floor_keeps_original_buffered_percentage_limit(self):
        result = size_order(1000, 50, .23, 100, rounding_mode="floor")
        self.assertEqual(result["quantity"], 19)
        self.assertLessEqual(result["buffered_cost"], 500)
        with self.assertRaises(ValidationError):
            size_order(1000, 50, .23, 100, rounding_mode="ceil")

    def test_total_cash_invariant_across_small_premiums(self):
        for cash in [0, 1, 5, 10, 21, 100, 1000]:
            for percent in [1, 10, 50, 99, 100]:
                for price in [.01, .15, .23, 1, 10]:
                    with self.subTest(cash=cash, percent=percent, price=price):
                        result = size_order(cash, percent, price, 100)
                        self.assertLessEqual(result["buffered_cost"], cash + 1e-10)


class EntryPlanTests(unittest.TestCase):
    def plan(self, action="BUY", mode="MKT", **kwargs):
        return build_entry_plan(action, mode, kwargs.pop("quote", {"bid": .20, "ask": .23}),
                                kwargs.pop("increments", [(0, .01)]), **kwargs)

    def test_regular_market_and_extended_protected_market(self):
        plan = self.plan()
        self.assertEqual((plan["order_type"], plan["limit_price"], plan["sizing_price"]), ("MKT", None, .23))
        self.assertFalse(plan["outside_rth"])
        extended = self.plan(session_kind="extended")
        self.assertEqual((extended["order_type"], extended["limit_price"], extended["price_mode"]), ("LMT", .25, "MKT"))
        self.assertEqual(extended["reserve_slippage_pct"], 0)
        self.assertTrue(extended["outside_rth"])

    def test_midpoint_uses_conservative_tick_and_stays_fixed(self):
        buy, sell = self.plan(mode="MID"), self.plan("SELL", "MID")
        self.assertEqual((buy["limit_price"], sell["limit_price"]), (.21, .22))
        self.assertEqual(buy["midpoint"], .215)
        self.assertEqual(buy["price_mode"], "MID")
        self.assertEqual(buy["sizing_price"], .21)
        self.assertEqual(buy["reserve_slippage_pct"], 0)

    def test_midpoint_tier_transition_final_tick_valid(self):
        quote = {"bid": 2.99, "ask": 3.02}
        tiers = [(0, .01), (3, .05)]
        self.assertEqual(self.plan(mode="MID", quote=quote, increments=tiers)["limit_price"], 3)
        self.assertEqual(self.plan("SELL", "MID", quote=quote, increments=tiers)["limit_price"], 3.05)

    def test_explicit_limit_is_exact_not_silently_rounded(self):
        self.assertEqual(self.plan(mode="LMT", limit_price=.19)["limit_price"], .19)
        for value in [.191, 0, -1, None, float("nan")]:
            with self.subTest(value=value), self.assertRaises(ValidationError):
                self.plan(mode="LMT", limit_price=value)

    def test_invalid_or_crossed_midpoint_rejected(self):
        for quote in ({"bid": -.01, "ask": .2}, {"bid": .3, "ask": .2},
                      {"bid": float("nan"), "ask": .2}, {"ask": .2}):
            with self.assertRaises(ValidationError):
                self.plan(mode="MID", quote=quote)
        with self.assertRaises(ValidationError):
            self.plan(session_kind="closed")


class DemoEntryAmendmentTests(unittest.TestCase):
    def setUp(self):
        self.broker = DemoBroker()
        self.broker.connect()
        self.settings = {"symbol": "SPY", "expiry": self.broker.expirations("SPY")[0],
                         "right": "C", "moneyness": "OTM", "depth": 4, "percent": 50,
                         "order_mode": "LMT", "limit_price": .19}

    def pending(self, **settings):
        preview = self.broker.preview({**self.settings, **settings})
        return self.broker.buy(preview["token"])

    def test_limit_below_ask_waits_and_reserves_cash_then_fills(self):
        order = self.pending()
        self.assertEqual((order["filled"], order["remaining"]), (0, 26))
        snap = self.broker.snapshot()
        self.assertEqual(snap["cash"], 480)
        self.assertEqual(snap["financial"]["usd_cash"], 1000)
        self.assertEqual(snap["positions"], [])
        self.broker._quote = lambda *args: (Decimal("600.4"), Decimal(".16"), Decimal(".18"))
        self.broker.pump()
        filled = self.broker.orders()[0]
        self.assertEqual((filled["filled"], filled["remaining"]), (26, 0))
        self.assertEqual(filled["avg_fill_price"], .18)
        self.assertEqual(self.broker.snapshot()["cash"], 506)
        self.assertEqual(self.broker.positions()[0]["quantity"], 26)

    def test_marketable_limit_fills_at_ask_not_higher_limit(self):
        order = self.pending(limit_price=.25)
        self.assertEqual(order["remaining"], 0)
        self.assertEqual(order["avg_fill_price"], .21)
        self.assertEqual(order["limit_price"], .25)

    def test_midpoint_is_fixed_limit_and_can_be_changed_to_market(self):
        order = self.pending(order_mode="MID", limit_price=None)
        self.assertEqual(order["limit_price"], .20)
        self.assertEqual(order["filled"], 0)
        self.broker._quote = lambda *args: (Decimal("600.4"), Decimal(".21"), Decimal(".23"))
        self.broker.pump()
        self.assertEqual(self.broker.orders()[0]["limit_price"], .20)
        preview = self.broker.preview_modify(order["order_id"], "MKT")
        amended = self.broker.modify_order(preview["token"])
        self.assertNotEqual(amended["order_id"], order["order_id"])
        self.assertEqual(amended["replaces_order_id"], order["order_id"])
        self.assertIn("Cancelled", self.broker.orders()[0]["status"])
        self.assertEqual(amended["quantity"], order["quantity"])
        self.assertEqual(amended["order_type"], "MKT")
        self.assertEqual(amended["remaining"], 0)
        self.assertEqual(amended["avg_fill_price"], .23)

    def test_cancel_releases_reservation_and_second_pending_entry_is_blocked(self):
        order = self.pending()
        preview = self.broker.preview(self.settings)
        with self.assertRaises(ValidationError):
            self.broker.buy(preview["token"])
        cancelled = self.broker.cancel_order(order["order_id"])
        self.assertIn("Cancelled", cancelled["status"])
        self.assertEqual(self.broker.snapshot()["cash"], 1000)
        with self.assertRaises(ValidationError):
            self.broker.preview_modify(order["order_id"], "MKT")

    def test_amend_limit_updates_reservation_without_quantity_increase(self):
        order = self.pending()
        preview = self.broker.preview_modify(order["order_id"], "LMT", .18)
        self.assertEqual(preview["old_limit_price"], .19)
        self.assertEqual(preview["new_limit_price"], .18)
        self.assertEqual(preview["remaining"], 26)
        amended = self.broker.modify_order(preview["token"])
        self.assertEqual(amended["order_id"], order["order_id"])
        self.assertEqual(amended["quantity"], 26)
        self.assertEqual(amended["filled"], 0)
        self.assertEqual(self.broker.snapshot()["cash"], 506)

    def test_amend_rejects_insufficient_total_cash_without_reducing_quantity(self):
        order = self.pending(limit_price=.05)
        self.assertEqual(order["quantity"], 100)
        with self.assertRaises(ValidationError):
            self.broker.preview_modify(order["order_id"], "MKT")
        self.assertEqual(self.broker.orders()[0]["remaining"], 100)

    def test_custom_fee_and_slippage_settings_survive_mid_limit_market_amendments(self):
        for minimum in (1, 100):
            with self.subTest(minimum=minimum):
                broker = DemoBroker()
                broker.connect()
                entry = broker.preview({**self.settings, "order_mode": "MID", "limit_price": None,
                                        "commission_per_contract": 3, "min_commission": minimum,
                                        "slippage_pct": 10})
                order = broker.buy(entry["token"])
                self.assertEqual(order["quantity"], 25)
                expected_fee = max(minimum, 25 * 3)
                for mode, limit, premium in (("LMT", .19, 475), ("MID", None, 500)):
                    preview = broker.preview_modify(order["order_id"], mode, limit)
                    self.assertEqual(preview["commission_per_contract"], 3)
                    self.assertEqual(preview["min_commission"], minimum)
                    self.assertEqual(preview["slippage_pct"], 10)
                    self.assertEqual(preview["reserve_slippage_pct"], 0)
                    self.assertEqual(preview["fee_reserve"], expected_fee)
                    self.assertEqual(preview["reserved_cash"], premium + expected_fee)
                    order = broker.modify_order(preview["token"])
                    self.assertEqual(order["remaining"], 25)
                preview = broker.preview_modify(order["order_id"], "MKT")
                self.assertEqual(preview["reserve_slippage_pct"], 10)
                self.assertEqual(preview["commission_per_contract"], 3)
                self.assertEqual(preview["min_commission"], minimum)
                self.assertEqual(preview["reserved_cash"], 577.5 + expected_fee)
                replaced = broker.modify_order(preview["token"])
                self.assertEqual(replaced["quantity"], 25)
                self.assertEqual(replaced["filled"], 25)
                self.assertEqual(broker.snapshot()["cash"], 1000 - 525 - expected_fee)

    def test_amend_token_immutable_single_use_expired_and_stale_after_fill(self):
        order = self.pending()
        preview = self.broker.preview_modify(order["order_id"], "LMT", .18)
        preview["limit_price"] = .99
        amended = self.broker.modify_order(preview["token"])
        self.assertEqual(amended["limit_price"], .18)
        with self.assertRaises(ValidationError):
            self.broker.modify_order(preview["token"])
        preview = self.broker.preview_modify(order["order_id"], "MKT")
        with patch("demo_broker.time.time", return_value=preview["created_at"] + 11):
            with self.assertRaises(ValidationError):
                self.broker.modify_order(preview["token"])
        preview = self.broker.preview_modify(order["order_id"], "MKT")
        self.broker._quote = lambda *args: (Decimal("600.4"), Decimal(".15"), Decimal(".17"))
        with self.assertRaises(ValidationError):
            self.broker.modify_order(preview["token"])
        self.assertEqual(self.broker.orders()[0]["remaining"], 0)
        self.assertEqual(len(self.broker.orders()), 1)

    def test_conditional_stop_requires_cancel_and_recreate_like_ib_adapter(self):
        self.pending(order_mode="MKT")
        position = self.broker.positions()[0]
        stop = self.broker.close(position["conid"], 100, {"type": "STP", "stop_price": .10})
        self.assertEqual(stop["filled"], 0)
        with self.assertRaises(ValidationError):
            self.broker.preview_modify(stop["order_id"], "MKT")
        self.assertEqual(self.broker.orders()[-1]["stop_price"], .10)
        cancelled = self.broker.cancel_order(stop["order_id"])
        self.assertIn("Cancelled", cancelled["status"])
        closed = self.broker.close(position["conid"], 100)
        self.assertEqual(closed["remaining"], 0)
        self.assertEqual(self.broker.positions(), [])

    def test_pending_sell_can_be_changed_to_midpoint_and_still_wait(self):
        self.pending(order_mode="MKT")
        position = self.broker.positions()[0]
        order = self.broker.close(position["conid"], 50, {"type": "LMT", "limit_price": .30})
        preview = self.broker.preview_modify(order["order_id"], "MID")
        amended = self.broker.modify_order(preview["token"])
        self.assertEqual(amended["price_mode"], "MID")
        self.assertEqual(amended["limit_price"], .20)
        self.assertEqual(amended["filled"], 0)
        self.assertEqual(amended["quantity"], order["quantity"])
        self.assertEqual(self.broker.cancel_own(), 1)

    def test_partial_sell_replacement_only_uses_unfilled_remainder(self):
        self.pending(order_mode="MKT")
        position = self.broker.positions()[0]
        order = self.broker.close(position["conid"], 100, {"type": "LMT", "limit_price": .30})
        state = self.broker._pending_exits[order["order_id"]]
        # Inject a broker-like partial state; the demo itself fills atomically.
        state["order"].update(filled=3, remaining=order["quantity"] - 3, avg_fill_price=.30)
        self.broker._positions[position["conid"]]["quantity"] -= 3
        preview = self.broker.preview_modify(order["order_id"], "MKT")
        amended = self.broker.modify_order(preview["token"])
        self.assertEqual(amended["quantity"], order["quantity"] - 3)
        self.assertEqual(amended["filled"], order["quantity"] - 3)
        self.assertEqual(self.broker.positions(), [])


if __name__ == "__main__":
    unittest.main()
