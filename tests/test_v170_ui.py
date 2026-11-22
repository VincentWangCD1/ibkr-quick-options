"""Offline controller coverage for position close tickets and history display."""
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from app import App
from positions_ui import position_values
from test_app_state import controller, Variable, Widget, Tree


def close_controller():
    ui = controller()
    ui.submit, ui.mutate = Mock(), Mock()
    ui.positions_by_id = {"71": dict(conid=71, quantity=2, local_symbol="XSP C751")}
    ui.pos_tree.selection_set("71")
    ui.position_close_dialog = dict(
        window=Mock(), id="ticket", conid=71, position_quantity=2,
        mode=Variable("限价"), percent=Variable("100"), limit=Variable(""),
        text=Variable(""), limit_entry=Widget(), submit_btn=Widget(),
        preview=None, suppress=False)
    return ui


def preview(**changes):
    result = dict(token="frozen-close", created_at=time.time(), action="SELL", quantity=2,
                  position_quantity=2, bid=.10, ask=.14, limit_price=.12,
                  order_type="LMT", limit_seeded=True, session_label="延长交易时段")
    result.update(changes)
    return result


class PositionUiTests(unittest.TestCase):
    def test_direction_on_each_side_of_calls_and_puts(self):
        for right, size, direction in (("C", 1, "↑ 做多"), ("P", 1, "↓ 做空"),
                                       ("C", -1, "↓ 做空"), ("P", -1, "↑ 做多")):
            with self.subTest(right=right, size=size):
                self.assertIn(direction, position_values(dict(right=right, quantity=size))[0])

    def test_first_position_is_selected_and_multiple_positions_keep_the_choice(self):
        ui = controller()
        ui.render_positions([dict(conid=23, quantity=1)])
        self.assertEqual(ui.selected_position()["conid"], 23)
        ui.render_positions([dict(conid=19, quantity=2), dict(conid=23, quantity=1)])
        self.assertEqual(ui.selected_position()["conid"], 23)

    def test_empty_position_table_distinguishes_pending_fill_and_order_lock(self):
        ui = controller()
        ui.position_summary = Variable("")
        ui.bean_mode = True
        ui.snapshot_data = {"position_sync_pending": True}
        ui.render_positions([])
        self.assertIn("正在核对持仓", ui.position_summary.get())
        ui.locked = True
        ui.render_positions([])
        self.assertIn("发单已锁定", ui.position_summary.get())
        ui.locked = False
        ui.snapshot_data = {"position_open_unconfirmed": True}
        ui.render_positions([])
        self.assertIn("刷新持仓 / 回报", ui.position_summary.get())
        ui.snapshot_data = {}
        ui.render_positions([])
        self.assertEqual(ui.position_summary.get(), "当前无标准期权持仓")

    def test_visible_search_budget_uses_verified_funds_and_masks_dollars(self):
        ui = controller()
        ui.bean_mode = False
        ui.snapshot_data = {"financial": {"cash": 80}}
        ui.budget_mode, ui.percent_input, ui.cash_amount = Variable("比例 %"), Variable("25"), Variable("100")
        self.assertIn("$20.00", ui.budget_summary_text())
        ui.budget_mode.set("金额 USD")
        self.assertIn("$80.00", ui.budget_summary_text())
        ui.bean_mode = True
        self.assertNotIn("80", ui.budget_summary_text())
        ui.financial_ready = False
        self.assertIn("等待资金核验", ui.budget_summary_text())

    def test_position_units_unknown_values_and_short_direction(self):
        values = position_values(dict(local_symbol="XSP", quantity=-2, avg_option_price=.1274,
                                      current_price=.10, avg_cost=12.74, market_value=-20,
                                      unrealized_pnl=5.48, unrealized_pnl_pct=21.506,
                                      bid=.08, ask=.12, last=None))
        self.assertEqual(values[1], "卖方持仓\n2 张")
        self.assertEqual(values[2], "0.127")
        self.assertEqual(values[4], "—")
        self.assertEqual(values[6:9], ("+5.48", "+21.51%", "-20.00"))
        empty = position_values(dict(quantity=1))
        self.assertEqual(empty[6:9], ("—", "—", "—"))

    def test_live_render_preserves_selected_contract_without_recreating_row(self):
        ui = controller()
        ui.position_summary, ui.position_detail = Variable(""), Variable("")
        positions = [dict(conid=71, quantity=2, entry_cost=25.48, unrealized_pnl=5.48),
                     dict(conid=72, quantity=1, entry_cost=10, unrealized_pnl=None)]
        ui.render_positions(positions)
        ui.pos_tree.selection_set("71")
        ui.pos_tree.deleted.clear()
        ui.render_positions(positions)
        self.assertEqual(ui.pos_tree.selection(), ("71",))
        self.assertEqual(ui.pos_tree.deleted, [])
        self.assertIn("1 个合约待估值", ui.position_summary.get())
        self.assertIn("+5.48", ui.position_summary.get())
        ui.render_positions([dict(conid=71, quantity=1)])
        self.assertIn("已知成本/基准 $—", ui.position_summary.get())
        self.assertNotIn("$0.00", ui.position_summary.get())

    def test_context_menu_never_reuses_selection_for_empty_space(self):
        ui = close_controller()
        ui.pos_tree.identify_row = Mock(return_value="")
        ui.position_menu = Mock()
        event = SimpleNamespace(y=300, x_root=1, y_root=2)
        ui.position_context_menu(event)
        ui.position_menu.tk_popup.assert_not_called()
        ui.pos_tree.identify_row.return_value = "71"
        ui.position_context_menu(event)
        ui.position_menu.tk_popup.assert_called_once_with(1, 2)

    def test_menu_reads_availability_on_post_and_is_untouched_during_refresh(self):
        ui = close_controller()
        ui.position_close_dialog = None
        for key in ("position_add_btn", "position_limit_btn", "position_protect_btn", "cost_stop_btn", "position_refresh_btn"):
            setattr(ui, key, Mock())
        ui.close_buttons = []
        ui.position_menu = Mock()
        ui.position_menu.entrycget.return_value = "normal"
        ui.position_add_btn.cget.return_value = "disabled"
        ui.prepare_position_menu()
        ui.position_menu.entryconfigure.assert_called_once_with(0, state="disabled")
        ui.position_menu.reset_mock()
        for ready, pending in ((True, False), (True, True), (False, False), (True, False)):
            ui.update_position_controls(ready, pending)
        ui.position_menu.entryconfigure.assert_not_called()
        ui.position_menu.entrycget.return_value = "disabled"
        ui.position_add_btn.cget.return_value = "normal"
        ui.prepare_position_menu()
        ui.position_menu.entryconfigure.assert_called_once_with(0, state="normal")

    def test_quote_tick_updates_row_without_another_selection_event(self):
        ui = close_controller()
        rows = [dict(conid=71, quantity=2, bid=.10, ask=.14)]
        ui.render_positions(rows)
        ui.pos_tree.selection_set = Mock(wraps=ui.pos_tree.selection_set)
        ui.pos_tree.move = Mock(wraps=ui.pos_tree.move)
        ui.render_positions([{**rows[0], "bid": .11}])
        ui.pos_tree.selection_set.assert_not_called()
        ui.pos_tree.move.assert_not_called()
        self.assertEqual(ui.pos_tree.selection(), ("71",))
        self.assertIn("0.110 / 0.140", ui.pos_tree.rows["71"]["values"])

    def test_blank_limit_requests_seed_and_retains_manual_edit(self):
        ui = close_controller()
        ui.request_position_close()
        ui.submit.assert_called_once_with("preview_close", conid=71, percent=100,
                                           order_mode="LMT", limit_price=None)
        ui.render_position_close(preview())
        state = ui.position_close_dialog
        self.assertEqual(state["limit"].get(), "0.12")
        self.assertEqual(state["fingerprint"], ui.close_form_fingerprint())
        state["limit"].set("0.15")
        ui.position_close_changed()
        self.assertIsNone(state["preview"])
        ui.render_position_close(preview(limit_price=.15, limit_seeded=False))
        self.assertEqual(state["limit"].get(), "0.15")

    def test_mode_or_ticket_changes_reject_old_preview(self):
        ui = close_controller()
        context = dict(session=ui.session_epoch, close_form=ui.close_form_fingerprint())
        self.assertTrue(ui.context_is_current("preview_close", context))
        ui.position_close_dialog["mode"].set("市价")
        self.assertFalse(ui.context_is_current("preview_close", context))
        ui.position_close_dialog["mode"].set("限价")
        ui.position_close_dialog["id"] = "another-ticket"
        self.assertFalse(ui.context_is_current("preview_close", context))

    def test_submit_consumes_ticket_before_enqueue_and_double_click_is_noop(self):
        ui = close_controller()
        ui.render_position_close(preview())
        ui.submit_position_close()
        self.assertIsNone(ui.position_close_dialog["preview"])
        ui.submit_position_close()
        ui.mutate.assert_called_once_with("close_confirm", token="frozen-close")

    def test_expired_or_changed_position_does_not_send(self):
        for reason in ("expiry", "quantity", "form", "pending", "disconnected"):
            with self.subTest(reason=reason):
                ui = close_controller()
                ui.render_position_close(preview(created_at=time.time() - 11 if reason == "expiry" else time.time()))
                if reason == "quantity":
                    ui.positions_by_id["71"]["quantity"] = 1
                elif reason == "form":
                    ui.position_close_dialog["limit"].set(".25")
                elif reason == "pending":
                    ui.has_pending = Mock(return_value=True)
                elif reason == "disconnected":
                    ui.connected = False
                ui.submit_position_close()
                ui.mutate.assert_not_called()
                self.assertIsNone(ui.position_close_dialog["preview"])

    def test_covered_stop_enables_add_and_selected_quick_close(self):
        ui = close_controller()
        ui.financial_ready = True
        ui.position_close_dialog = None
        ui.position_add_btn, ui.position_limit_btn, ui.position_protect_btn = Widget(), Widget(), Widget()
        ui.cost_stop_btn, ui.position_refresh_btn = Widget(), Widget()
        ui.close_buttons = [Widget()]
        ui.positions_by_id["71"].update(close_pending=True, can_add_with_stop=True, protection_id=70)
        ui.update_position_controls(True, False)
        self.assertEqual(ui.position_add_btn.options["state"], "normal")
        self.assertEqual(ui.close_buttons[0].options["state"], "normal")
        ui.close_selected(100)
        ui.mutate.assert_called_once_with("close", conid=71, percent=100, protection_id=70)

    def test_one_times_cost_button_dispatches_directly_after_an_add(self):
        ui = close_controller()
        ui.position_close_dialog = None
        ui.position_limit_btn, ui.position_protect_btn, ui.position_refresh_btn = Widget(), Widget(), Widget()
        ui.close_buttons = []
        ui.cost_stop_btn, ui.stop_multiple = Widget(), Variable("1")
        ui.positions_by_id["71"].update(quantity=6, close_pending=True, protection_id=70,
                                        active_exit_orders=[dict(order_id=70, remaining=4)])
        ui.update_position_controls(True, False)
        self.assertEqual(ui.cost_stop_btn.options, {"text": "一键保本损", "state": "normal"})
        ui.submit_selected_close_order = Mock(side_effect=AssertionError("unexpected confirmation"))
        ui.request_cost_stop()
        ui.mutate.assert_called_once_with("set_breakeven_stop", conid=71)
        ui.submit.assert_not_called()

    def test_close_pending_disables_short_and_long_actions_but_allows_read_refresh(self):
        ui = close_controller()
        ui.position_close_dialog = None
        ui.position_limit_btn, ui.position_protect_btn, ui.position_refresh_btn = Widget(), Widget(), Widget()
        ui.close_buttons = [Widget()]
        ui.positions_by_id["71"]["close_pending"] = True
        ui.cost_stop_btn = Widget()
        ui.update_position_controls(True, False)
        self.assertEqual(ui.position_limit_btn.options["state"], "disabled")
        self.assertEqual(ui.cost_stop_btn.options["state"], "disabled")
        self.assertEqual(ui.close_buttons[0].options["state"], "disabled")
        self.assertEqual(ui.position_refresh_btn.options["state"], "normal")


