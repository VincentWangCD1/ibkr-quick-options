"""Headless controller checks for entry modes and pending-order editing."""
import time
import unittest
from unittest.mock import patch

from app import App, ORDER_REQUESTS, READ_REQUESTS
from ui_state import build_entry_fields, modify_order_confirmation
from tests.test_app_state import controller, Variable


def order_ui():
    ui = controller()
    order = dict(order_id=71, client_id=31, local_symbol="ORIGINAL CONTRACT", action="BUY",
                 order_type="LMT", limit_price=1.2, quantity=5, filled=2, remaining=3,
                 status="Submitted", avg_fill_price=1.1)
    ui.orders_by_id = {"31|71": order}
    ui.order_tree.selection_set("31|71")
    return ui


def amendment():
    return dict(token="single-use", order_id=71, local_symbol="ORIGINAL CONTRACT", action="BUY",
                old_order_type="LMT", old_limit_price=1.2, order_type="LMT", limit_price=1.5,
                quantity=5, filled=2, remaining=3, price_mode="MID", method="amend",
                created_at=time.time(), account="DEMO")


class EntryAndAmendmentUITests(unittest.TestCase):
    def test_mode_input_ignores_hidden_price_and_validates_visible_price(self):
        for mode in ("MKT", "MID"):
            self.assertIsNone(build_entry_fields(mode, "stale invalid value")["limit_price"])
        for price in ("", "nan", "inf", "-1", "0"):
            with self.assertRaises(ValueError):
                build_entry_fields("LMT", price)
        self.assertEqual(build_entry_fields("LMT", "1.25", "floor"),
                         dict(order_mode="LMT", limit_price=1.25, rounding_mode="floor"))

    def test_entry_settings_and_fingerprint_include_new_fields(self):
        ui = controller()
        for name, value in (("right", "C"), ("percent", 50), ("slippage", 5), ("fee", 1), ("min_fee", 1), ("cap", 100)):
            setattr(ui, name, Variable(value))
        before = App.fingerprint(ui)
        self.assertEqual(ui.settings()["rounding_mode"], "nearest")
        ui.entry_mode.set("中间价（固定限价）")
        self.assertNotEqual(before, App.fingerprint(ui))
        ui.rounding_mode.set("向下取整")
        self.assertEqual(ui.settings()["order_mode"], "MID")
        self.assertEqual(ui.settings()["rounding_mode"], "floor")

    def test_edit_uses_selected_original_order_and_coalesces_double_click(self):
        ui = order_ui()
        ui.active_symbol = "DIFFERENT SYMBOL"
        ui.request_modify()
        ui.request_modify()
        self.assertEqual(tuple(ui.pending_requests), ("preview_modify",))
        self.assertEqual(ui.pending_requests["preview_modify"]["args"],
                         dict(order_id=71, order_mode="LMT", limit_price=1.5))
        self.assertIn("preview_modify", READ_REQUESTS)
        self.assertTrue({"modify_order", "cancel_order"} <= ORDER_REQUESTS)

    def test_changed_selection_or_edit_invalidates_returned_preview(self):
        ui = order_ui()
        ui.request_modify()
        context = ui.pending_requests["preview_modify"]["context"]
        self.assertTrue(ui.context_is_current("preview_modify", context))
        ui.modify_limit.set("1.6")
        self.assertFalse(ui.context_is_current("preview_modify", context))
        ui.modify_limit.set("1.5")
        ui.order_tree.selection_set("missing")
        self.assertFalse(ui.context_is_current("preview_modify", context))

    def test_confirmation_submits_only_token_and_not_new_quantity(self):
        ui = order_ui()
        context = dict(ui.context(), modify_form=ui.modify_fingerprint())
        with patch("app.messagebox.askyesno", return_value=True) as ask:
            ui.confirm_modify(amendment(), context)
        self.assertEqual(ui.pending_requests["modify_order"]["args"], {"token": "single-use"})
        self.assertIn("当前剩余：3 张", ask.call_args.args[1])
        self.assertIn("固定限价", ask.call_args.args[1])
        self.assertFalse(ui.modify_confirmation_open)

    def test_confirmation_cancel_expiry_or_account_switch_never_mutates(self):
        for case in ("cancel", "expired", "session"):
            with self.subTest(case=case):
                ui = order_ui()
                context = dict(ui.context(), modify_form=ui.modify_fingerprint())
                preview = amendment()
                if case == "expired":
                    preview["created_at"] -= 20
                def answer(*args, **kwargs):
                    if case == "session":
                        ui.session_epoch += 1
                    return case != "cancel"
                with patch("app.messagebox.askyesno", side_effect=answer):
                    ui.confirm_modify(preview, context)
                self.assertFalse(ui.pending_requests)
                self.assertTrue(ui.inbox.empty())

    def test_filled_canceling_or_disconnected_order_cannot_edit(self):
        for status in ("Filled", "Cancelled", "PendingCancel", "Inactive"):
            ui = order_ui()
            ui.selected_order()["status"] = status
            ui.request_modify()
            ui.cancel_selected_order()
            self.assertFalse(ui.pending_requests)
        ui = order_ui()
        ui.selected_order()["remaining"] = 0
        self.assertFalse(ui.order_edit_ready())

    def test_demo_status_and_conditional_cancel_have_separate_eligibility(self):
        ui = order_ui()
        ui.selected_order()["status"] = "Submitted (离线演示)"
        self.assertTrue(ui.order_edit_ready())
        ui.selected_order()["order_type"] = "STP"
        self.assertFalse(ui.order_edit_ready())
        self.assertTrue(ui.order_edit_ready(cancel=True))
        ui.cancel_selected_order()
        self.assertEqual(ui.pending_requests["cancel_order"]["args"], {"order_id": 71})

    def test_snapshot_preserves_selection_and_user_typed_limit(self):
        ui = order_ui()
        ui.order_selected()
        ui.modify_limit.set("1.7")
        order = dict(ui.selected_order(), filled=3, remaining=2)
        ui.render_snapshot(dict(connected=True, mode="DEMO", account="DEMO", orders=[order],
                                positions=[], financial=dict(ready=True)))
        self.assertEqual(ui.order_tree.selection(), ("31|71",))
        self.assertEqual(ui.modify_limit.get(), "1.7")
        self.assertIn("剩余 2 张", ui.order_caption.get())
        ui.cancel_selected_order()
        self.assertEqual(ui.pending_requests["cancel_order"]["args"], {"order_id": 71})

    def test_type_switch_confirmation_explains_cancel_replace(self):
        preview = amendment()
        preview.update(method="replace", order_type="MKT", limit_price=None, price_mode="MKT")
        text = modify_order_confirmation(preview)
        self.assertIn("收到撤单确认后", text)
        self.assertIn("剩余数量", text)
        self.assertIn("原单不会恢复", text)

    def test_amendment_confirmation_discloses_reserves_and_reconnect_defaults(self):
        preview = amendment()
        preview.update(order_type="MKT", slippage_pct=10, commission_per_contract=3,
                       min_commission=7, reserve_settings_source="original")
        text = modify_order_confirmation(preview)
        self.assertIn("$3/张，最低 $7", text)
        self.assertIn("10%", text)
        self.assertNotIn("默认参数", text)
        preview["reserve_settings_source"] = "defaults"
        self.assertIn("默认参数", modify_order_confirmation(preview))


if __name__ == "__main__":
    unittest.main()
