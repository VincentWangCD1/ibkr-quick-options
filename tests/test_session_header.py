import unittest

from app import READ_REQUESTS
from test_app_state import controller, Variable
from unittest.mock import Mock
from contract_display import pnl_color
from ui_state import last_session_label


class SessionHeaderTests(unittest.TestCase):
    def test_keeps_currencies_separate_and_marks_partial_data(self):
        report = dict(date="2026-11-20", fills=3, missing_pnl_count=1,
                      by_currency={"USD": -12.3, "CAD": None, "EUR": 0.0})
        text = last_session_label(report)
        for expected in ("2026-11-20", "-12.30 USD", "+0.00 EUR", "— CAD", "已知小计"):
            self.assertIn(expected, text)

    def test_daily_header_tracks_currency_sign_and_disconnect(self):
        ui = controller()
        ui.daily_account, ui.daily_amount_label = Variable(""), Mock()
        ui.green_profit = ui.bean_mode = False
        ui.snapshot_data = {"financial": {"daily_pnl": -15, "base_currency": "USD"}}
        ui.update_daily_pnl()
        self.assertIn("-15.00 USD", ui.daily_account.get())
        ui.daily_amount_label.configure.assert_called_with(foreground=pnl_color(-1))
        ui.green_profit = True
        ui.update_daily_pnl()
        ui.daily_amount_label.configure.assert_called_with(foreground=pnl_color(1))
        ui.bean_mode = True
        ui.update_daily_pnl()
        ui.daily_amount_label.configure.assert_called_with(foreground=pnl_color(None))
        ui.clear_session_view("断开")
        self.assertEqual(ui.daily_account.get(), "—")
        self.assertNotIn("last_session_pnl", READ_REQUESTS)
