"""Short-cover controller checks without Tk, a window, or a broker."""
import unittest
from unittest.mock import patch

from app import App
from tests.test_app_state import controller, Widget


def short_controller():
    ui = controller()
    ui.positions_by_id = {"42": dict(conid=42, quantity=-3, local_symbol="SHORT CONTRACT")}
    ui.pos_tree.selection_set("42")
    ui.preview_data = None
    ui.connect_controls = []
    ui.close_buttons = [Widget(), Widget()]
    for name in ("connect_btn", "disconnect_btn", "funds_btn", "depth_box", "preview_btn",
                 "cancel_btn", "flatten_btn", "chain_refresh_btn", "chain_width_box",
                 "chain_atm_btn", "chain_prev_btn", "chain_next_btn"):
        setattr(ui, name, Widget())
    return ui


class ShortCoverUiTests(unittest.TestCase):
    def test_custom_sell_panel_rejects_short_before_confirmation(self):
        ui = short_controller()
        with patch("app.messagebox.askyesno") as ask:
            ui.submit_selected_close_order()
        ask.assert_not_called()
        self.assertFalse(ui.pending_requests)
        self.assertIn("快捷平仓", ui.status.get())
        self.assertIn("买回", ui.status.get())

    def test_short_selection_disables_advanced_sells_but_keeps_quick_cover(self):
        ui = short_controller()
        App.update_enabled(ui)
        self.assertEqual(ui.close_order_btn.options["state"], "disabled")
        self.assertIn("买回", ui.close_order_btn.options["text"])
        self.assertEqual(ui.close_type_box.options["state"], "disabled")
        self.assertTrue(all(button.options["state"] == "normal" for button in ui.close_buttons))
        ui.positions_by_id["42"]["quantity"] = 3
        App.update_enabled(ui)
        self.assertEqual(ui.close_order_btn.options["state"], "normal")
        self.assertEqual(ui.close_order_btn.options["text"], "提交所选卖单")

    def test_quick_cover_passes_original_short_contract_to_broker(self):
        ui = short_controller()
        ui.close_selected(50)
        self.assertEqual(ui.pending_requests["close"]["args"], {"conid": 42, "percent": 50})


if __name__ == "__main__":
    unittest.main()
