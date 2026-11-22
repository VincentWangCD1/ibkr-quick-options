"""Pure exit validation/order fields; never creates a broker connection."""
from decimal import Decimal
from importlib.util import find_spec
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from core import ValidationError
from exit_orders import build_exit_order, validate_exit_spec


TICKS = [(0, .01)]


class ExitValidationTests(unittest.TestCase):
    def test_limit_normalizes_without_changing_price(self):
        result = validate_exit_spec({"type": " lmt ", "limit_price": "1.230", "tif": "gtc"}, TICKS)
        self.assertEqual(result, {"type": "LMT", "limit_price": 1.23, "stop_price": None,
                                  "trail_mode": None, "trail_value": None, "limit_offset": None, "tif": "GTC"})

    def test_normalized_spec_is_idempotent_and_does_not_mutate_input(self):
        original = {"type": "STP", "stop_price": Decimal("1.20")}
        result = validate_exit_spec(original, TICKS)
        self.assertEqual(original["stop_price"], Decimal("1.20"))
        self.assertEqual(validate_exit_spec(result, TICKS), result)

    def test_stop_limit_rejects_sell_limit_above_trigger(self):
        with self.assertRaisesRegex(ValidationError, "小于或等于"):
            validate_exit_spec({"type": "STP LMT", "stop_price": 1.2, "limit_price": 1.21}, TICKS)
        result = validate_exit_spec({"type": "STP LMT", "stop_price": 1.2, "limit_price": 1.2}, TICKS)
        self.assertEqual(result["limit_price"], result["stop_price"])

    def test_trail_percent_can_omit_initial_stop(self):
        result = validate_exit_spec({"type": "TRAIL", "trail_mode": "PERCENT", "trail_value": "5"}, TICKS)
        self.assertEqual(result["trail_value"], 5)
        self.assertEqual(result["trail_mode"], "percent")
        self.assertIsNone(result["stop_price"])

    def test_trail_percent_is_not_quote_currency_grid(self):
        result = validate_exit_spec({"type": "TRAIL", "trail_mode": "percent", "trail_value": .125}, [(0, .05)])
        self.assertEqual(result["trail_value"], .125)

    def test_trailing_percent_strict_open_interval(self):
        for value in (0, -1, 100, 101, "nan", "inf", True):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                validate_exit_spec({"type": "TRAIL", "trail_mode": "percent", "trail_value": value}, TICKS)

    def test_trail_amount_and_initial_stop_are_distinct_fields(self):
        result = validate_exit_spec({"type": "TRAIL", "trail_mode": "amount", "trail_value": .15, "stop_price": 1.20}, TICKS)
        self.assertEqual(result["trail_value"], .15)
        self.assertEqual(result["stop_price"], 1.2)

    def test_amount_offset_uses_minimum_tick_not_a_premium_tier(self):
        result = validate_exit_spec({"type": "TRAIL", "trail_mode": "amount", "trail_value": .05, "stop_price": 3.10},
                                    [(0, .05), (3, .10)])
        self.assertEqual(result["trail_value"], .05)

    def test_trail_limit_requires_initial_stop_and_explicit_offset(self):
        base = {"type": "TRAIL LIMIT", "trail_mode": "percent", "trail_value": 5}
        with self.assertRaises(ValidationError):
            validate_exit_spec({**base, "limit_offset": .02}, TICKS)
        with self.assertRaises(ValidationError):
            validate_exit_spec({**base, "stop_price": 1.20}, TICKS)

    def test_trail_limit_zero_offset_is_explicit_and_valid(self):
        result = validate_exit_spec({"type": "TRAIL LIMIT", "trail_mode": "amount", "trail_value": .1,
                                     "stop_price": 1.2, "limit_offset": 0}, TICKS)
        self.assertEqual(result["limit_offset"], 0)
        self.assertIsNone(result["limit_price"])

    def test_trail_limit_rejects_fixed_limit_price(self):
        with self.assertRaises(ValidationError):
            validate_exit_spec({"type": "TRAIL LIMIT", "trail_mode": "percent", "trail_value": 5,
                                "stop_price": 1.2, "limit_offset": .05, "limit_price": 1.15}, TICKS)

    def test_trail_limit_initial_limit_must_be_positive(self):
        for offset in (-.01, 1.2, 2):
            with self.subTest(offset=offset), self.assertRaises(ValidationError):
                validate_exit_spec({"type": "TRAIL LIMIT", "trail_mode": "percent", "trail_value": 5,
                                    "stop_price": 1.2, "limit_offset": offset}, TICKS)

    def test_trail_limit_derived_limit_uses_its_own_price_band(self):
        base = {"type": "TRAIL LIMIT", "trail_mode": "percent", "trail_value": 5, "stop_price": 3.10}
        with self.assertRaisesRegex(ValidationError, "卖出限价"):
            validate_exit_spec({**base, "limit_offset": .05}, [(0, .05), (3, .10)])
        result = validate_exit_spec({**base, "limit_offset": .15}, [(0, .05), (3, .10)])
        self.assertEqual(result["limit_offset"], .15)

    def test_prices_are_not_silently_rounded_to_tick(self):
        for spec in ({"type": "LMT", "limit_price": 1.23},
                     {"type": "STP", "stop_price": 1.23},
                     {"type": "TRAIL", "stop_price": 1.23, "trail_mode": "percent", "trail_value": 5}):
            with self.subTest(spec=spec), self.assertRaisesRegex(ValidationError, "不会自动改价"):
                validate_exit_spec(spec, [(0, .05)])

    def test_offset_cannot_have_sub_tick_amount(self):
        with self.assertRaises(ValidationError):
            validate_exit_spec({"type": "TRAIL", "trail_mode": "amount", "trail_value": .015}, TICKS)
        with self.assertRaises(ValidationError):
            validate_exit_spec({"type": "TRAIL LIMIT", "trail_mode": "amount", "trail_value": .1,
                                "stop_price": 1.2, "limit_offset": .015}, TICKS)

    def test_price_band_boundary_uses_new_increment(self):
        tiers = [(0, .05), (3, .10)]
        self.assertEqual(validate_exit_spec({"type": "LMT", "limit_price": 2.95}, tiers)["limit_price"], 2.95)
        self.assertEqual(validate_exit_spec({"type": "LMT", "limit_price": 3}, tiers)["limit_price"], 3)
        with self.assertRaises(ValidationError):
            validate_exit_spec({"type": "LMT", "limit_price": 3.05}, tiers)

    def test_price_grid_is_anchored_at_low_edge_like_execution_policy(self):
        tiers = [(0, .01), (1.03, .05)]
        self.assertEqual(validate_exit_spec({"type": "LMT", "limit_price": 1.08}, tiers)["limit_price"], 1.08)
        with self.assertRaises(ValidationError):
            validate_exit_spec({"type": "LMT", "limit_price": 1.10}, tiers)

    def test_market_rules_accept_tuple_mapping_and_ib_object(self):
        for tiers in ([(0, .01)], [{"lowEdge": 0, "increment": .01}],
                      [SimpleNamespace(lowEdge=0, increment=.01)]):
            with self.subTest(tiers=tiers):
                self.assertEqual(validate_exit_spec({"type": "LMT", "limit_price": 1.23}, tiers)["limit_price"], 1.23)

    def test_invalid_or_conflicting_market_rules_are_rejected(self):
        rules = [None, [], [(1, .01)], [(0, 0)], [(0, -.01)], [(0, .01), (0, .05)],
                 [(0, "nan")], [(0, True)], [(.0,)], [{"increment": .01}], ".01"]
        for tiers in rules:
            with self.subTest(tiers=tiers), self.assertRaises(ValidationError):
                validate_exit_spec({"type": "LMT", "limit_price": 1.2}, tiers)

    def test_inactive_none_fields_are_allowed_but_unused_prices_not_ignored(self):
        result = validate_exit_spec({"type": "LMT", "limit_price": 1.20, "stop_price": None,
                                     "trail_mode": None, "trail_value": "", "limit_offset": None}, TICKS)
        self.assertIsNone(result["stop_price"])
        with self.assertRaises(ValidationError):
            validate_exit_spec({"type": "LMT", "limit_price": 1.20, "stop_price": .8}, TICKS)

    def test_required_type_and_fields_and_tif(self):
        specs = [{}, {"type": "MKT"}, {"type": "LMT"}, {"type": "STP"},
                 {"type": "LMT", "limit_price": 1.2, "tif": "IOC"},
                 {"type": "TRAIL", "trail_value": 5},
                 {"type": "TRAIL", "trail_mode": "amount"},
                 {"type": "TRAIL", "trail_mode": "foo", "trail_value": 5}]
        for spec in specs:
            with self.subTest(spec=spec), self.assertRaises(ValidationError):
                validate_exit_spec(spec, TICKS)

    def test_invalid_values_or_unrepresentable_precision_never_become_zero(self):
        for value in (0, -1, True, "NaN", "Infinity", "1e400", "1e-400", "0.10000000000000000001"):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                validate_exit_spec({"type": "LMT", "limit_price": value}, TICKS)

    def test_unknown_api_fields_cannot_override_fixed_sell_action(self):
        with self.assertRaises(ValidationError):
            validate_exit_spec({"type": "LMT", "limit_price": 1.2, "action": "BUY"}, TICKS)


