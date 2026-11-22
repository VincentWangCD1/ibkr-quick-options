"""Presentation changes must not alter contracts, selections or trading state."""
import copy
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from contract_display import option_label, option_parts, pnl_color, compact_exit
from positions_ui import position_values
from chain_view import OptionChainView, short_status, price_label, chain_columns
from panel_focus import PanelFocusMixin
from test_app_state import controller, Variable


class ContractDisplayTests(unittest.TestCase):
    def test_occ_put_is_readable_without_modifying_source(self):
        row = dict(local_symbol="XSP  261119P00711000", quantity=1)
        original = copy.deepcopy(row)
        self.assertEqual(option_label(row), "XSP · 2026-11-19 · 711 Put")
        self.assertEqual(option_label(row, True), "XSP · 2026-11-19\n711 Put")
        self.assertEqual(position_values(row)[1], "买方持仓\n1 张")
        self.assertEqual(row, original)

    def test_long_short_call_put_are_not_market_direction_labels(self):
        for right in ("C", "P"):
            for qty in (1, -2):
                row = dict(symbol="SPY", expiry="20261218", strike=600, right=right, quantity=qty)
                values = position_values(row)
                self.assertIn("Call" if right == "C" else "Put", values[0])
                self.assertEqual(values[1], "买方持仓\n1 张" if qty == 1 else "卖方持仓\n2 张")

    def test_fractional_strike_and_trading_class_preserved(self):
        self.assertEqual(option_label(dict(local_symbol="SPXW 261119C07312500", symbol="SPX")), "SPXW · 2026-11-19 · 7312.5 Call")
        self.assertEqual(option_parts(dict(local_symbol="261119P00711000", symbol="XSP"))[0], "XSP")

    def test_structured_fields_win_and_unknown_identity_is_not_invented(self):
        label = option_label(dict(local_symbol="XSP 261119P00711000", expiry="20261120", strike=712, right="C"))
        self.assertEqual(label, "XSP · 2026-11-20 · 712 Call")
        self.assertEqual(option_label(dict(local_symbol="UNKNOWN")), "UNKNOWN")
        self.assertEqual(option_label({}), "—")

    def test_red_profit_green_loss_unknown_neutral(self):
        self.assertEqual(pnl_color(3), "#be3045")
        self.assertEqual(pnl_color(-3), "#008653")
        self.assertEqual(pnl_color(None), pnl_color(float("nan")))
        self.assertNotIn(pnl_color(0), (pnl_color(1), pnl_color(-1)))

    def test_compact_exit_keeps_price_pending_state_and_full_original(self):
        row = dict(active_exit_orders=[dict(category="stop_loss", order_type="STP", target_price=.3, remaining=2, status_label="待确认")], stop_loss_summary="FULL DETAILS")
        self.assertEqual(compact_exit(row, "stop_loss"), "STP @0.3\n2张 · 待确认")
        self.assertEqual(row["stop_loss_summary"], "FULL DETAILS")
        self.assertEqual(compact_exit(row, "take_profit"), "未发现挂单")


def chain_widget():
    table = OptionChainView.__new__(OptionChainView)
    table.canvas = Mock()
    table.canvas.winfo_width.return_value = 1000
    table.canvas.winfo_height.return_value = 300
    table.canvas.canvasy.side_effect = lambda y: y
    table._ids, table._rows, table._indices = [], {}, {}
    table._selected, table._side = "", ""
    table.contract_rows, table.reference_meta = {}, {}
    table._schedule, table._update_detail = Mock(), Mock()
    return table


