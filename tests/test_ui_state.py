"""UI display regressions without importing or opening Tk."""
from copy import deepcopy
from decimal import Decimal
import unittest

from ui_state import (build_fund_cards, default_depth, empty_chain_message, money_text,
                      chain_rows_with_reference, quote_age_text, centered_chain_fraction,
                      close_order_fields, build_close_order_spec, order_parameters_text,
                      order_number_text, close_order_confirmation)


def snapshot(mode="LIVE", **changes):
    state = {
        "mode": mode, "account": "DEMO-1000" if mode == "DEMO" else "U123456",
        "connected": True,
        "financial": {
            "net_liquidation": 1250, "net_liquidation_currency": "USD",
            "usd_cash": 1000, "settled_cash": 800, "available_funds": 900,
            "cash": 800, "base_currency": "USD", "ready": True, "error": "",
        },
    }
    state.update(changes)
    return state


class DepthDisplayTests(unittest.TestCase):
    def test_category_defaults(self):
        self.assertEqual(default_depth("OTM"), 4)
        self.assertEqual(default_depth("ITM"), 1)
        self.assertEqual(default_depth("ATM"), 1)

    def test_unrecognized_category_is_not_silently_mapped(self):
        for value in ("otm", "虚值 OTM", "", None, "INVALID"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                default_depth(value)


class ChainDisplayTests(unittest.TestCase):
    def test_reference_divider_splits_actual_sorted_strikes_and_labels_both_sides(self):
        rows = [{"strike": strike} for strike in (102, 99, 101, 100)]
        original = deepcopy(rows)
        display = chain_rows_with_reference(rows, 100.5)
        self.assertEqual([entry["strike"] for entry in display], [99, 100, 100.5, 101, 102])
        self.assertEqual(display[2]["kind"], "reference")
        self.assertEqual((display[0]["call_zone"], display[0]["put_zone"]), ("实值", "虚值"))
        self.assertEqual((display[-1]["call_zone"], display[-1]["put_zone"]), ("虚值", "实值"))
        self.assertEqual(rows, original)

    def test_exact_reference_strike_is_atm_not_wrongly_itm(self):
        rows = chain_rows_with_reference([{"strike": 100}, {"strike": 99}], 100)
        self.assertEqual([entry["kind"] for entry in rows], ["contract", "reference", "contract"])
        self.assertEqual((rows[-1]["call_zone"], rows[-1]["put_zone"]), ("平值", "平值"))

    def test_missing_reference_does_not_create_arbitrary_middle(self):
        for reference in (None, float("nan"), float("inf"), 0, -1, True):
            with self.subTest(reference=reference):
                self.assertEqual(chain_rows_with_reference([{"strike": 100}], reference), [])

    def test_bid_and_ask_ages_are_independent_not_quote_expiration(self):
        text = quote_age_text({"quote_type": "实时订阅", "bid_age": 2, "ask_age": 12, "age": 2})
        self.assertEqual(text, "实时订阅 · 买 2s / 卖 12s")
        self.assertNotIn("过期", text)
        self.assertEqual(quote_age_text({"age": 7}), "无报价 · 买 — / 卖 —")

    def test_reference_centering_respects_viewport_edges(self):
        self.assertAlmostEqual(centered_chain_fraction(20, 41, 8), 16 / 41)
        self.assertEqual(centered_chain_fraction(1, 41, 8), 0)
        self.assertAlmostEqual(centered_chain_fraction(40, 41, 8), 33 / 41)
        self.assertEqual(centered_chain_fraction(2, 4, 8), 0)
        self.assertEqual(centered_chain_fraction(0, 0, 8), 0)


class CloseOrderDisplayTests(unittest.TestCase):
    def test_outside_rth_request_is_not_presented_as_confirmed_protection(self):
        text = order_parameters_text({"outside_rth": True, "execution_warning": "IBKR 已忽略盘外设置"})
        self.assertIn("已请求延长时段", text)
        self.assertIn("已忽略盘外设置", text)

    def test_order_types_use_only_their_visible_price_fields(self):
        values = dict(limit_price="2.5", stop_price="2.8", trail_mode="percent", trail_value="5", limit_offset="0.1", tif="GTC")
        expected = {
            "LMT": {"type": "LMT", "tif": "GTC", "limit_price": 2.5},
            "STP": {"type": "STP", "tif": "GTC", "stop_price": 2.8},
            "STP LMT": {"type": "STP LMT", "tif": "GTC", "stop_price": 2.8, "limit_price": 2.5},
            "TRAIL": {"type": "TRAIL", "tif": "GTC", "trail_mode": "percent", "trail_value": 5.0},
            "TRAIL LIMIT": {"type": "TRAIL LIMIT", "tif": "GTC", "stop_price": 2.8, "trail_mode": "percent", "trail_value": 5.0, "limit_offset": .1},
        }
        for kind, result in expected.items():
            with self.subTest(kind=kind):
                self.assertEqual(build_close_order_spec(kind, values), result)
        self.assertEqual(build_close_order_spec("LMT", {"limit_price": "2", "stop_price": "malformed"}),
                         {"type": "LMT", "tif": "DAY", "limit_price": 2.0})

    def test_trail_limit_requires_initial_stop_but_plain_trail_does_not(self):
        fields = dict(trail_mode="amount", trail_value="0.2", limit_offset="0.1")
        self.assertNotIn("stop_price", close_order_fields("TRAIL"))
        self.assertIn("stop_price", close_order_fields("TRAIL LIMIT"))
        self.assertEqual(build_close_order_spec("TRAIL", fields)["trail_mode"], "amount")
        with self.assertRaisesRegex(ValueError, "触发价"):
            build_close_order_spec("TRAIL LIMIT", fields)

    def test_invalid_prices_modes_percentages_and_sell_stop_limit_are_rejected(self):
        for value in ("", "NaN", "inf", 0, -1, True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                build_close_order_spec("LMT", {"limit_price": value})
        for fields in ({"trail_mode": "percent", "trail_value": 100}, {"trail_mode": "money", "trail_value": 1}):
            with self.assertRaises(ValueError):
                build_close_order_spec("TRAIL", fields)
        with self.assertRaisesRegex(ValueError, "高于"):
            build_close_order_spec("STP LMT", {"stop_price": 1, "limit_price": 1.2})
        with self.assertRaisesRegex(ValueError, "有效期"):
            build_close_order_spec("LMT", {"limit_price": 1, "tif": "IOC"})

    def test_order_display_suppresses_unset_sentinels_and_shows_trailing_units(self):
        self.assertEqual(order_number_text(1.7976931348623157e308), "—")
        self.assertEqual(order_number_text(None), "—")
        self.assertEqual(order_parameters_text({"trail_mode": "percent", "trail_value": 5, "limit_offset": .1}), "移动 5%；限价偏移 0.1")
        self.assertEqual(order_parameters_text({"trail_mode": "amount", "trail_value": .2}), "移动 0.2 美元价差")

    def test_confirmation_has_contract_quantity_initial_stop_and_price_units(self):
        spec = build_close_order_spec("TRAIL LIMIT", dict(stop_price=1.2, trail_mode="percent", trail_value=5, limit_offset=.1, tif="DAY"))
        text = close_order_confirmation({"local_symbol": "SPXW test"}, 3, 50.0, spec)
        for expected in ("SPXW test", "50%", "3 张", "TRAIL LIMIT", "DAY", "初始止损 1.2", "美元/份", "不是到盈利价才启动", "常规时段外触发/成交", "限价单可能不成交"):
            self.assertIn(expected, text)

    def test_confirmation_distinguishes_market_trigger_and_limit_trigger_sessions(self):
        values = dict(stop_price=1.2, limit_price=1.1, trail_mode="percent", trail_value=5, limit_offset=.1)
        for kind in ("STP", "TRAIL", "LMT", "STP LMT", "TRAIL LIMIT"):
            spec = build_close_order_spec(kind, values)
            text = close_order_confirmation({"local_symbol": "SPXW test"}, 1, 100.0, spec)
            with self.subTest(kind=kind):
                if kind in ("STP", "TRAIL"):
                    self.assertIn("仅常规时段触发", text)
                    self.assertIn("触发后为市价单", text)
                    self.assertNotIn("时段外触发/成交", text)
                else:
                    self.assertIn("常规时段外", text)
                    self.assertIn("须 IBKR 支持", text)
                    self.assertNotIn("仅常规时段", text)


class MoneyDisplayTests(unittest.TestCase):
    def test_usd_and_non_usd_keep_denominations(self):
        self.assertEqual(money_text(1000), "$1,000.00")
        self.assertEqual(money_text(10, "HKD"), "HKD 10.00")
        self.assertEqual(money_text(Decimal("10.25"), "eur"), "EUR 10.25")
        self.assertEqual(money_text(-12), "$-12.00")

    def test_unknown_values_do_not_look_like_zero(self):
        for value in (None, "", "bad", float("nan"), float("inf"), True):
            with self.subTest(value=value):
                self.assertEqual(money_text(value), "—")
        self.assertEqual(money_text(0), "$0.00")

    def test_base_without_actual_currency_never_looks_like_usd(self):
        self.assertEqual(money_text(1000, "BASE"), "1,000.00（币种待确认）")
        self.assertEqual(money_text(1000, ""), "1,000.00（币种待确认）")


class FundCardTests(unittest.TestCase):
    def test_demo_funds_are_unmistakably_fictional(self):
        cards = build_fund_cards(snapshot("DEMO"))
        self.assertEqual(cards["usd_cash"], "$1,000.00（演示虚构）")
        for key in ("connection", "detail", "equity", "settled_cash", "order_cash"):
            self.assertIn("演示虚构", cards[key])

    def test_disconnect_discards_even_populated_demo_data(self):
        state = snapshot("DEMO")
        self.assertIn("1,000", build_fund_cards(state)["usd_cash"])
        state["connected"] = False
        cards = build_fund_cards(state)
        for key in ("equity", "usd_cash", "settled_cash", "order_cash"):
            self.assertEqual(cards[key], "—")
        self.assertNotIn("1,000", str(cards))
        self.assertIn("未连接", cards["connection"])

    def test_live_connection_failure_does_not_use_old_demo_cash(self):
        state = snapshot("LIVE", connected=False, cash=1000)
        cards = build_fund_cards(state)
        self.assertNotIn("1,000", str(cards))
        self.assertNotIn("演示虚构", str(cards))

    def test_connected_real_account_never_uses_legacy_top_level_cash(self):
        state = snapshot("LIVE", financial={}, cash=1000)
        cards = build_fund_cards(state)
        self.assertEqual(cards["usd_cash"], "—")
        self.assertEqual(cards["order_cash"], "暂停开仓")
        self.assertNotIn("1,000", str(cards))

    def test_settlement_difference_and_local_hold_are_shown_separately(self):
        state = snapshot()
        state["financial"].update(net_liquidation=1140.45, usd_cash=1140.18, settled_cash=933.18,
                                  settlement_cash_cap=933.18, cash_before_local_hold=933.18,
                                  cash=933.18, available_funds=1140)
        cards = build_fund_cards(state)
        self.assertEqual(cards["order_cash"], "$933.18")
        self.assertIn("结算上限", cards["detail"])
        self.assertIn("美元现金高出该上限 $207.00", cards["detail"])
        self.assertIn("本地买单预留 $0.00", cards["detail"])
        state["cash_reserve"] = 10
        state["financial"]["cash"] -= 10
        cards = build_fund_cards(state)
        self.assertEqual(cards["order_cash"], "$923.18")
        self.assertIn("高出该上限 $207.00", cards["detail"])
        self.assertIn("本地实际占用 $10.00", cards["detail"])

    def test_missing_settlement_still_shows_known_actual_cash(self):
        state = snapshot()
        state["financial"].update(settled_cash=None, cash=None, ready=False, error="缺少已结算资金")
        cards = build_fund_cards(state)
        self.assertEqual(cards["usd_cash"], "$1,000.00")
        self.assertEqual(cards["settled_cash"], "—")
        self.assertEqual(cards["order_cash"], "暂停开仓")
        self.assertIn("缺少已结算资金", cards["detail"])

    def test_header_reconciles_equity_with_same_currency_account_cash(self):
        state = snapshot()
        state["financial"].update(net_liquidation=1062.44, total_cash_value=1020.44, usd_cash=1020.44)
        cards = build_fund_cards(state)
        self.assertEqual(cards["equity"], "$1,062.44")
        self.assertEqual(cards["usd_cash"], "$1,020.44")
        self.assertIn("持仓净值 $42.00", cards["detail"])
        state["financial"].update(net_liquidation_currency="EUR", total_cash_value=1082.44)
        detail = build_fund_cards(state)["detail"]
        self.assertIn("现金 EUR 1,082.44、持仓净值 EUR -20.00", detail)
        state["financial"].pop("total_cash_value")
        self.assertNotIn("持仓净值", build_fund_cards(state)["detail"])

    def test_non_usd_equity_is_not_displayed_as_dollars(self):
        state = snapshot()
        state["financial"].update(net_liquidation_currency="HKD", base_currency="HKD", ready=False)
        cards = build_fund_cards(state)
        self.assertEqual(cards["equity"], "HKD 1,250.00")
        self.assertEqual(cards["usd_cash"], "$1,000.00")
        self.assertEqual(cards["order_cash"], "暂停开仓")

    def test_ready_flag_does_not_format_invalid_budget(self):
        for value in (None, float("nan"), float("inf"), -1):
            state = snapshot()
            state["financial"]["cash"] = value
            self.assertEqual(build_fund_cards(state)["order_cash"], "暂停开仓")

    def test_account_structure_does_not_get_presented_as_margin_permission(self):
        state = snapshot()
        state["financial"].update(account_type_label="账户结构 INDIVIDUAL；现金/保证金类型未确认", settled_as_of="20261111")
        cards = build_fund_cards(state)
        self.assertIn("现金/保证金类型未确认", cards["detail"])
        self.assertIn("结算明细日期 20261111", cards["detail"])
        self.assertEqual(cards["settled_cash"], "$800.00")

    def test_valid_zero_is_a_known_balance_and_input_is_unchanged(self):
        state = snapshot()
        state["financial"].update(usd_cash=0, settled_cash=0, cash=0)
        original = deepcopy(state)
        cards = build_fund_cards(state)
        self.assertEqual(cards["order_cash"], "$0.00")
        self.assertEqual(state, original)

    def test_chain_empty_states(self):
        self.assertIn("已清空", empty_chain_message(False, "SPY", "20261111"))
        self.assertIn("标的", empty_chain_message(True))
        self.assertIn("到期日", empty_chain_message(True, "SPY"))
        self.assertIn("等待", empty_chain_message(True, "SPY", "20261111"))


if __name__ == "__main__":
    unittest.main()
