"""V1.6 controller integration, without creating windows or broker connections."""
import json
import time
import tkinter as tk
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app import App
from entry_ui import direction_label, parse_chain_limit
from tests.test_app_state import controller, Variable, Widget, chain_data


def entry_controller():
    ui = controller()
    ui.right, ui.percent, ui.percent_text = Variable("C"), Variable(50), Variable("")
    ui.slippage, ui.fee, ui.min_fee, ui.cap = Variable(5), Variable(1), Variable(1), Variable(10000)
    ui.side_vars = {side: {key: Variable("") for key in ("contract", "quotes", "amount", "quantity", "status", "detail")} for side in ("C", "P")}
    ui.side_buttons = {side: Widget() for side in ("C", "P")}
    ui.side_frames = {side: Widget() for side in ("C", "P")}
    del ui.fingerprint
    return ui


def preview(side="C", **extra):
    result = dict(token="token-" + side, contract=dict(local_symbol="TEST " + side, strike=100, right=side, conid=123 if side == "C" else 124),
                  quantity=2, bid=1.2, ask=1.4, buffered_cost=262, estimated_premium=260, fee_reserve=2,
                  order_type="LMT", limit_price=1.3, price_mode="LMT", created_at=time.time(), session_label="常规时段")
    result.update(extra)
    return result