class ChainDisplayTests(unittest.TestCase):
    def test_left_center_right_mapping_is_stable_for_resizes(self):
        table = chain_widget()
        for width in (600, 1000, 1800, 2400):
            table.canvas.winfo_width.return_value = width
            left, right, _ = chain_columns(width)
            for x, column in ((left / 6, "#1"), (left / 2, "#2"), (left * 5 / 6, "#3"),
                              ((left + right) / 2, "#5"), (right + left / 6, "#6"), (right + left / 2, "#7"), (right + left * 5 / 6, "#8")):
                self.assertEqual(table.identify_column(x), column)
            self.assertEqual(table.identify_column(-1), "")
            self.assertEqual(table.identify_column(width), "")

    def test_stable_rows_and_quote_updates_do_not_touch_scroll(self):
        table = chain_widget()
        for key in ("a", "reference-price", "b"):
            table.insert("", "end", key, values=(key,))
        table.item("a", values=("updated",))
        table.move("a", "", 0)
        self.assertEqual(table.get_children(), ("a", "reference-price", "b"))
        self.assertEqual(table.item("a")["values"], ("updated",))
        table.canvas.yview.assert_not_called()
        table.move("reference-price", "", 2)
        self.assertEqual(table.get_children(), ("a", "b", "reference-price"))
        table.delete("a", "b")
        self.assertEqual(table.get_children(), ("reference-price",))

    def test_blank_area_and_scrolled_hit_testing(self):
        table = chain_widget()
        for key in ("a", "reference-price", "b"):
            table.insert("", "end", key)
        self.assertEqual(table.identify_row(table.row_height + 1), "reference-price")
        self.assertEqual(table.identify_row(200), "")
        self.assertEqual(table.identify_row(-1), "")
        table.canvas.canvasy.side_effect = lambda y: y + table.row_height * 2
        self.assertEqual(table.identify_row(10), "b")

    def test_call_and_put_selection_use_original_ids_and_never_trade(self):
        for col, side in (("#1", "C"), ("#3", "C"), ("#6", "P"), ("#8", "P")):
            ui = controller()
            ui.right = Variable("C")
            ui.chain_rows_by_id = {"row": dict(strike=711, call=dict(conid=10), put=dict(conid=20))}
            ui.chain_tree.identify_row = Mock(return_value="row")
            ui.chain_tree.identify_column = Mock(return_value=col)
            ui.reset_entry_prices, ui.changed, ui.request_preview, ui.submit = Mock(), Mock(), Mock(), Mock()
            ui.panel_focus_active, ui.restore_panel_focus = True, Mock()
            ui.select_chain_contract(SimpleNamespace(x=1, y=1))
            self.assertEqual(ui.right.get(), side)
            self.assertEqual(ui.selected_conids, {"C": 10, "P": 20})
            self.assertEqual(ui.exact_strike.get(), "711")
            ui.restore_panel_focus.assert_called_once()
            ui.submit.assert_not_called()

    def test_center_and_reference_double_click_do_not_select_contract(self):
        ui = controller()
        ui.chain_rows_by_id = {"row": dict(strike=711)}
        ui.chain_tree.identify_row = Mock(return_value="row")
        ui.chain_tree.identify_column = Mock(return_value="#5")
        ui.changed, ui.request_preview = Mock(), Mock()
        ui.select_chain_contract(SimpleNamespace(x=1, y=1))
        ui.changed.assert_not_called()
        ui.chain_tree.identify_row.return_value = "reference-price"
        ui.chain_tree.identify_column.return_value = "#1"
        ui.select_chain_contract(SimpleNamespace(x=1, y=1))
        ui.request_preview.assert_not_called()

    def test_missing_delayed_and_frozen_prices_stay_distinct(self):
        self.assertEqual(price_label(None), "—")
        self.assertEqual(price_label(.03), "0.03")
        self.assertEqual(price_label(1.2), "1.20")
        self.assertEqual(price_label(-1), "—")
        for kind, label in (("延迟行情", "延迟"), ("冻结", "冻结"), ("实时订阅", "实时"), ("实时订阅·盘口久未更新", "盘口较旧")):
            status = short_status(dict(quote_type=kind, bid_age=2, ask_age=12), "实值")
            self.assertIn(label, status)
            self.assertIn("买 2s / 卖 12s", status)


class FocusTests(unittest.TestCase):
    def ui(self, state="normal"):
        ui = PanelFocusMixin()
        ui.panel_focus_active = False
        ui.root = Mock()
        ui.root.state.return_value = state
        ui.root.geometry.return_value = "1220x930+50+60"
        ui.shutdown = Mock()
        ui.workspace = Mock()
        ui.workspace.grid_info.return_value = {"row": 5}
        ui.upper = [Mock() for _ in range(5)]
        for row, widget in enumerate(ui.upper):
            widget.grid_info.return_value = {"row": row}
        ui.workspace_outer = Mock()
        ui.workspace_outer.grid_slaves.return_value = ui.upper + [ui.workspace]
        ui.focus_button = Mock()
        ui.focus_pin = Mock()
        ui.focus_language = Mock()
        ui.focus_title = Variable("")
        ui.notebook = Mock()
        ui.notebook.select.return_value = "history"
        ui.notebook.tab.return_value = "成交记录 / 盈亏"
        return ui

    def test_focus_restores_existing_widgets_and_native_close_returns(self):
        ui = self.ui()
        ui.toggle_panel_focus()
        for widget in ui.upper:
            widget.grid_remove.assert_called_once()
        ui.workspace.grid_remove.assert_not_called()
        ui.root.protocol.assert_called_with("WM_DELETE_WINDOW", ui.restore_panel_focus)
        self.assertTrue(ui.panel_focus_active)
        self.assertIn("最大化查看", ui.focus_title.get())
        ui.restore_panel_focus()
        for widget in ui.upper:
            widget.grid.assert_called_once_with()
        ui.root.geometry.assert_called_with("1220x930+50+60")
        ui.root.state.assert_called_with("normal")
        ui.root.protocol.assert_called_with("WM_DELETE_WINDOW", ui.shutdown)
        ui.shutdown.assert_not_called()
        self.assertFalse(ui.panel_focus_active)
        ui.notebook.destroy.assert_not_called()

    def test_already_maximized_window_remains_maximized_on_restore(self):
        ui = self.ui("zoomed")
        ui.toggle_panel_focus()
        ui.root.geometry.reset_mock()
        ui.toggle_panel_focus()
        ui.root.state.assert_called_with("zoomed")
        ui.root.geometry.assert_not_called()

    def test_escape_in_child_dialog_does_not_close_or_restore_work_area(self):
        ui = self.ui()
        ui.toggle_panel_focus()
        widget = Mock()
        widget.winfo_toplevel.return_value = object()
        ui.focus_escape(SimpleNamespace(widget=widget))
        self.assertTrue(ui.panel_focus_active)
        widget.winfo_toplevel.return_value = ui.root
        self.assertEqual(ui.focus_escape(SimpleNamespace(widget=widget)), "break")
        self.assertFalse(ui.panel_focus_active)


if __name__ == "__main__":
    unittest.main()
