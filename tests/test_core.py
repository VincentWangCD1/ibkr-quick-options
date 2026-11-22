import math
from datetime import date, datetime, timezone
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import ValidationError, close_quantity, select_strike, size_order
from demo_broker import DemoBroker, _new_york_date
from zoneinfo import ZoneInfoNotFoundError


class StrikeTests(unittest.TestCase):
    def test_atm_and_tie_lower(self):
        self.assertEqual(select_strike([100, 101, 103], 101.6, "C", "ATM", 1), 101)
        self.assertEqual(select_strike([100, 101, 103], 102, "P", "ATM", 1), 101)

    def test_call_and_put_strict_direction(self):
        strikes = [97, 98, 99, 100, 101, 102, 103]
        for right, money, expected in [("C", "OTM", 102), ("C", "ITM", 98),
                                       ("P", "OTM", 98), ("P", "ITM", 102)]:
            with self.subTest(right=right, money=money):
                self.assertEqual(select_strike(strikes, 100, right, money, 2), expected)

    def test_irregular_spacing_counts_listed_strikes(self):
        self.assertEqual(select_strike([90, 95, 100, 105, 110], 99, "C", "OTM", 2), 105)
        self.assertEqual(select_strike([100, 100, 101, 104], 99, "C", "OTM", 3), 104)

    def test_invalid_and_insufficient_chain(self):
        cases = [([], 100, "C", "ATM", 1), ([99, 100], 100, "C", "OTM", 1),
                 ([100], math.nan, "C", "ATM", 1), ([100], 100, "X", "ATM", 1),
                 ([100], 100, "C", "ATM", 0), ([0, 100], 100, "C", "ATM", 1),
                 ([100], 100, "C", "BOGUS", 1)]
        for args in cases:
            with self.subTest(args=args), self.assertRaises(ValidationError):
                select_strike(*args)


class SizingTests(unittest.TestCase):
    def test_user_examples_without_reserves(self):
        self.assertEqual(size_order(1000, 50, .23, 100, 0, 0, 0)["quantity"], 22)
        self.assertEqual(size_order(1000, 50, .15, 100, 0, 0, 0)["quantity"], 33)

    def test_cash_percent_reserves_and_quantity_cap(self):
        result = size_order(1000, 50, .23, 100, rounding_mode="floor")
        self.assertEqual(result["quantity"], 19)
        self.assertEqual(result["budget"], 500)
        self.assertAlmostEqual(result["estimated_premium"], 437)
        self.assertAlmostEqual(result["buffered_cost"], 477.85)
        self.assertEqual(size_order(1000, 25, .15, 100, max_contracts=3)["quantity"], 3)

    def test_exact_decimal_boundary(self):
        self.assertEqual(size_order(69, 100, .23, 100, 0, 0, 0)["quantity"], 3)
        self.assertEqual(size_order("68.99", 100, ".23", 100, 0, 0, 0)["quantity"], 2)

    def test_minimum_commission_can_be_binding(self):
        result = size_order(50, 100, .10, 100, 0, .1, 5)
        self.assertEqual(result["quantity"], 4)
        self.assertEqual(result["fee_reserve"], 5)
        self.assertEqual(result["buffered_cost"], 45)

    def test_maximal_size_under_constraints(self):
        for cash in [0, 1, 5, 10, 50, 73.12, 500, 1000]:
            for price in [.01, .15, .23, 10]:
                with self.subTest(cash=cash, price=price):
                    result = size_order(cash, 50, price, 100, 5, .65, 1, rounding_mode="floor")
                    n = result["quantity"]
                    self.assertLessEqual(result["buffered_cost"], cash * .5 + 1e-9)
                    if n < 100:
                        next_cost = (n + 1) * price * 100 * 1.05 + max(1, .65 * (n + 1))
                        self.assertGreater(next_cost, cash * .5)

    def test_insufficient_cash_has_zero_fees(self):
        result = size_order(5, 50, .15, 100)
        self.assertEqual((result["quantity"], result["buffered_cost"], result["fee_reserve"]), (0, 0, 0))

    def test_reject_invalid_numeric_inputs(self):
        for key, value in [("cash", math.nan), ("ask", math.inf), ("ask", -.1),
                           ("cash", -1), ("percent", 0), ("percent", 101),
                           ("percent", True), ("multiplier", 0), ("multiplier", 1.5),
                           ("slippage_pct", -1), ("commission_per_contract", -1),
                           ("min_commission", -1), ("max_contracts", 0)]:
            args = {"cash": 1000, "percent": 50, "ask": .23, "multiplier": 100, key: value}
            with self.subTest(key=key, value=value), self.assertRaises(ValidationError):
                size_order(**args)

    def test_close_floor_minimum_and_all(self):
        self.assertEqual(close_quantity(19, 50), 9)
        self.assertEqual(close_quantity(1, 10), 1)
        self.assertEqual(close_quantity(19, 100), 19)
        for position, percent in [(-1, 50), (0, 50), (1.5, 50), (10, 0), (10, 101)]:
            with self.assertRaises(ValidationError):
                close_quantity(position, percent)


