"""Offline presentation regressions. No broker connection required."""
from pathlib import Path
from tempfile import TemporaryDirectory
import tkinter as tk
import unittest

import localized_widgets as widgets
from ui_language import Language, english


class LanguageTextTests(unittest.TestCase):
    def test_embedded_trade_terms_keep_identifiers_prices_and_quantity(self):
        self.assertEqual(english("IBKR 快捷期权 V1.13.0"), "IBKR Quick Options V1.13.0")
        self.assertEqual(english("作者：VincentWangCD"), "Author: VincentWangCD")
        message = "账户：DU123\n合约：XSP   261120C00711000\n买Call（做多） 2 张\n限价：0.07"
        self.assertEqual(english(message), "Account: DU123\nContract: XSP   261120C00711000\n"
                         "Buy call (bullish) 2 contracts\nLimit: 0.07")

    def test_persistence_and_unknown_preference(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "data" / "ui_language.txt"
            first = Language(path)
            self.assertEqual(first.code, "zh")
            self.assertTrue(first.toggle())
            self.assertEqual(Language(path).code, "en")
            path.write_bytes(b"unsupported")
            self.assertEqual(Language(path).code, "zh")


class LanguageWidgetTests(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(str(exc))
        self.addCleanup(self.root.destroy)
        self.root.withdraw()
        self.lang = self.root.ui_language = Language(Path(folder.name) / "ui_language.txt")

    def test_combobox_keyboard_and_selection_keep_canonical_order_type(self):
        model = tk.StringVar(master=self.root, value="限价")
        choice = widgets.Combobox(self.root, textvariable=model, values=("市价", "限价"), state="readonly")
        changes = []
        model.trace_add("write", lambda *_: changes.append(model.get()))
        self.lang.toggle()
        self.assertEqual(choice.get(), "Limit")
        self.assertEqual(model.get(), "限价")
        self.assertEqual(changes, [])
        choice.current(0)
        self.assertEqual(model.get(), "市价")
        self.assertEqual(changes, ["市价"])
        self.lang.toggle()
        self.assertEqual(choice.get(), "市价")
        self.assertEqual(changes, ["市价"])
        choice.configure(state="normal", values=("SPX", "SPY"))
        choice.set("AAPL")
        self.lang.toggle()
        self.assertEqual((model.get(), choice.get()), ("AAPL", "AAPL"))

    def test_existing_and_new_widgets_across_switches(self):
        tab = widgets.Notebook(self.root)
        pane = widgets.Frame(tab)
        first = widgets.Frame(tab)
        tab.add(first, text="期权链")
        tab.insert(first, pane, text="成交记录 / 盈亏")
        tree = widgets.Treeview(pane, columns=("合约",), show="headings")
        tree.heading("合约", text="合约")
        row = tree.insert("", "end", iid="contract-17", values=("买方持仓",))
        tree.selection_set(row)
        menu = widgets.Menu(pane, tearoff=False)
        menu.add_command(label="设置止损…")
        source = tk.StringVar(master=self.root, value="未连接")
        label = widgets.Label(pane, textvariable=source)
        self.lang.toggle()
        self.assertEqual(tab.tab(pane, "text"), "History / P&L")
        self.assertEqual(tree.heading("合约", "text"), "Contract")
        self.assertEqual(tree.item(row, "values"), ("Long position",))
        self.assertEqual(menu.entrycget(0, "label"), "Set stop…")
        source.set("资金已更新。")
        self.assertEqual(label.cget("text"), "Funds updated.")
        win = widgets.Toplevel(self.root)
        win.withdraw()
        win.title("确认加仓")
        button = widgets.Button(win, text="是 / 确认")
        self.assertEqual((win.title(), button.cget("text")), ("Confirm add", "Yes / Confirm"))
        tree.item(row, values=("卖方持仓",))
        self.assertEqual(tree.item(row, "values"), ("Short position",))
        self.lang.toggle()
        self.assertEqual((win.title(), button.cget("text")), ("确认加仓", "是 / 确认"))
        self.assertEqual(tree.item(row, "values"), ("卖方持仓",))
        self.assertEqual(tree.selection(), (row,))
        self.assertEqual(tab.tab(pane, "text"), "成交记录 / 盈亏")
        self.assertEqual(label.cget("text"), source.get())
        win.destroy()
        tree.delete(row)
        self.lang.toggle()
