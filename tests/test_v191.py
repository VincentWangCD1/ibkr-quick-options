"""Compact controls do not mutate trading settings or selected contracts."""
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from entry_ui import EntryUiMixin
from panel_focus import PanelFocusMixin
from tests.test_v190 import chain_widget
from tests.test_app_state import Variable


class CompactLayoutTests(unittest.TestCase):
    def entry(self):
        ui = EntryUiMixin()
        ui.entry_mode = Variable("市价")
        ui.entry_price_frames = [Mock(), Mock()]
        ui.side_buttons = {side: Mock() for side in ("C", "P")}
        ui.entry_follow_frame, ui.entry_match_label = Mock(), Mock()
        ui.entry_details_expanded = False
        return ui

    def test_limit_inputs_visible_only_for_limit_orders(self):
        ui = self.entry()
        ui.update_entry_visibility()
        for widget in ui.entry_price_frames:
            widget.pack_forget.assert_called_once()
        ui.entry_mode.set("限价")
        ui.update_entry_visibility()
        for side, widget in zip(("C", "P"), ui.entry_price_frames):
            self.assertIs(widget.pack.call_args.kwargs["before"], ui.side_buttons[side])
        self.assertEqual(ui.entry_mode.get(), "限价")

    def test_auto_mid_parameters_reopen_with_values_untouched(self):
        ui = self.entry()
        ui.entry_mode.set("中间价（自动追价）")
        ui.mid_interval, ui.mid_duration, ui.mid_drift = Variable(7), Variable(600), Variable(5)
        ui.entry_detail_widgets, ui.entry_explanation, ui.entry_details_button = [Mock(), Mock()], Mock(), Mock()
        ui.toggle_entry_details()
        self.assertTrue(ui.entry_details_expanded)
        ui.entry_follow_frame.pack.assert_called_once()
        ui.toggle_entry_details()
        self.assertFalse(ui.entry_details_expanded)
        self.assertEqual((ui.mid_interval.get(), ui.mid_duration.get(), ui.mid_drift.get()), (7, 600, 5))

    def test_connection_disclosure_does_not_map_widgets_in_focus_mode(self):
        ui = PanelFocusMixin()
        ui.connection_panel, ui.connection_toggle = Mock(), Mock()
        ui.panel_focus_active = True
        ui.show_connection_details(True)
        ui.connection_panel.grid.assert_not_called()
        ui.panel_focus_active = False
        ui.show_connection_details(False)
        ui.connection_panel.grid_remove.assert_called_once()
        ui.show_connection_details(True)
        ui.connection_panel.grid.assert_called_once()

    def test_hover_shows_quote_status_without_changing_selection(self):
        table = chain_widget()
        table._selected, table._side = "picked", "P"
        table.contract_rows = {"a": dict(strike=711, call=dict(quote_type="延迟行情", bid_age=9, ask_age=10), put=dict(quote_type="冻结"))}
        table.detail_var = Variable("")
        table.insert("", "end", "a")
        table._hover(SimpleNamespace(y=4))
        self.assertIn("延迟行情", table.detail_var.get())
        self.assertIn("冻结", table.detail_var.get())
        self.assertEqual((table._selected, table._side), ("picked", "P"))


if __name__ == "__main__":
    unittest.main()