class DemoTests(unittest.TestCase):
    def setUp(self):
        self.broker = DemoBroker()
        self.broker.connect()
        self.settings = {"symbol": "SPY", "expiry": self.broker.expirations("SPY")[0],
                         "right": "C", "moneyness": "OTM", "depth": 4, "percent": 50,
                         "slippage_pct": 5, "commission_per_contract": 1,
                         "min_commission": 1, "max_contracts": 100}

    def test_expiration_uses_new_york_day_near_utc_midnight(self):
        instant = datetime(2026, 11, 12, 0, 30, tzinfo=timezone.utc)
        ny_day = _new_york_date(instant)
        self.assertEqual(ny_day, date(2026, 11, 11))
        with patch("demo_broker._new_york_date", return_value=ny_day):
            self.assertEqual(self.broker.expirations("SPY")[0], "20261111")

    def test_demo_new_york_date_without_timezone_database(self):
        cases = [
            (datetime(2026, 11, 12, 0, 30, tzinfo=timezone.utc), date(2026, 11, 11)),
            (datetime(2026, 1, 5, 4, 30, tzinfo=timezone.utc), date(2026, 1, 4)),
            (datetime(2026, 7, 6, 4, 30, tzinfo=timezone.utc), date(2026, 7, 6)),
            (datetime(2026, 3, 8, 4, 30, tzinfo=timezone.utc), date(2026, 3, 7)),
            (datetime(2026, 3, 9, 4, 30, tzinfo=timezone.utc), date(2026, 3, 9)),
            (datetime(2026, 11, 1, 4, 30, tzinfo=timezone.utc), date(2026, 11, 1)),
            (datetime(2026, 11, 2, 4, 30, tzinfo=timezone.utc), date(2026, 11, 1)),
        ]
        with patch("demo_broker.ZoneInfo", side_effect=ZoneInfoNotFoundError):
            for instant, expected in cases:
                with self.subTest(instant=instant):
                    self.assertEqual(_new_york_date(instant), expected)

    def test_preview_does_not_spend_and_cannot_be_tampered(self):
        preview = self.broker.preview(self.settings)
        expected = preview["quantity"]
        preview["quantity"] = 9999
        self.settings["percent"] = 100
        self.assertEqual(self.broker.snapshot()["cash"], 1000)
        order = self.broker.buy(preview["token"])
        self.assertEqual(order["quantity"], expected)
        self.assertLess(self.broker.snapshot()["cash"], 1000)

    def test_token_one_use_and_expiry(self):
        preview = self.broker.preview(self.settings)
        self.broker.buy(preview["token"])
        with self.assertRaises(ValidationError):
            self.broker.buy(preview["token"])
        preview = self.broker.preview(self.settings)
        with patch("demo_broker.time.time", return_value=preview["created_at"] + 11):
            with self.assertRaises(ValidationError):
                self.broker.buy(preview["token"])

    def test_partial_then_full_close_never_short(self):
        preview = self.broker.preview(self.settings)
        order = self.broker.buy(preview["token"])
        position = self.broker.positions()[0]
        partial = self.broker.close(position["conid"], 50)
        self.assertEqual(partial["quantity"], order["quantity"] // 2)
        final = self.broker.close_all()
        self.assertEqual(len(final), 1)
        self.assertEqual(self.broker.positions(), [])
        with self.assertRaises(ValidationError):
            self.broker.close(position["conid"], 100)

    def test_background_preview_preserves_clicked_token_but_fill_invalidates_all(self):
        old = self.broker.preview(self.settings)
        newer = self.broker.preview(self.settings)
        self.broker.buy(old["token"])
        with self.assertRaises(ValidationError):
            self.broker.buy(newer["token"])
        self.broker.disconnect()
        with self.assertRaises(ValidationError):
            self.broker.preview(self.settings)

    def test_balance_accounts_for_both_sides(self):
        preview = self.broker.preview(self.settings)
        quantity = preview["quantity"]
        self.broker.buy(preview["token"])
        self.broker.close_all()
        expected = 1000 - quantity * (preview["ask"] - preview["bid"]) * 100
        expected -= preview["fee_reserve"] + quantity
        self.assertAlmostEqual(self.broker.snapshot()["cash"], expected)

    def test_zero_quantity_not_submitted(self):
        self.settings["percent"] = .001
        preview = self.broker.preview(self.settings)
        self.assertEqual(preview["quantity"], 0)
        with self.assertRaises(ValidationError):
            self.broker.buy(preview["token"])
        self.assertEqual(self.broker.orders(), [])


if __name__ == "__main__":
    unittest.main()