class HistoryUiTests(unittest.TestCase):
    def ui(self):
        ui = controller()
        ui.history_period, ui.history_timezone = Variable("今天"), Variable("北京时间")
        ui.history_tree, ui.history_summary, ui.history_coverage = Tree(), Variable(""), Variable("")
        return ui

    def test_filter_or_timezone_change_discards_late_history(self):
        ui = self.ui()
        context = dict(session=ui.session_epoch, history_form=ui.history_fingerprint())
        self.assertTrue(ui.context_is_current("execution_history", context))
        ui.history_period.set("最近 7 日")
        self.assertFalse(ui.context_is_current("execution_history", context))
        ui.history_period.set("今天")
        ui.history_timezone.set("纽约时间")
        self.assertFalse(ui.context_is_current("execution_history", context))

    def test_initial_history_download_and_refresh_failure_are_visible(self):
        ui = self.ui()
        ui.submit = Mock()
        ui.request_history()
        self.assertTrue(ui.submit.call_args.kwargs["refresh"])
        ui.request_history()
        self.assertFalse(ui.submit.call_args.kwargs["refresh"])
        ui.render_history(dict(rows=[], api_refresh_error="连接超时", api_history_note="API 不保证 30 日回补"))
        self.assertIn("连接超时", ui.history_coverage.get())
        self.assertIn("API 不保证 30 日回补", ui.history_coverage.get())

    def test_missing_pnl_never_becomes_zero_and_commission_not_subtracted_twice(self):
        ui = self.ui()
        base = dict(date="2026-11-19", time="01:02:03", timestamp="2026-11-18T17:02:03Z",
                    currency="USD", pnl_currency="USD", local_symbol="XSP C751", action="SELL", quantity=1, price=.13,
                    gross=13, commission=None, commission_currency="USD", source="IBKR")
        rows = [dict(base, exec_id="filled", realized_pnl=-1.2, commission=.74),
                dict(base, exec_id="waiting", realized_pnl=None)]
        ui.render_history(dict(rows=rows, start_date="2026-11-19", end_date="2026-11-19",
                               totals_by_currency={"USD": dict(realized_pnl=-1.2, pnl_complete=False)},
                               missing_pnl_count=1, missing_commission_count=1))
        records = list(ui.history_tree.rows.values())
        self.assertEqual(records[0]["values"][7], "-1.20 USD")
        self.assertEqual(records[1]["values"][7], "-1.20 USD")
        self.assertEqual(records[2]["values"][6:8], ("—", "—"))
        self.assertIn("已知盈亏小计 -1.20 USD", ui.history_summary.get())
        self.assertIn("1 笔缺少", ui.history_coverage.get())


if __name__ == "__main__":
    unittest.main()
