import unittest
from history_groups import order_rows, closing_trades, trade_recap
from exit_orders import cost_stop_quote, is_cost_protection
from ui_state import account_day_label


def fill(n, side, quantity, price, fee=0, date="2026-01-02", conid=7):
    return dict(timestamp=f"{date}T12:00:0{n}+00:00", date=date, exec_id=str(n), conid=conid,
                action=side, quantity=quantity, price=price, multiplier=100, currency="USD",
                commission_currency="USD", commission=fee, realized_pnl=None, sec_type="OPT",
                symbol="DEMO", expiry="20260102", strike=100, right="C")


class HistoryAndStops(unittest.TestCase):
    def test_weighted_order_price_and_separate_orders_seconds_apart(self):
        rows = [dict(fill(1, "BUY", 3, 1, date="2026-01-01"), perm_id=25),
                dict(fill(2, "SELL", 1, 1.15), perm_id=26), dict(fill(3, "SELL", 2, 1.6), perm_id=26),
                dict(fill(4, "BUY", 1, 1.5), perm_id=27)]
        orders = list(order_rows(rows).values())
        self.assertEqual(len(orders), 3)
        self.assertEqual(orders[1]["price"], 1.45)
        self.assertEqual([r["price"] for r in orders[1]["fills"]], [1.6, 1.15])
        results = closing_trades(rows, rows[1:], "Asia/Shanghai")
        recap = trade_recap(orders[1]["fills"], results)
        self.assertEqual((recap["pnl"], recap["percent"], recap["quantity"]), (135, 45, 3))
        self.assertEqual((recap["open_first"], recap["close_last"]), ("2026-01-01 20:00:01", "2026-01-02 20:00:03"))
        self.assertIsNone(trade_recap(orders[2]["fills"], results))

    def test_partial_close_allocates_commissions_once(self):
        rows = [fill(1, "BUY", 3, 1, 3), fill(2, "SELL", 2, 1.2, 2)]
        r = trade_recap(rows[1:], closing_trades(rows, rows, "America/New_York"))
        self.assertAlmostEqual(r["percent"], 100 * 36/202)
        self.assertEqual((r["pnl"], r["open_first"]), (36, "2026-01-02 07:00:01"))

    def test_short_and_unknown_fee(self):
        rows = [fill(1, "SELL", 2, 2), fill(2, "BUY", 1, 1, None)]
        r = trade_recap(rows[1:], closing_trades(rows, rows, "Asia/Shanghai"))
        self.assertEqual((r["percent"], r["quantity"], r["before_fees"], r["action"]), (50, 1, True, "BUY"))

    def test_no_invented_entry_or_percent_for_unmatched_known_close(self):
        rows = [dict(fill(1, "SELL", 1, 2), realized_pnl=20), fill(2, "BUY", 1, 1)]
        results = closing_trades(rows, rows, "Asia/Shanghai")
        r = trade_recap(rows[:1], results)
        self.assertEqual(r["pnl"], 20)
        self.assertIsNone(r["percent"])
        self.assertNotIn("open_first", r)
        self.assertIsNone(trade_recap(rows[1:], results))

    def test_unknown_and_conflicting_order_ids_stay_separate(self):
        rows = [dict(fill(n, "BUY", 1, 2), perm_id=0) for n in (1, 2, 3)]
        self.assertEqual(len(order_rows(rows)), 3)
        rows[0].update(source="FLEX", order_id="15", perm_id=99)
        rows[1].update(source="FLEX", order_id="15")
        rows[2].update(source="API", perm_id=99)
        self.assertEqual(len(order_rows(rows)), 1)
        rows[2].update(source="FLEX", perm_id=100, order_id="15")
        self.assertEqual(len(order_rows(rows)), 3)

    def test_explicit_reversal_only_shares_its_closed_quantity(self):
        rows = [fill(1, "BUY", 1, 1), dict(fill(2, "SELL", 3, 2), open_close="C;O", realized_pnl=100),
                dict(fill(3, "BUY", 2, 1.5), open_close="C", realized_pnl=100)]
        results = closing_trades(rows, rows, "Asia/Shanghai")
        first = trade_recap(rows[1:2], results)
        second = trade_recap(rows[2:], results)
        self.assertEqual((first["quantity"], first["pnl"]), (1, 100))
        self.assertEqual((second["quantity"], second["open_price"], second["pnl"]), (2, 2, 100))

    def test_reopening_does_not_change_previous_close(self):
        rows = [fill(1, "BUY", 1, 2.00, .50), dict(fill(2, "SELL", 1, 2.20, .50), realized_pnl=19.00),
                fill(3, "BUY", 1, 1.25, .50)]
        r = trade_recap(rows[1:2], closing_trades(rows, rows, "Asia/Shanghai"))
        self.assertEqual((r["open_price"], r["close_price"], r["pnl"]), (2.00, 2.20, 19.00))
        self.assertAlmostEqual(r["percent"], 100 * 19.00 / 200.50)

    def test_account_day_percent_uses_starting_equity_and_currency(self):
        f = dict(base_currency="USD", net_liquidation_currency="USD", net_liquidation=750, daily_pnl=-250)
        self.assertIn("-25.00%", account_day_label(f))
        self.assertNotIn("%", account_day_label(dict(f, net_liquidation_currency="EUR")))
        self.assertNotIn("%", account_day_label(dict(f, daily_pnl=float("nan"))))

    def test_seven_loss_triggers_and_legacy_break_even(self):
        for loss in range(10, 71, 10):
            value = cost_stop_quote(100, 100, 2, 1.20, [(0,.01)], (100-loss)/100)
            self.assertEqual(value["stop_price"], (100-loss)/100)
        with self.assertRaisesRegex(ValueError, "不足一个有效报价档"):
            cost_stop_quote(5, 100, 1, .07, [(0, .01)], .9)
        self.assertTrue(is_cost_protection("QOPT1:" + "a"*16, 1.01, 100, "100"))
        self.assertFalse(is_cost_protection("QOPT1:" + "a"*16, .90, 100, "100"))
        self.assertFalse(is_cost_protection("MANUAL", 1.20, 100, "100"))