@unittest.skipUnless(find_spec("ib_async"), "ib_async is optional for pure validation")
class ExitOrderConstructionTests(unittest.TestCase):
    def build(self, spec, outside=False):
        normalized = validate_exit_spec(spec, TICKS)
        # Catch any accidental future transport/client creation in this helper.
        with patch("ib_async.IB", side_effect=AssertionError("No broker client may be created")):
            return build_exit_order(normalized, 3, "DU123", "QO1:test", outside_rth=outside)

    def test_limit_sell_retains_price_account_quantity_and_lifetime(self):
        from ib_async.util import UNSET_DOUBLE
        order = self.build({"type": "LMT", "limit_price": 1.2, "tif": "GTC"}, True)
        self.assertEqual((order.action, order.orderType, order.totalQuantity), ("SELL", "LMT", 3))
        self.assertEqual((order.account, order.orderRef, order.tif), ("DU123", "QO1:test", "GTC"))
        self.assertEqual(order.lmtPrice, 1.2)
        self.assertEqual(order.auxPrice, UNSET_DOUBLE)
        self.assertTrue(order.outsideRth)

    def test_stop_uses_aux_price_and_default_broker_trigger(self):
        from ib_async.util import UNSET_DOUBLE
        order = self.build({"type": "STP", "stop_price": .8})
        self.assertEqual(order.auxPrice, .8)
        self.assertEqual(order.lmtPrice, UNSET_DOUBLE)
        self.assertEqual(order.triggerMethod, 0)
        self.assertFalse(order.outsideRth)
        self.assertFalse(order.conditionsIgnoreRth)
        self.assertEqual(order.conditions, [])

    def test_stop_limit_sets_both_correct_fields(self):
        order = self.build({"type": "STP LMT", "stop_price": 1.2, "limit_price": 1.1})
        self.assertEqual((order.orderType, order.auxPrice, order.lmtPrice), ("STP LMT", 1.2, 1.1))

    def test_percent_trail_leaves_amount_and_initial_stop_unset(self):
        from ib_async.util import UNSET_DOUBLE
        order = self.build({"type": "TRAIL", "trail_mode": "percent", "trail_value": 5})
        self.assertEqual(order.trailingPercent, 5)
        self.assertEqual(order.auxPrice, UNSET_DOUBLE)
        self.assertEqual(order.trailStopPrice, UNSET_DOUBLE)
        self.assertEqual(order.lmtPrice, UNSET_DOUBLE)

    def test_amount_trail_never_also_sets_trailing_percent(self):
        from ib_async.util import UNSET_DOUBLE
        order = self.build({"type": "TRAIL", "trail_mode": "amount", "trail_value": .15, "stop_price": 1.2})
        self.assertEqual(order.auxPrice, .15)
        self.assertEqual(order.trailingPercent, UNSET_DOUBLE)
        self.assertEqual(order.trailStopPrice, 1.2)

    def test_trail_limit_only_sets_limit_offset_not_limit_price(self):
        from ib_async.util import UNSET_DOUBLE
        order = self.build({"type": "TRAIL LIMIT", "trail_mode": "percent", "trail_value": 5,
                            "stop_price": 1.2, "limit_offset": 0})
        self.assertEqual(order.orderType, "TRAIL LIMIT")
        self.assertEqual(order.trailStopPrice, 1.2)
        self.assertEqual(order.lmtPriceOffset, 0)
        self.assertEqual(order.lmtPrice, UNSET_DOUBLE)
        self.assertEqual(order.auxPrice, UNSET_DOUBLE)
        self.assertEqual(order.trailingPercent, 5)

    def test_market_producing_conditional_types_cannot_enable_outside_rth(self):
        specs = [{"type": "STP", "stop_price": 1.2},
                 {"type": "TRAIL", "trail_mode": "percent", "trail_value": 5}]
        for spec in specs:
            with self.subTest(spec=spec), self.assertRaises(ValidationError):
                self.build(spec, True)

    def test_limit_producing_conditional_types_request_outside_rth_for_broker_validation(self):
        specs = [{"type": "STP LMT", "stop_price": 1.2, "limit_price": 1.1},
                 {"type": "TRAIL LIMIT", "trail_mode": "amount", "trail_value": .1, "stop_price": 1.2, "limit_offset": .05}]
        for spec in specs:
            with self.subTest(spec=spec):
                order = self.build(spec, True)
                self.assertTrue(order.outsideRth)
                self.assertEqual(order.orderType, spec["type"])
                self.assertEqual(order.conditions, [])
                self.assertEqual(order.triggerMethod, 0)

    def test_builder_revalidates_basic_constraints_and_cannot_override_action(self):
        with self.assertRaises(ValidationError):
            build_exit_order({"type": "STP LMT", "stop_price": 1, "limit_price": 2}, 1, "DU123", "ref")
        with self.assertRaises(ValidationError):
            build_exit_order({"type": "LMT", "limit_price": 1, "action": "BUY"}, 1, "DU123", "ref")

    def test_invalid_quantity_account_reference_or_flag_rejected(self):
        spec = {"type": "LMT", "limit_price": 1.2}
        for quantity in (0, -1, .5, True, "nan"):
            with self.subTest(quantity=quantity), self.assertRaises(ValidationError):
                build_exit_order(spec, quantity, "DU123", "ref")
        for account, ref in (("", "ref"), ("DU123", ""), ("DU123\0", "ref"), ("DU123", "ref\n")):
            with self.subTest(account=account, ref=ref), self.assertRaises(ValidationError):
                build_exit_order(spec, 1, account, ref)
        with self.assertRaises(ValidationError):
            build_exit_order(spec, 1, "DU123", "ref", outside_rth="false")


if __name__ == "__main__":
    unittest.main()
