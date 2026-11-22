"""V1.6 budgets, paired contract previews and synthetic seller lifecycle."""
from decimal import Decimal
import unittest
from unittest.mock import patch

from core import ValidationError, opening_quantity, select_settings_strike, size_order
from demo_broker import DemoBroker
from entry_orders import build_entry_plan, resolve_entry_price


class V160CoreTests(unittest.TestCase):
    def test_fractional_percent_and_explicit_cash_amount(self):
        percent = size_order(100000, .1, .19, 100)
        self.assertEqual(percent["budget"], 100)
        self.assertEqual(percent["quantity"], 5)
        explicit = size_order(100000, "ignored", .19, 100, budget_mode="amount", cash_amount=99)
        self.assertEqual(explicit["quantity"], 4)
        self.assertLessEqual(explicit["buffered_cost"], 99)
        self.assertEqual(explicit["requested_budget"], 99)
        self.assertEqual(explicit["budget_mode"], "amount")

    def test_explicit_amount_caps_all_in_not_premium_and_not_account_cash(self):
        for cash in (20, 100, 100000):
            for amount in (1, 10, 49, 99, 500):
                result = size_order(cash, 100, .19, 100, budget_mode="amount", cash_amount=amount)
                self.assertLessEqual(result["buffered_cost"], min(cash, amount))
        for value in (None, 0, -1, True, "nan", "inf"):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                size_order(1000, 50, .2, 100, budget_mode="amount", cash_amount=value)

    def test_specific_strike_requires_exact_available_value_but_no_spot(self):
        settings = {"selection_mode": "strike", "strike": 625}
        self.assertEqual(select_settings_strike([600, 625, 630], None, "P", settings), 625)
        for wrong in (626, 0, "nan"):
            with self.assertRaises(ValidationError):
                select_settings_strike([600, 625], None, "P", {**settings, "strike": wrong})
        self.assertEqual(select_settings_strike(range(500, 800), 600.4, "C", {"depth": 25}), 625)

    def test_seller_quantity_is_explicit_positive_integer_not_received_premium(self):
        self.assertEqual(opening_quantity({"percent": 100}), 1)
        self.assertEqual(opening_quantity({"seller_quantity": 3, "max_contracts": 3}), 3)
        for value in (0, -1, 1.5, "nan", True, 10001):
            with self.assertRaises(ValidationError):
                opening_quantity({"seller_quantity": value})

    def test_per_side_limits_and_blank_limit_seeding(self):
        settings = {"order_mode": "LMT", "limit_price": 99, "limit_prices": {"C": "", "P": .23}}
        self.assertEqual(resolve_entry_price(settings, "C"), ("LMT", None, True))
        self.assertEqual(resolve_entry_price(settings, "P"), ("LMT", .23, False))
        buy = build_entry_plan("BUY", "LMT", {"bid": .2, "ask": .23}, [(0, .01)], auto_limit=True)
        sell = build_entry_plan("SELL", "LMT", {"bid": .2, "ask": .23}, [(0, .01)], auto_limit=True)
        self.assertEqual((buy["limit_price"], sell["limit_price"]), (.21, .22))
        self.assertTrue(buy["limit_seeded"])
        self.assertEqual(buy["price_mode"], "LMT")
        with self.assertRaises(ValidationError):
            build_entry_plan("BUY", "LMT", {"bid": .2, "ask": .23}, [(0, .01)], auto_limit=False)

    def test_bound_limits_keep_same_contract_and_reseed_changed_side_only(self):
        settings = {"order_mode": "LMT", "limit_prices": {"C": .3, "P": .4},
                    "limit_conids": {"C": 10, "P": 20}}
        self.assertEqual(resolve_entry_price(settings, "C", 10), ("LMT", .3, False))
        self.assertEqual(resolve_entry_price(settings, "C", 11), ("LMT", None, True))
        self.assertEqual(resolve_entry_price(settings, "P", 20), ("LMT", .4, False))
        self.assertEqual(resolve_entry_price(settings, "C"), ("LMT", .3, False))