class OrderRecapDisplay(unittest.TestCase):
    def setUp(self):
        self.rows = [dict(fill(1, "BUY", 2, 1), time="20:00:01", perm_id=1),
                     dict(fill(2, "SELL", 1, 1.05), time="20:00:02", perm_id=2),
                     dict(fill(3, "SELL", 1, 1.10), time="20:00:03", perm_id=2)]
        self.results = closing_trades(self.rows, self.rows, "Asia/Shanghai")

    def test_phone_image_contains_only_trade_amounts_and_selected_zone(self):
        from unittest.mock import patch
        from PIL import ImageDraw
        from share_card import render_share_card
        from quickoptions_version import APP_VERSION
        recap = trade_recap(self.rows[1:], self.results)
        with patch.object(ImageDraw.ImageDraw, "text", autospec=True) as painter:
            image = render_share_card(self.rows[2], recap, language="en")
            labels = [call.args[2] for call in painter.call_args_list]
            self.assertEqual(image.size, (1080, 1920))
            for label in (f"Quick Options V{APP_VERSION}", "Buy  2 contracts", "Sell  2 contracts",
                          "+$15.00", "$1.00", "$1.075", "2026-01-02 20:00:01",
                          "2026-01-02 20:00:02", "to  2026-01-02 20:00:03", "Beijing time", "VincentWangCD"):
                self.assertIn(label, labels)
            draws = {call.args[2]: call for call in painter.call_args_list}
            self.assertGreater(draws["+7.50%"].kwargs["font"].size, draws["+$15.00"].kwargs["font"].size)
            self.assertLess(draws["+7.50%"].args[1][1], draws["+$15.00"].args[1][1])
            painter.reset_mock()
            render_share_card(self.rows[2], recap, private=True)
            labels = [call.args[2] for call in painter.call_args_list]
            self.assertIn("+7.50%", labels)
            self.assertFalse(any("$" in label for label in labels))
            self.assertFalse(any("张" in label for label in labels))

    def test_expiry_and_full_fill_time_are_visible_on_collapsed_and_expanded_cards(self):
        from tkinter import Tk, TclError
        from history_cards import HistoryCards
        try:
            root = Tk()
        except TclError as exc:
            self.skipTest(str(exc))
        self.addCleanup(root.destroy)
        root.withdraw()
        cards = HistoryCards(root)
        cards.canvas.configure(height=600)
        cards.set_data([dict(row, expiry="20261016") for row in self.rows], self.results)
        group = next(row for kind, row in cards.records if kind == "order" and len(row["fills"]) == 2)
        from unittest.mock import patch
        for expanded in (False, True):
            if expanded:
                cards.toggle(group["key"])
            with patch.object(cards.canvas, "winfo_height", return_value=600):
                cards.draw()
            labels = [cards.canvas.itemcget(item, "text") for item in cards.canvas.find_all()
                      if cards.canvas.type(item) == "text"]
            self.assertIn("到期 2026-10-16  100 Call", labels)
            self.assertIn("成交时间 2026-01-02 20:00:03", labels)
            self.assertEqual("成交时间 2026-01-02 20:00:02" in labels, expanded)

    def test_share_eye_and_saved_png_match_in_both_languages(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        import tkinter as tk
        from unittest.mock import patch
        from PIL import Image
        import app
        import share_card
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(str(exc))
        root.withdraw()
        with TemporaryDirectory() as temp, patch.object(app, "BASE", Path(temp)), patch.object(app, "Worker"), \
                patch.object(app.App, "poll"), patch("history_ui.PointerPopup"):
            ui = app.App(root)
            try:
                self.assertEqual(list(app.MODES.values()), ["LIVE", "PAPER", "DEMO"])
                self.assertEqual((app.MODES[ui.mode.get()], ui.port.get()), ("LIVE", "7496"))
                self.assertFalse(ui.connected)
                self.assertTrue(ui.inbox.empty())
                ui.mode.set("IBKR 模拟盘")
                ui.mode_changed()
                self.assertEqual(ui.port.get(), "7497")
                group = self.rows[2]
                recap = trade_recap(self.rows[1:], self.results)
                def buttons():
                    controls = ui.share_window.winfo_children()[-1]
                    return controls.winfo_children()
                ui.share_history_group(group, recap)
                eye, save = buttons()
                visible = ui.share_window.card_image.tobytes()
                eye.invoke()
                hidden = ui.share_window.card_image.tobytes()
                self.assertNotEqual(visible, hidden)
                eye.invoke()
                self.assertEqual(ui.share_window.card_image.tobytes(), visible)
                eye.invoke()
                ui.toggle_language()
                self.assertEqual(eye.cget("text"), "Show amounts and quantities")
                expected = share_card.render_share_card(group, recap, private=True, language="en").tobytes()
                png = Path(temp) / "card.png"
                with patch("history_ui.filedialog.asksaveasfilename", return_value=str(png)):
                    save.invoke()
                with Image.open(png) as actual:
                    self.assertEqual(actual.tobytes(), expected)
                ui.toggle_bean_mode()
                ui.share_history_group(group, recap)
                eye, _ = buttons()
                self.assertTrue(eye.instate(["disabled"]))
                self.assertEqual(ui.share_window.card_image.tobytes(), expected)
            finally:
                ui.shutdown()

    def test_cards_expand_and_share_parent_or_individual_fill(self):
        import tkinter as tk
        from types import SimpleNamespace
        from history_cards import HistoryCards
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(str(exc))
        self.addCleanup(root.destroy)
        root.withdraw()
        shares = []
        cards = HistoryCards(root, lambda row, recap: shares.append(recap))
        cards.set_data(self.rows, self.results)
        self.assertEqual([k for k, _ in cards.records], ["day", "order", "order"])
        # Tk may quantize the scroll origin to the configured yscrollincrement.
        def click(index, x, offset):
            y = cards.tops[index] - cards.canvas.canvasy(0) + offset
            cards.click(SimpleNamespace(x=x, y=y))
        click(1, cards.canvas.winfo_width(), 90)
        self.assertEqual(shares[-1]["pnl"], 15)
        click(1, 0, 30)
        self.assertEqual([k for k, _ in cards.records], ["day", "order", "fill", "fill", "order"])
        click(2, cards.canvas.winfo_width(), 90)
        self.assertEqual(shares[-1]["pnl"], 10)
        cards.set_data(self.rows, self.results)
        self.assertEqual(cards.records[2][0], "fill")
        click(1, 0, 30)
        self.assertEqual(len(cards.records), 3)