class EntryUiTests(unittest.TestCase):
    def test_direction_buttons_and_seller_layout(self):
        self.assertEqual([direction_label("BUY", side) for side in ("C", "P")], ["买Call（做多）", "买Put（做空）"])
        self.assertEqual([direction_label("SELL", side) for side in ("P", "C")], ["卖Put（做多）", "卖Call（做空）"])
        ui = entry_controller()
        ui.open_side.set("卖出期权（卖方）")
        ui.open_side_changed()
        self.assertEqual(ui.side_frames["P"].options["column"], 0)
        self.assertEqual(ui.side_frames["C"].options["column"], 1)
        self.assertEqual(ui.settings()["open_action"], "SELL")

    def test_decimal_percent_and_exact_amount_have_distinct_settings(self):
        ui = entry_controller()
        ui.percent_input.set("0.125")
        self.assertEqual(ui.settings()["percent"], .125)
        ui.budget_mode.set("金额 USD")
        ui.cash_amount.set("123.45")
        self.assertEqual(ui.settings()["cash_amount"], 123.45)
        self.assertEqual(ui.settings()["budget_mode"], "amount")
        ui.set_percent(25)
        self.assertEqual(ui.settings()["percent"], 25)
        self.assertEqual(ui.settings()["budget_mode"], "percent")

    def budget_controller(self):
        ui = entry_controller()
        ui.selection_mode = tk.StringVar(master=tk.Tcl(), value="按档位")
        ui.selection_mode.trace_add("write", ui.selection_mode_changed)
        ui.request_preview = Mock()
        return ui

    def test_budget_exit_by_same_percent_restores_sizing_and_rejects_late_quote(self):
        from core import size_order
        ui = self.budget_controller()
        ui.match_budget()
        settings = ui.settings()
        self.assertEqual((settings["selection_mode"], settings["rounding_mode"]), ("budget", "floor"))
        scan_context = dict(ui.context(), form=ui.fingerprint())
        self.assertEqual(size_order(1000, 50, 3, 100, rounding_mode=settings["rounding_mode"])["quantity"], 1)
        ui.set_percent(50)
        settings = ui.settings()
        self.assertEqual((settings["selection_mode"], settings["rounding_mode"], settings["percent"]),
                         ("depth", "nearest", 50))
        self.assertEqual(size_order(1000, 50, 3, 100, rounding_mode=settings["rounding_mode"])["quantity"], 2)
        self.assertFalse(ui.context_is_current("preview_pair", scan_context))
        self.assertEqual(ui.request_preview.call_count, 2)

    def test_explicit_budget_exit_restores_exact_contract_amount_and_floor(self):
        ui = self.budget_controller()
        ui.selection_mode.set("指定行权价")
        ui.exact_strike.set("600")
        ui.selected_conids = {"P": 123}
        ui.budget_mode.set("金额 USD")
        ui.cash_amount.set("320")
        ui.rounding_mode.set("向下取整")
        before = ui.settings()
        ui.match_budget()
        first = ui.settings()["budget_search_id"]
        ui.refresh_entry_match()
        self.assertGreater(ui.settings()["budget_search_id"], first)
        ui.match_budget()
        self.assertEqual(ui.settings(), before)
        self.assertEqual(ui.request_preview.call_count, 3)

    def test_invalid_cash_never_reaches_worker(self):
        ui = entry_controller()
        ui.budget_mode.set("金额 USD")
        for value in ("", "0", "NaN", "inf", "-1"):
            ui.cash_amount.set(value)
            with self.subTest(value=value), self.assertRaises(ValueError):
                ui.settings()

    def test_arbitrary_depth_and_exact_strike_ignore_unused_depth(self):
        ui = entry_controller()
        ui.depth.set("250")
        self.assertEqual(ui.settings()["depth"], 250)
        ui.selection_mode.set("指定行权价")
        ui.exact_strike.set("100.5")
        ui.depth.set("unused")
        ui.selected_conids = {"C": 123}
        settings = ui.settings()
        self.assertEqual((settings["selection_mode"], settings["strike"], settings["conids"]), ("strike", 100.5, {"C": 123}))

    def test_limit_empty_seeds_both_sides_independently(self):
        ui = entry_controller()
        ui.entry_mode.set("限价")
        original = ui.fingerprint()
        self.assertEqual(ui.settings()["limit_prices"], {"C": None, "P": None})
        ui.render_pair({"C": preview(limit_seeded=True), "P": preview("P", limit_price=2.2, limit_seeded=True)}, {"form": original})
        self.assertEqual(ui.entry_limits["C"].get(), "1.3")
        self.assertEqual(ui.entry_limits["P"].get(), "2.2")
        self.assertNotEqual(ui.fingerprint(), original)
        self.assertEqual(ui.side_fingerprints["C"], ui.fingerprint())
        self.assertEqual(ui.side_fingerprints["P"], ui.fingerprint())

    def test_manual_limit_survives_refresh_and_budget_change(self):
        ui = entry_controller()
        ui.entry_mode.set("限价")
        ui.entry_limits["C"].set("1.1")
        ui.render_pair({"C": preview(limit_price=1.1), "P": preview("P", limit_price=2.2, limit_seeded=True)}, {})
        ui.cash_amount.set("100")
        ui.changed()
        self.assertEqual(ui.entry_limits["C"].get(), "1.1")
        self.assertEqual(ui.entry_limits["P"].get(), "2.2")
        ui.contract_input_changed()
        self.assertEqual(ui.entry_limits["C"].get(), "")
        self.assertEqual(ui.entry_limits["P"].get(), "")

    def test_new_matched_contract_reseeds_limit_but_mode_switch_keeps_binding(self):
        ui = entry_controller()
        ui.entry_mode.set("限价")
        ui.entry_limits["C"].set("1.1")
        ui.render_pair({"C": preview(limit_price=1.1)}, {})
        self.assertEqual(ui.settings()["limit_conids"], {"C": 123})
        moved = preview(contract=dict(local_symbol="NEW C", strike=101, right="C", conid=125),
                        limit_seeded=True, limit_reseeded=True, limit_price=1.5)
        ui.render_pair({"C": moved}, {})
        self.assertEqual(ui.entry_limits["C"].get(), "1.5")
        self.assertEqual(ui.settings()["limit_conids"], {"C": 125})
        ui.entry_mode.set("市价")
        ui.render_pair({"C": preview(price_mode="MKT")}, {})
        self.assertEqual(ui.limit_conids, {"C": 125})
        ui.reset_entry_prices()
        self.assertEqual(ui.limit_conids, {})

    def test_failed_side_does_not_disable_other_preview(self):
        ui = entry_controller()
        ui.render_pair({"C": {"error": "等待盘口"}, "P": preview("P")}, {})
        self.assertEqual(set(ui.side_previews), {"P"})
        self.assertEqual(ui.side_vars["C"]["status"].get(), "等待盘口")
        self.assertIn("262.00", ui.side_vars["P"]["amount"].get())
        ui.submit = Mock()
        ui.execute_side("P")
        ui.submit.assert_called_once_with("buy", token="token-P")

    def test_clicked_side_uses_its_token_and_is_single_use(self):
        ui = entry_controller()
        ui.render_pair({"C": preview(), "P": preview("P")}, {})
        ui.submit = Mock()
        ui.execute_side("P")
        ui.execute_side("P")
        ui.submit.assert_called_once_with("buy", token="token-P")
        self.assertEqual(ui.side_previews, {})

    def test_stale_form_and_expired_preview_do_not_submit(self):
        for cause in ("form", "time"):
            ui = entry_controller()
            ui.render_pair({"C": preview()}, {})
            ui.submit = Mock()
            if cause == "form":
                ui.percent_input.set("20")
            else:
                ui.side_previews["C"]["created_at"] -= 11
            ui.execute_side("C")
            ui.submit.assert_not_called()

    def test_sell_requires_confirmation_and_rejects_edit_during_dialog(self):
        for answer in (False, True):
            ui = entry_controller()
            ui.open_side.set("卖出期权（卖方）")
            ui.render_pair({"P": preview("P", opens_short=True, estimated_credit=260, margin_required=500)}, {})
            ui.submit = Mock()
            def confirm(*args, **kwargs):
                if answer:
                    ui.seller_quantity.set("not a number")
                return answer
            with patch("entry_ui.messagebox.askyesno", side_effect=confirm) as ask:
                ui.execute_side("P")
            ask.assert_called_once()
            self.assertIn("不是平已有多头", ask.call_args.args[1])
            ui.submit.assert_not_called()
            self.assertFalse(ui.entry_confirmation_open)

    def test_sell_confirmation_accepts_exact_preview(self):
        ui = entry_controller()
        ui.open_side.set("卖出期权（卖方）")
        ui.render_pair({"P": preview("P", opens_short=True)}, {})
        ui.submit = Mock()
        with patch("entry_ui.messagebox.askyesno", return_value=True):
            ui.execute_side("P")
        ui.submit.assert_called_once_with("buy", token="token-P")

    def test_double_click_selects_contract_without_sending_order(self):
        ui = entry_controller()
        ui.chain_rows_by_id = {"k100": dict(strike=100, call={"conid": 123}, put={"conid": 124})}
        ui.chain_tree.identify_row = Mock(return_value="k100")
        ui.chain_tree.identify_column = Mock(return_value="#7")
        ui.request_preview, ui.submit = Mock(), Mock()
        ui.select_chain_contract(SimpleNamespace(x=20, y=30))
        self.assertEqual(ui.settings()["conids"], {"C": 123, "P": 124})
        self.assertEqual(ui.right.get(), "P")
        ui.request_preview.assert_called_once()
        ui.submit.assert_not_called()
        for column in ("", "#0", "#5"):
            ui.chain_tree.identify_column.return_value = column
            ui.select_chain_contract(SimpleNamespace(x=20, y=30))
        self.assertEqual(ui.request_preview.call_count, 1)

    def test_all_and_custom_chain_count_commit_invalid_text_keeps_last(self):
        ui = entry_controller()
        for text, expected in (("250", 250), ("所有档数", 0), ("5", 5)):
            ui.chain_width.set(text)
            ui.chain_width_changed()
            self.assertEqual(ui.chain_limit(), expected)
        ui.chain_width.set("NaN")
        ui.chain_width_changed()
        self.assertEqual(ui.chain_limit(), 5)
        for value in ("-1", "1.5", "inf"):
            with self.assertRaises(ValueError):
                parse_chain_limit(value)

    def test_all_chain_submits_visible_strikes_after_scroll(self):
        ui = entry_controller()
        ui.preview_data = None
        ui.render_chain(chain_data())
        ui.chain_tree.view = (.5, .7)
        ui.need_expiries = ui.need_classes = ui.busy = False
        ui.submit = Mock()
        ui.request_chain()
        visible = ui.submit.call_args.kwargs["visible_strikes"]
        self.assertTrue(visible)
        self.assertLess(len(visible), 40)
        self.assertGreater(min(visible), 81)

    def test_periodic_expirations_preserve_manual_form_until_removed(self):
        ui = entry_controller()
        ui.entry_limits["C"].set("1.1")
        ui.handle_result("expirations", ["20261111", "20261112"], None, ui.context())
        self.assertEqual(ui.entry_limits["C"].get(), "1.1")
        ui.handle_result("expirations", ["20261112"], None, ui.context())
        self.assertEqual(ui.expiry.get(), "20261112")
        self.assertEqual(ui.entry_limits["C"].get(), "")
        self.assertTrue(ui.need_classes)

    def test_obsolete_pair_result_cannot_seed_new_contract_price(self):
        ui = entry_controller()
        context = dict(ui.context(), form=ui.fingerprint())
        ui.depth.set("8")
        ui.handle_result("preview_pair", {"C": preview(limit_seeded=True)}, None, context)
        self.assertEqual(ui.entry_limits["C"].get(), "")
        self.assertEqual(ui.side_previews, {})


if __name__ == "__main__":
    unittest.main()