class V160DemoTests(unittest.TestCase):
    def setUp(self):
        self.b = DemoBroker()
        self.b.connect()
        self.s = dict(symbol="SPY", expiry=self.b.expirations("SPY")[0], trading_class="SPY",
                      right="C", moneyness="OTM", depth=4, percent=50)

    def test_two_independent_side_previews_keep_exact_tokens_and_limits(self):
        pair = self.b.preview_pair({**self.s, "order_mode": "LMT", "limit_prices": {"C": None, "P": .3}})
        self.assertEqual(pair["C"]["contract"]["right"], "C")
        self.assertEqual(pair["P"]["contract"]["right"], "P")
        self.assertTrue(pair["C"]["limit_seeded"])
        self.assertEqual(pair["P"]["limit_price"], .3)
        self.assertNotEqual(pair["C"]["token"], pair["P"]["token"])
        order = self.b.buy(pair["P"]["token"])
        self.assertEqual(order["conid"], pair["P"]["contract"]["conid"])
        with self.assertRaises(ValidationError):
            self.b.buy(pair["C"]["token"])

    def test_one_invalid_side_does_not_hide_other_valid_side(self):
        pair = self.b.preview_pair({**self.s, "order_mode": "LMT", "limit_prices": {"C": -.1, "P": .3}})
        self.assertIn("error", pair["C"])
        self.assertIn("token", pair["P"])

    def test_automatic_strike_change_reseeds_limit_for_new_contract(self):
        settings = {**self.s, "order_mode": "LMT", "limit_prices": {"C": .3}}
        old = self.b.preview(settings)
        settings["limit_conids"] = {"C": old["contract"]["conid"]}
        same = self.b.preview(settings)
        self.assertEqual(same["limit_price"], .3)
        self.assertFalse(same.get("limit_seeded", False))
        with patch.object(DemoBroker, "_spot", return_value=Decimal("601.4")):
            moved = self.b.preview(settings)
            self.assertNotEqual(old["contract"]["conid"], moved["contract"]["conid"])
            self.assertTrue(moved["limit_seeded"])
            self.assertTrue(moved["limit_reseeded"])
            self.assertEqual(moved["limit_price"], .2)
            order = self.b.buy(moved["token"])
            self.assertEqual(order["conid"], moved["contract"]["conid"])
            self.assertEqual(order["limit_price"], moved["limit_price"])

    def test_exact_contract_metadata_selects_same_side_and_rejects_wrong_conid(self):
        row = self.b.chain("SPY", self.s["expiry"])["rows"][0]
        settings = {**self.s, "selection_mode": "strike", "strike": row["strike"],
                    "conids": {"C": row["call"]["conid"], "P": row["put"]["conid"]}}
        pair = self.b.preview_pair(settings)
        self.assertEqual(pair["C"]["contract"]["conid"], row["call"]["conid"])
        self.assertEqual(pair["P"]["contract"]["conid"], row["put"]["conid"])
        with self.assertRaises(ValidationError):
            self.b.preview({**settings, "conids": {"C": row["put"]["conid"]}})

    def test_chain_all_custom_size_and_exact_far_contract(self):
        custom = self.b.chain("SPY", self.s["expiry"], limit=80)
        self.assertEqual(len(custom["rows"]), 80)
        chain = self.b.chain("SPY", self.s["expiry"], limit=0, visible_strikes=[650])
        self.assertEqual(len(chain["rows"]), len(self.b._strikes("SPY")))
        self.assertFalse(chain["has_prev"] or chain["has_next"])
        last = chain["rows"][-1]
        selected = self.b.preview({**self.s, "selection_mode": "strike", "strike": last["strike"],
                                   "conids": {"C": last["call"]["conid"]}})
        self.assertEqual(selected["contract"]["conid"], last["call"]["conid"])

    def test_explicit_cash_preview_and_execution_stay_within_amount(self):
        p = self.b.preview({**self.s, "budget_mode": "amount", "cash_amount": 99})
        self.assertLessEqual(p["buffered_cost"], 99)
        self.b.buy(p["token"])
        self.assertGreaterEqual(self.b.snapshot()["financial"]["usd_cash"], 901)

    def test_explicit_amount_rejects_later_fill_jump_and_amendment(self):
        p = self.b.preview({**self.s, "budget_mode": "amount", "cash_amount": 99})
        original_quote = self.b._quote
        self.b._quote = lambda strike, right, symbol: (Decimal("600.4"), Decimal(".28"), Decimal(".3"))
        order = self.b.buy(p["token"])
        self.assertIn("Cancelled", order["status"])
        self.assertEqual(self.b.snapshot()["cash"], 1000)
        self.assertEqual(self.b.positions(), [])
        self.b._quote = original_quote
        p = self.b.preview({**self.s, "budget_mode": "amount", "cash_amount": 99,
                            "order_mode": "LMT", "limit_price": .2})
        order = self.b.buy(p["token"])
        with self.assertRaisesRegex(ValidationError, "美元预算"):
            self.b.preview_modify(order["order_id"], "LMT", .3)

    def test_short_open_reserves_margin_receives_credit_and_covers(self):
        p = self.b.preview({**self.s, "open_action": "SELL", "seller_quantity": 1, "percent": 100})
        self.assertEqual(p["action"], "SELL")
        self.assertTrue(p["opens_short"])
        self.assertEqual(p["initial_margin_change"], 500)
        self.assertIn("非 IBKR", p["margin_source"])
        order = self.b.buy(p["token"])
        self.assertEqual(order["filled"], 1)
        position = self.b.positions()[0]
        self.assertEqual(position["quantity"], -1)
        snap = self.b.snapshot()
        self.assertEqual(snap["financial"]["usd_cash"], 1018)
        self.assertEqual(snap["financial"]["net_liquidation"], 997)
        self.assertEqual(snap["cash"], 518)
        cover = self.b.close(position["conid"], 100)
        self.assertEqual(cover["action"], "BUY")
        self.assertEqual(self.b.positions(), [])
        self.assertEqual(self.b.snapshot()["cash"], 996)

    def test_short_limit_cancellable_and_not_confused_with_long_exit(self):
        p = self.b.preview({**self.s, "open_action": "SELL", "order_mode": "LMT", "limit_price": .3, "percent": 100})
        order = self.b.buy(p["token"])
        self.assertEqual(order["remaining"], 1)
        self.assertEqual(self.b.snapshot()["cash"], 499)
        with self.assertRaises(ValidationError):
            self.b.preview_modify(order["order_id"], "MKT")
        cancelled = self.b.cancel_order(order["order_id"])
        self.assertIn("Cancelled", cancelled["status"])
        self.assertEqual(self.b.snapshot()["cash"], 1000)
        self.assertEqual(self.b.positions(), [])

    def test_short_partial_cover_and_opposite_side_opening_guards(self):
        self.b._cash = Decimal(2000)
        p = self.b.preview({**self.s, "open_action": "SELL", "seller_quantity": 3, "percent": 100})
        self.b.buy(p["token"])
        position = self.b.positions()[0]
        with self.assertRaises(ValidationError):
            self.b.preview(self.s)
        self.b.close(position["conid"], 50)
        self.assertEqual(self.b.positions()[0]["quantity"], -2)
        self.assertEqual(self.b.positions()[0]["demo_margin"], 1000)
        with self.assertRaises(ValidationError):
            self.b.close(position["conid"], 100, {"type": "STP", "stop_price": 1})
        self.b.close_all("SPY")
        self.assertEqual(self.b.positions(), [])

    def test_seller_margin_rejected_and_never_cash_divided_by_credit(self):
        with self.assertRaises(ValidationError):
            self.b.preview({**self.s, "open_action": "SELL", "seller_quantity": 2})
        p = self.b.preview({**self.s, "open_action": "SELL", "percent": 100})
        self.assertEqual(p["quantity"], 1)

    def test_seller_budget_caps_margin_and_fees_instead_of_premium_credit(self):
        for settings in ({"percent": 50}, {"budget_mode": "amount", "cash_amount": 500}):
            with self.subTest(settings=settings), self.assertRaisesRegex(ValidationError, "保证金预算不足"):
                self.b.preview({**self.s, "open_action": "SELL", **settings})
        preview = self.b.preview({**self.s, "open_action": "SELL", "budget_mode": "amount", "cash_amount": 501})
        self.assertEqual(preview["budget"], 501)
        self.assertEqual(preview["buffered_cost"], 501)
        self.assertEqual(preview["quantity"], 1)


if __name__ == "__main__":
    unittest.main()
