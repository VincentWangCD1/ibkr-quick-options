"""IBKR Quick Options: compact desktop UI, one serialized broker worker."""
from __future__ import annotations
import asyncio
import json
import logging
import math
import os
from pathlib import Path
import queue
import threading
import time
import traceback
import tkinter as tk
import localized_widgets as ttk
from ui_language import Language, display, window_title
from privacy_view import BeanStrip
from quickoptions_version import APP_VERSION
from popup_position import messagebox
from entry_ui import BUDGET_SELECTION, EntryUiMixin, OPEN_SIDES, parse_chain_limit, direction_label
from positions_ui import PositionsUiMixin
from contract_display import order_display
from history_ui import HistoryUiMixin
from panel_focus import PanelFocusMixin
from auto_mid import AutoMidManager, policy as auto_mid_policy
from usage_logs import UsageLog, UsageHandler, sanitize, message_kind
from core import normalize_symbol, close_quantity
from ui_state import (build_fund_cards, default_depth, empty_chain_message,
                      chain_rows_with_reference, quote_age_text, centered_chain_fraction,
                      close_order_fields, build_close_order_spec, close_order_confirmation,
                      order_number_text, order_parameters_text, build_entry_fields,
                      modify_order_confirmation)

MODES = {"IBKR 实盘": "LIVE", "IBKR 模拟盘": "PAPER", "离线演示（虚构行情）": "DEMO"}
MONEYNESS = {"虚值 OTM": "OTM", "平值 ATM": "ATM", "实值 ITM": "ITM"}
BASE = Path(os.environ.get("QUICKOPTIONS_DATA_HOME") or Path(__file__).resolve().parent)
READ_REQUESTS = frozenset(("snapshot", "refresh_funds", "refresh_positions", "expirations", "trading_classes", "chain", "preview", "preview_pair", "preview_modify", "preview_close", "preview_add", "prepare_cost_stop", "execution_history", "import_execution_history", "import_flex_reports"))
ORDER_REQUESTS = frozenset(("buy", "add_confirm", "set_breakeven_stop", "raise_cost_stop", "close", "close_confirm", "close_all", "cancel_own", "modify_order", "cancel_order", "auto_mid_start", "auto_mid_stop"))
CONTROL_REQUESTS = frozenset(("connect", "disconnect"))
ENTRY_MODES = {"市价": "MKT", "限价": "LMT", "中间价（固定限价）": "MID", "中间价（自动追价）": "MID"}
ROUNDING_MODES = {"四舍五入": "nearest", "向下取整": "floor"}
CLOSE_TYPES = {"限价止盈（LMT）": "LMT", "止损（STP）": "STP", "止损限价（STP LMT）": "STP LMT",
               "移动止损/止盈（TRAIL）": "TRAIL", "移动限价止损（TRAIL LIMIT）": "TRAIL LIMIT"}


class Worker(threading.Thread):
    def __init__(self, inbox, outbox, history_path=None):
        super().__init__(daemon=True)
        self.inbox, self.outbox, self.broker = inbox, outbox, None
        self.last_pump_error = 0.0
        self.loop = None
        self.active_job = None
        self.history_path = history_path
        self.auto_mid = None

    def interrupt_read(self, request_id):
        """Interrupt only a read, on its owning event-loop thread, for disconnect.

        IB's synchronous wrappers run this loop while waiting for responses.
        Never cancel an order operation, never touch the broker from Tk's thread.
        """
        loop = self.loop
        if loop is None or loop.is_closed():
            return

        def interrupt():
            job = self.active_job
            if not job or job[0] not in READ_REQUESTS or (job[1] or {}).get("id") != request_id:
                return
            try:
                if self.broker:
                    self.broker.disconnect()
            except Exception as exc:
                self.log(f"断开后台查询连接时返回：{exc}")
            finally:
                for task in asyncio.all_tasks(loop):
                    task.cancel()

        try:
            loop.call_soon_threadsafe(interrupt)
        except RuntimeError:
            pass  # The worker may have closed its loop during window shutdown.

    def log(self, message):
        self.outbox.put(("log", str(message), None, None))

    def request_auto_stop(self, order_id=None):
        if self.auto_mid:
            self.auto_mid.request_stop(order_id)

    def run(self):
        loop = asyncio.new_event_loop()
        self.loop = loop
        asyncio.set_event_loop(loop)
        try:
            while True:
                try:
                    item = self.inbox.get(timeout=.05)
                except queue.Empty:
                    if self.broker:
                        try:
                            self.broker.pump()
                            if self.auto_mid and self.inbox.empty():
                                self.auto_mid.tick()
                        except Exception as exc:
                            if time.monotonic() - self.last_pump_error > 5:
                                self.log(f"连接事件：{exc}")
                                self.last_pump_error = time.monotonic()
                    continue
                name, args = item[:2]
                context = item[2] if len(item) > 2 else None
                self.active_job = (name, context)
                try:
                    if name == "shutdown":
                        if self.auto_mid:
                            self.auto_mid.stop(reason="工具关闭，停止自动追价；原订单保留")
                        if self.broker:
                            self.broker.disconnect()
                        break
                    if name == "connect":
                        if self.auto_mid:
                            self.auto_mid.stop(reason="账户重连，停止自动追价")
                        if self.broker:
                            self.broker.disconnect()
                        self.broker = None
                        if args["mode"] == "DEMO":
                            from demo_broker import DemoBroker
                            self.broker = DemoBroker(self.log, history_path=self.history_path)
                        else:
                            try:
                                from ib_broker import IBBroker
                                self.broker = IBBroker(self.log, history_path=self.history_path)
                            except ImportError as exc:
                                raise RuntimeError("请先运行 install.bat 安装依赖。") from exc
                        result = self.broker.connect(**args)
                        self.auto_mid = AutoMidManager(self.broker)
                    elif self.broker:
                        result = self.execute_broker(name, args)
                    else:
                        raise RuntimeError("请先连接账户。")
                    self.outbox.put((name, result, None, context))
                except asyncio.CancelledError:
                    self.outbox.put((name, None, "查询已因断开操作取消。", context))
                except Exception as exc:
                    self.outbox.put((name, None, str(exc), context))
                finally:
                    self.active_job = None
        finally:
            loop.close()

    def execute_broker(self, name, args):
        args = dict(args)
        config = args.pop("auto_mid", None)
        if config is not None:
            config = auto_mid_policy(config)  # Validate before any initial order.
        if self.auto_mid is None:
            self.auto_mid = AutoMidManager(self.broker)
        if name == "auto_mid_start":
            self.auto_mid.start(args["order_id"], args.get("config"), expected=args.get("expected"))
            return self.auto_mid.decorate(self.broker.snapshot())
        if name == "auto_mid_stop":
            self.auto_mid.stop(args.get("order_id"))
            return self.auto_mid.decorate(self.broker.snapshot())
        if name in ("disconnect", "cancel_own", "modify_order"):
            self.auto_mid.stop(reason="手动操作/断开连接，停止自动追价；请核对原单")
        elif name in ("preview_modify", "cancel_order"):
            self.auto_mid.stop(args.get("order_id"), "已切换到手动改单/撤单")
        result = getattr(self.broker, "buy" if name == "add_confirm" else name)(**args)
        if config is not None and isinstance(result, dict) and "order_id" in result:
            try:
                self.auto_mid.start(result["order_id"], config, order=result)
            except Exception as exc:
                # The initial order already exists: never report it as unsent.
                self.log("原订单已提交，但自动追价未启用：" + str(exc))
                result = {**result, "auto_mid_status": "未启用自动追价：" + str(exc)}
        return self.auto_mid.decorate(result)


class App(EntryUiMixin, PositionsUiMixin, HistoryUiMixin, PanelFocusMixin):
    def __init__(self, root):
        self.root = root
        self.language = root.ui_language = Language(BASE / "data" / "ui_language.txt")
        self.visible_log_lines = []
        self.bean_file = BASE / "data" / "bean_mode.enabled"
        self.bean_mode = self.bean_file.exists()
        self.color_file = BASE / "data" / "green_profit.enabled"
        self.green_profit = self.color_file.exists()
        self.masked_labels = []
        self.normal_position_widths = {}
        root.bean_mode = self.bean_mode
        window_title(root, f"IBKR 快捷期权 V{APP_VERSION}")
        root.geometry("1220x930")
        root.minsize(1020, 780)
        self.usage_log = UsageLog(BASE / "logs", APP_VERSION)
        self.usage_handler = UsageHandler(self.usage_log)
        root.usage_journal, root.usage_handler = self.usage_log, self.usage_handler
        logging.getLogger().addHandler(self.usage_handler)
        root.report_callback_exception = self.report_ui_exception
        root.bind("<Destroy>", lambda event: self.close_usage_log() if event.widget == root else None, add="+")
        self.inbox, self.outbox = queue.Queue(), queue.Queue()
        self.worker = Worker(self.inbox, self.outbox, history_path=str(BASE / "data" / "trade_history.sqlite3"))
        self.worker.start()
        self.busy = self.connected = self.locked = self.financial_ready = False
        self.session_mode = None
        self.session_epoch = self.request_sequence = 0
        self.active_request = None
        self.active_job = None
        self.pending_requests = {}
        self.request_generations = {}
        self.active_symbol = "SPX"
        self.close_confirmation_open = False
        self.modify_confirmation_open = False
        self.expiry_symbol = None
        self.pending_symbol = self.suppress_change = False
        self.need_expiries = self.need_classes = False
        self.preview_dirty = self.chain_dirty = False
        self.preview_data = self.preview_fingerprint = None
        self.init_entry_state()
        self.init_position_ui_state()
        self.init_history_ui_state()
        self.snapshot_data, self.positions_by_id = {}, {}
        self.orders_by_id = {}
        self.edit_order_key = None
        self.chain_offset = 0
        self.chain_center_pending = True
        self.chain_render_key = None
        self.chain_row_height = 26
        self.reference_price = None
        self.reference_pending = False
        self.chain_has_prev = self.chain_has_next = False
        self.last_snapshot = self.last_funds = self.last_chain = self.last_preview = 0.0
        self.connect_controls, self.logs = [], []
        self.build()
        self.apply_bean_mode()
        self.clear_session_view("未连接。可先使用离线演示。", bump=False)
        root.after(100, self.poll)
        root.protocol("WM_DELETE_WINDOW", self.shutdown)
        self.log("离线演示使用虚构资金和行情；IBKR 模拟盘/实盘使用各自账户，切换时清空旧显示。")

    @staticmethod
    def var(value):
        return tk.StringVar(value=str(value))

    def display_text(self, source, hidden=None):
        from privacy_view import mask_money
        target = self.var("")
        def update(*_):
            target.set((hidden if hidden is not None else mask_money(source.get())) if self.bean_mode else source.get())
        source.trace_add("write", update)
        self.masked_labels.append(update)
        update()
        return target

    def toggle_language(self):
        saved = self.language.toggle()
        self.language_action.set("English" if self.language.code == "zh" else "简体中文")
        window_title(self.root, self.root.source_title)
        self.chain_tree.draw()
        self.history_cards.draw()
        self.update_focus_title()
        if getattr(self, "share_window", None) and self.share_window.winfo_exists():
            self.share_window.redraw_card()
        position = self.log_box.yview()
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.insert("end", "".join(display(self.root, line) for line in self.visible_log_lines))
        self.log_box.configure(state="disabled")
        if position[1] >= 0.999:
            self.log_box.see("end")
        else:
            self.log_box.yview_moveto(position[0])
        if not saved:
            self.status.set("语言已切换，本次偏好未能保存。")

    def toggle_bean_mode(self):
        self.bean_mode = not self.bean_mode
        self.apply_bean_mode()
        try:
            self.bean_file.parent.mkdir(exist_ok=True)
            if self.bean_mode:
                self.bean_file.touch()
            else:
                self.bean_file.unlink(missing_ok=True)
        except OSError:
            self.status.set("显示模式已切换，本次偏好未能保存。")

    def apply_bean_mode(self):
        self.dismiss_share_card()
        hide = self.bean_mode
        self.root.bean_mode = hide
        self.bean_button.configure(text="欢乐豆模式 · 开" if hide else "欢乐豆模式 · 关")
        for update in self.masked_labels:
            update()
        for strip in self.bean_strips:
            if hide:
                strip.place(x=0, y=0, relwidth=1, relheight=1)
            else:
                strip.place_forget()
        compact_columns = ("合约", "持仓 / 张数", "盈亏 %")
        if hide and not self.normal_position_widths:
            self.normal_position_widths = {c: self.pos_tree.column(c, "width") for c in compact_columns}
        self.pos_tree.configure(displaycolumns=compact_columns if hide else "#all")
        if not hide and self.normal_position_widths:
            for column, width in self.normal_position_widths.items():
                self.pos_tree.column(column, width=width)
            self.normal_position_widths.clear()
        self.history_tree.configure(displaycolumns=("成交时间", "合约 / 品种", "买卖", "数量", "成交价", "来源", "分享") if hide else "#all")
        self.apply_pnl_colors()
        self.history_cards.hide_amounts = hide
        self.history_cards.draw()
        if hide and self.notebook.select() == str(self.log_tab):
            self.notebook.select(self.position_tab)
        self.notebook.tab(self.log_tab, state="disabled" if hide else "normal")
        if hide:
            self.history_pnl_label.pack_forget()
        else:
            self.history_pnl_label.pack(side="right")
        self.budget_input.configure(show="•" if hide and self.budget_mode.get() == "金额 USD" else "")
        self.budget_summary.set(self.budget_summary_text())
        self.dismiss_position_add()
        self.dismiss_position_close()
        if self.positions_by_id:
            self.render_positions(list(self.positions_by_id.values()))

    def apply_window_pin(self):
        self.root.attributes("-topmost", self.pin_window.get())

    def toggle_pnl_colors(self):
        self.green_profit = not self.green_profit
        self.apply_pnl_colors()
        if self.positions_by_id:
            self.render_positions(list(self.positions_by_id.values()))
        if self.history_data:
            self.render_history(self.history_data)
        if self.share_window and self.share_window.winfo_exists():
            self.share_window.redraw_card()
        try:
            self.color_file.parent.mkdir(parents=True, exist_ok=True)
            if self.green_profit:
                self.color_file.touch()
            else:
                self.color_file.unlink(missing_ok=True)
        except OSError:
            self.status.set("显示模式已切换，本次偏好未能保存。")

    def apply_pnl_colors(self):
        from contract_display import pnl_color
        self.pnl_color_button.configure(text="盈绿亏红 ⇄" if self.green_profit else "盈红亏绿 ⇄")
        self.history_cards.green_profit = self.green_profit
        for tag, value in (("profit", 1), ("loss", -1)):
            color = pnl_color(value, self.green_profit)
            self.pos_tree.tag_configure(tag, foreground=color)
            self.history_tree.tag_configure(tag, foreground="#334c5d" if self.bean_mode else color)
        self.update_daily_pnl()

    def update_daily_pnl(self):
        from contract_display import pnl_color
        from ui_state import account_day_label
        financial = (self.snapshot_data or {}).get("financial") or {}
        value = account_day_label(financial).partition("　")[2]
        self.daily_account.set(value)
        color = pnl_color(financial.get("daily_pnl") if value != "—" and not self.bean_mode else None, self.green_profit)
        self.daily_amount_label.configure(foreground=color)

    def build(self):
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TLabel", font=("Microsoft YaHei UI", 9))
        style.configure("TButton", font=("Microsoft YaHei UI", 9), padding=4)
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 17, "bold"), foreground="#14384e")
        style.configure("Money.TLabel", font=("Microsoft YaHei UI", 18, "bold"), foreground="#173f55")
        style.configure("Buy.TButton", font=("Microsoft YaHei UI", 12, "bold"), padding=9)
        style.configure("Close.TButton", font=("Microsoft YaHei UI", 12, "bold"), padding=(12, 9), foreground="#8b2525")
        style.configure("Confirmation.TLabel", font=("Microsoft YaHei UI", 11))
        style.configure("Confirmation.TButton", font=("Microsoft YaHei UI", 11, "bold"),
                        padding=(16, 7), foreground="white", background="#185575")
        style.map("Confirmation.TButton", background=[("pressed", "#123e56"), ("active", "#206888")])
        style.configure("Treeview", rowheight=self.chain_row_height)
        self.root.rowconfigure(0, weight=1)
        self.root.columnconfigure(0, weight=1)
        outer = ttk.Frame(self.root, padding=10)
        outer.grid(row=0, column=0, sticky="nsew")
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(5, weight=1)
        header = ttk.Frame(outer)
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(2, weight=1)
        ttk.Label(header, text=f"快捷期权 / Quick Options · V{APP_VERSION}", style="Title.TLabel").grid(row=0, column=0)
        from PIL import ImageTk
        from share_card import BRAND, brand_avatar
        self.brand_image = ImageTk.PhotoImage(brand_avatar(28), master=self.root)
        ttk.Label(header, text=f" 作者：{BRAND}", image=self.brand_image, compound="left", foreground="#78445e",
                  font=("Segoe UI", 10, "bold")).grid(row=0, column=1, padx=5)
        self.language_action = self.var("English" if self.language.code == "zh" else "简体中文")
        self.language_button = ttk.Button(header, textvariable=self.language_action, width=8, command=self.toggle_language)
        self.language_button.grid(row=0, column=7, padx=4)
        self.pin_window = tk.BooleanVar(master=self.root, value=False)
        ttk.Checkbutton(header, text="窗口置顶", variable=self.pin_window,
                        command=self.apply_window_pin).grid(row=0, column=6, padx=4)
        self.banner = self.var("未连接")
        banner_label = ttk.Label(header, textvariable=self.display_text(self.banner), foreground="#9b4f00")
        banner_label.grid(row=0, column=5, padx=4)
        self.connection_expanded = True
        self.connection_toggle = ttk.Button(header, text="收起账户设置", command=lambda: self.show_connection_details(not self.connection_expanded))
        self.connection_toggle.grid(row=0, column=4, padx=4)
        self.bean_button = ttk.Button(header, text="欢乐豆模式", command=self.toggle_bean_mode)
        self.bean_button.grid(row=0, column=3, padx=4)

        def fit_header(*_):
            # Account names and the English controls can exceed the available width.
            full_width = sum(child.winfo_reqwidth() for child in header.winfo_children()) + 50
            row = int(full_width > header.winfo_width())
            if row != int(banner_label.grid_info()["row"]):
                banner_label.grid_configure(row=row, column=0 if row else 5,
                                            columnspan=8 if row else 1, sticky="e")

        header.bind("<Configure>", fit_header, add="+")
        banner_label.bind("<Configure>", fit_header, add="+")
        funds = ttk.Frame(outer)
        funds.grid(row=1, column=0, sticky="ew", pady=6)
        self.fund_vars = {}
        self.bean_strips = []
        bean = tk.PhotoImage(master=self.root, file=str(Path(__file__).with_name("assets") / "happy_bean.png"))
        sizes = (bean, bean.zoom(3).subsample(4), bean.subsample(2))
        faces = tuple((icon, icon.subsample(-1, 1)) for icon in sizes)
        for col, (key, label) in enumerate((("usd_cash", "账户余额（美元现金）"), ("equity", "总资产（现金＋持仓）"), ("settled_cash", "已结算美元资金"), ("order_cash", "可用于本工具开仓"))):
            funds.columnconfigure(col, weight=1, uniform="fund")
            card = ttk.LabelFrame(funds, text=label, padding=(7, 4))
            card.grid(row=0, column=col, sticky="ew", padx=(0, 5))
            self.fund_vars[key] = self.var("—")
            amount = ttk.Label(card, textvariable=self.display_text(self.fund_vars[key], "欢乐豆"), style="Money.TLabel", wraplength=250, justify="left")
            self.bean_strips.append(BeanStrip(amount, faces))
            amount.pack(anchor="w", fill="x")
            card.bind("<Configure>", lambda event, label=amount: label.configure(wraplength=max(120, event.width - 20)))
        daily = ttk.Frame(funds)
        daily.grid(row=2, column=0, columnspan=4, sticky="w")
        daily_font = ("Microsoft YaHei UI", 11, "bold")
        ttk.Label(daily, text="账户当日盈亏", font=daily_font).pack(side="left", padx=(0, 6))
        self.daily_account = self.var("—")
        self.daily_amount_label = ttk.Label(daily, textvariable=self.display_text(self.daily_account, "欢乐豆　欢乐豆　欢乐豆"),
                                            font=daily_font, wraplength=480)
        self.daily_amount_label.pack(side="left")
        self.bean_strips.append(BeanStrip(self.daily_amount_label, faces))
        self.pnl_color_button = ttk.Button(daily, command=self.toggle_pnl_colors)
        self.pnl_color_button.pack(side="left", padx=8)
        self.fund_detail = self.var("")
        ttk.Label(funds, textvariable=self.display_text(self.fund_detail, ""), foreground="#63717a", wraplength=1160).grid(row=1, column=0, columnspan=4, sticky="w", pady=(3, 0))
        conn = ttk.LabelFrame(outer, text="账户连接", padding=6)
        self.connection_panel = conn
        conn.grid(row=2, column=0, sticky="ew")
        conn.columnconfigure(9, weight=1)
        self.mode = self.var(next(iter(MODES)))
        self.host, self.port, self.client_id = self.var("127.0.0.1"), self.var(7496), self.var(31)
        self.account = self.var("")
        combo = ttk.Combobox(conn, textvariable=self.mode, values=list(MODES), state="readonly", width=22)
        combo.grid(row=0, column=0, padx=3)
        combo.bind("<<ComboboxSelected>>", self.mode_changed)
        self.connect_controls.append((combo, "readonly"))
        for col, label, variable, width in ((1, "Host", self.host, 13), (3, "端口", self.port, 7), (5, "Client ID", self.client_id, 5)):
            ttk.Label(conn, text=label).grid(row=0, column=col, padx=3)
            entry = ttk.Entry(conn, textvariable=variable, width=width)
            entry.grid(row=0, column=col + 1, padx=3)
            self.connect_controls.append((entry, "normal"))
        self.connect_btn = ttk.Button(conn, text="连接 / 开始", command=self.connect)
        self.connect_btn.grid(row=0, column=7, padx=4)
        self.disconnect_btn = ttk.Button(conn, text="断开", command=self.disconnect)
        self.disconnect_btn.grid(row=0, column=8)
        self.funds_btn = ttk.Button(conn, text="刷新资金", command=lambda: self.submit("refresh_funds"))
        self.funds_btn.grid(row=0, column=9, sticky="e", padx=3)
        self.account_picker = ttk.Frame(conn)
        self.account_picker.grid(row=1, column=0, columnspan=7, sticky="w", pady=(5, 0))
        ttk.Label(self.account_picker, text="选择 TWS 账户").pack(side="left", padx=3)
        self.account_box = ttk.Combobox(self.account_picker, textvariable=self.account, state="readonly", width=18)
        self.account_box.pack(side="left")
        self.connect_controls.append((self.account_box, "readonly"))
        self.account_picker.grid_remove()
        body = ttk.Frame(outer)
        body.grid(row=3, column=0, sticky="ew", pady=7)
        for col in (0, 1):
            body.columnconfigure(col, weight=1, uniform="form")
        left = ttk.LabelFrame(body, text="选择合约与现金比例", padding=8)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 7))
        right = ttk.LabelFrame(body, text="匹配预览 / 开仓", padding=8)
        right.grid(row=0, column=1, sticky="nsew")
        for col in (1, 3):
            left.columnconfigure(col, weight=1)
        self.symbol, self.expiry, self.right = self.var("SPX"), self.var(""), self.var("C")
        self.open_side = self.var("买入期权（买方）")
        self.trading_class = self.var("")
        self.moneyness, self.depth = self.var("虚值 OTM"), self.var(4)
        self.previous_moneyness = self.moneyness.get()
        fields = (("标的（可输入）", self.symbol, ("SPX", "SPY", "XSP", "QQQ")),
                  ("到期日（纽约）", self.expiry, ()), ("交易类别", self.trading_class, ()),
                  ("开仓身份", self.open_side, tuple(OPEN_SIDES)),
                  ("虚实值", self.moneyness, tuple(MONEYNESS)), ("档数（平值忽略）", self.depth, tuple(range(1, 21))))
        boxes = []
        for index, (label, variable, values) in enumerate(fields):
            row, col = index // 2, (index % 2) * 2
            ttk.Label(left, text=label).grid(row=row, column=col, sticky="w", pady=3)
            box = ttk.Combobox(left, textvariable=variable, values=values, state="normal" if index in (0, 5) else "readonly", width=12)
            box.grid(row=row, column=col + 1, sticky="ew", padx=(4, 8))
            boxes.append(box)
        self.symbol_box, self.expiry_box, self.class_box, self.right_box, _, self.depth_box = boxes
        for event in ("<<ComboboxSelected>>", "<Return>", "<FocusOut>"):
            self.symbol_box.bind(event, self.commit_symbol)
        self.percent = tk.DoubleVar(value=50)
        self.percent_text = self.var("使用现金 50%")
        ttk.Label(left, textvariable=self.percent_text).grid(row=3, column=0, sticky="w", pady=4)
        self.percent_scale = ttk.Scale(left, from_=0.01, to=100, variable=self.percent, command=self.percent_changed)
        self.percent_scale.grid(row=3, column=1, columnspan=3, sticky="ew", padx=4)
        presets = ttk.Frame(left)
        presets.grid(row=4, column=0, columnspan=4, sticky="ew", pady=(0, 4))
        for col, pct in enumerate((10, 25, 50, 75, 100)):
            presets.columnconfigure(col, weight=1)
            ttk.Button(presets, text=f"{pct}%", command=lambda p=pct: self.set_percent(p)).grid(row=0, column=col, sticky="ew", padx=2)
        self.slippage, self.fee, self.min_fee, self.cap = self.var(5), self.var(1), self.var(1), self.var(10000)
        for index, (label, variable) in enumerate((("滑点预留 %", self.slippage), ("手续费 $/张", self.fee), ("最低费用 $", self.min_fee), ("最多张数", self.cap))):
            row, col = 5 + index // 2, (index % 2) * 2
            ttk.Label(left, text=label).grid(row=row, column=col, sticky="w", pady=2)
            ttk.Entry(left, textvariable=variable, width=9).grid(row=row, column=col + 1, sticky="w", padx=4)
            variable.trace_add("write", self.changed)
        ttk.Button(left, text="重新载入标的", command=lambda: self.commit_symbol(force=True)).grid(row=7, column=0, columnspan=2, sticky="w", pady=(3, 0))
        ttk.Label(left, text="输入后按回车；SPX 请核对交易类别。", foreground="#667780").grid(row=7, column=2, columnspan=2, sticky="w")
        self.rounding_mode = self.var("四舍五入")
        ttk.Label(left, text="开仓张数取整").grid(row=8, column=0, sticky="w", pady=(6, 0))
        ttk.Combobox(left, textvariable=self.rounding_mode, values=tuple(ROUNDING_MODES), state="readonly", width=12).grid(row=8, column=1, sticky="w", padx=4, pady=(6, 0))
        ttk.Label(left, text="四舍五入可超过所选比例，上限为可用资金。按预算找档时向下取整。", foreground="#945900", wraplength=250).grid(row=8, column=2, columnspan=2, sticky="w", pady=(6, 0))
        self.rounding_mode.trace_add("write", self.changed)
        for variable, callback in ((self.symbol, self.symbol_typed), (self.expiry, self.expiry_changed), (self.trading_class, self.class_changed),
                                   (self.moneyness, self.moneyness_changed), (self.right, self.changed), (self.depth, self.contract_input_changed)):
            variable.trace_add("write", callback)
        self.build_extra_selection(left)
        self.entry_options_expanded = False
        self.entry_option_widgets = [w for w in left.grid_slaves() if int(w.grid_info()["row"]) in (5, 6, 7, 8, 10, 11)]
        self.entry_options_button = ttk.Button(left, text="高级参数 / 指定合约 ▾", command=self.toggle_entry_options)
        self.entry_options_button.grid(row=12, column=0, columnspan=4, sticky="w", pady=(3, 0))
        self.toggle_entry_options(False)
        self.build_entry_panel(right)
        self.open_side.trace_add("write", self.open_side_changed)
        self.status = self.var("就绪")
        ttk.Label(outer, textvariable=self.display_text(self.status), foreground="#24506a", wraplength=1160).grid(row=4, column=0, sticky="ew", pady=(0, 5))
        self.build_notebook(outer)
        self.update_enabled()

    def build_notebook(self, outer):
        self.build_work_area(outer)
        self.notebook = ttk.Notebook(self.workspace)
        self.notebook.grid(row=0, column=0, sticky="nsew")
        self.focus_button.lift()
        self.chain_tab, self.position_tab, self.order_tab, self.log_tab = [ttk.Frame(self.notebook, padding=6) for _ in range(4)]
        for frame, title in ((self.chain_tab, "期权链"), (self.position_tab, "持仓 / 平仓"), (self.order_tab, "订单 / 成交"), (self.log_tab, "运行日志")):
            self.notebook.add(frame, text=title)
            frame.columnconfigure(0, weight=1)
            frame.rowconfigure(1, weight=1)
        self.notebook.bind("<<NotebookTabChanged>>", self.tab_changed)
        bar = ttk.Frame(self.chain_tab)
        bar.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        self.chain_caption = self.var("未连接，期权链已清空。")
        self.chain_heading = self.var("未连接")
        ttk.Label(bar, textvariable=self.chain_heading).pack(side="left")
        self.chain_reference_button = ttk.Button(bar, text="参考/说明 ▾", style="Compact.TButton", command=self.toggle_chain_reference)
        self.chain_reference_button.pack(side="right", padx=2)
        self.chain_width = self.var(40)
        self.chain_next_btn = ttk.Button(bar, text="下一页 →", command=lambda: self.page_chain(self.chain_limit()))
        self.chain_atm_btn = ttk.Button(bar, text="回到平值", command=lambda: self.page_chain(None))
        self.chain_prev_btn = ttk.Button(bar, text="← 上一页", command=lambda: self.page_chain(-self.chain_limit()))
        self.chain_refresh_btn = ttk.Button(bar, text="刷新链", command=self.request_chain)
        for button in (self.chain_next_btn, self.chain_atm_btn, self.chain_prev_btn, self.chain_refresh_btn):
            button.pack(side="right", padx=2)
        self.chain_width_box = ttk.Combobox(bar, textvariable=self.chain_width, values=(20, 40, 80, 100, 200, "所有档数"), width=9, state="normal")
        self.chain_width_box.pack(side="right", padx=3)
        self.chain_width_box.bind("<<ComboboxSelected>>", self.chain_width_changed)
        self.chain_width_box.bind("<Return>", self.chain_width_changed)
        self.chain_width_box.bind("<FocusOut>", self.chain_width_changed)
        ttk.Label(bar, text="每页档数").pack(side="right")
        refbar = ttk.Frame(self.chain_tab)
        self.chain_reference_bar = refbar
        refbar.grid(row=1, column=0, sticky="ew", pady=(0, 4))
        ttk.Label(refbar, textvariable=self.chain_caption, wraplength=1160, foreground="#687680").pack(side="top", anchor="w")
        refbar = ttk.Frame(refbar)
        refbar.pack(fill="x")
        ttk.Label(refbar, text="备用参考价（仅看链）").pack(side="left")
        self.reference_input = self.var("")
        self.reference_entry = ttk.Entry(refbar, textvariable=self.reference_input, width=11)
        self.reference_entry.pack(side="left", padx=5)
        self.reference_entry.bind("<Return>", self.commit_reference)
        self.reference_entry.bind("<FocusOut>", self.commit_reference)
        self.reference_input.trace_add("write", self.reference_typed)
        ttk.Label(refbar, text="回车确认；仅在其他参考价均缺失时使用，不参与下单；切换标的清空。", foreground="#687680").pack(side="left")
        self.chain_status = self.var("等待期权链数据。")
        self.chain_status_label = ttk.Label(self.chain_tab, textvariable=self.chain_status, foreground="#945900", wraplength=1160)
        self.chain_status_label.grid(row=2, column=0, sticky="w", pady=(0, 2))
        self.chain_reference_bar.grid_remove()
        self.chain_tab.rowconfigure(1, weight=0)
        self.chain_tab.rowconfigure(2, weight=0)
        self.chain_tab.rowconfigure(3, weight=1)
        from chain_view import OptionChainView
        self.chain_tree = OptionChainView(self.chain_tab)
        self.chain_tree.grid(row=3, column=0, sticky="nsew")
        self.chain_row_height = self.chain_tree.row_height
        self.chain_tree.bind("<Double-1>", self.select_chain_contract)
        self.build_position_panel()
        self.build_order_edit_panel()
        self.order_tree = self.make_tree(self.order_tab, (("订单", 62), ("合约", 205), ("买卖", 50), ("类型", 70),
            ("委托价", 68), ("触发 / 移动参数", 130), ("有效期", 55), ("已成 / 委托", 85),
            ("剩余", 50), ("状态", 93), ("成交均价", 68), ("下单时间（北京）", 132), ("距到期", 77),
            ("买一 / 卖一", 104), ("最新成交", 72), ("行情", 90), ("中间价", 72)))
        self.order_tree.configure(displaycolumns=("合约", "距到期", "下单时间（北京）", "买卖", "类型", "委托价", "买一 / 卖一", "中间价",
            "最新成交", "行情", "已成 / 委托", "剩余", "状态", "成交均价", "有效期", "触发 / 移动参数", "订单"))
        self.order_tree.bind("<<TreeviewSelect>>", self.order_selected)
        self.build_history_panel()
        logbar = ttk.Frame(self.log_tab)
        logbar.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 3))
        ttk.Button(logbar, text="打开日志文件夹", command=self.open_logs_folder).pack(side="right")
        ttk.Button(logbar, text="更新周/月汇总", command=self.usage_log.request_refresh).pack(side="right", padx=5)
        self.usage_status = self.var("自动日志：" + str(BASE / "logs") + " · 每分钟更新汇总")
        ttk.Label(logbar, textvariable=self.usage_status, wraplength=850).pack(side="left")
        self.log_box = tk.Text(self.log_tab, height=8, font=("Consolas", 9), state="disabled", background="#f2f5f7", relief="flat", wrap="word")
        self.log_box.grid(row=1, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(self.log_tab, orient="vertical", command=self.log_box.yview)
        scroll.grid(row=1, column=1, sticky="ns")
        self.log_box.configure(yscrollcommand=scroll.set)

    def build_order_edit_panel(self):
        panel = ttk.Frame(self.order_tab)
        panel.grid(row=0, column=0, sticky="ew", pady=(0, 5))
        self.order_caption = self.var("选择一笔未成交订单后可改价或撤单。")
        ttk.Label(panel, textvariable=self.display_text(self.order_caption), foreground="#24506a").pack(anchor="w")
        bar = ttk.Frame(panel)
        bar.pack(fill="x", pady=4)
        self.modify_mode, self.modify_limit = self.var("限价"), self.var("")
        self.order_filter = self.var("全部")
        order_filter = ttk.Combobox(bar, textvariable=self.order_filter, values=("全部", "挂单", "已结束"), state="readonly", width=8)
        order_filter.pack(side="left", padx=(0, 8))
        order_filter.bind("<<ComboboxSelected>>", lambda _: self.render_orders(self.snapshot_data.get("orders", [])))
        ttk.Label(bar, text="改为").pack(side="left")
        self.modify_mode_box = ttk.Combobox(bar, textvariable=self.modify_mode, values=("市价", "限价", "中间价（固定限价）"), state="readonly", width=20)
        self.modify_mode_box.pack(side="left", padx=5)
        ttk.Label(bar, text="限价 $/份").pack(side="left")
        self.modify_limit_entry = ttk.Entry(bar, textvariable=self.modify_limit, width=10)
        self.modify_limit_entry.pack(side="left", padx=5)
        self.modify_btn = ttk.Button(bar, text="预览改单", command=self.request_modify)
        self.modify_btn.pack(side="left", padx=5)
        self.cancel_selected_btn = ttk.Button(bar, text="撤销所选", command=self.cancel_selected_order)
        self.cancel_selected_btn.pack(side="left", padx=5)
        self.auto_mid_start_btn = ttk.Button(bar, text="开启自动中间价", command=self.start_auto_mid_selected)
        self.auto_mid_start_btn.pack(side="left", padx=3)
        self.auto_mid_stop_btn = ttk.Button(bar, text="停止自动追价", command=self.stop_auto_mid_selected)
        self.auto_mid_stop_btn.pack(side="left", padx=3)
        self.modify_mode.trace_add("write", lambda *_: self.update_enabled())
        ttk.Label(panel, text="挂单优先。其他客户端可见挂单仅供查看。* 表示该侧报价超过 10 秒或时间未知。「首报」是首次收到回报的时间。", foreground="#687680", wraplength=1160).pack(anchor="w")

    def selected_order(self):
        selected = self.order_tree.selection()
        return self.orders_by_id.get(selected[0], {}) if selected else {}

    def order_selected(self, *_):
        order = self.selected_order()
        if order:
            key = (order.get("client_id"), order["order_id"])
            if key != self.edit_order_key:
                self.edit_order_key = key
                self.modify_limit.set(order_number_text(order.get("limit_price")) if order.get("order_type") == "LMT" else "")
                self.modify_mode.set("市价" if order.get("order_type") == "MKT" else "限价")
            view = order_display(order)
            note = order.get("auto_mid_status", "") if view["active"] else ""
            if not order.get("owned", True):
                note = "其他客户端委托，请在原客户端管理。"
            self.order_caption.set(f'{view["contract"]} · {view["side"]} · {view["status"]} · 剩余 {order["remaining"]:g} 张'
                                   f' · 订单 {order["order_id"]} · {order_parameters_text(order)}  {note}')
        else:
            self.edit_order_key = None
            self.order_caption.set("选择一笔未成交订单后可改价或撤单。")
        self.update_enabled()

    def order_edit_ready(self, cancel=False):
        order = self.selected_order()
        status = str(order.get("status", "")).split(" (", 1)[0]
        statuses = ("PreSubmitted", "Submitted", "PendingSubmit") if cancel else ("PreSubmitted", "Submitted")
        return bool(self.connected and not self.locked and not self.modify_confirmation_open
                    and not self.has_pending(ORDER_REQUESTS | CONTROL_REQUESTS | {"preview_modify"})
                    and order and order.get("owned", True) and order.get("remaining", 0) > 0
                    and status in statuses and (cancel or (order.get("order_type") in ("MKT", "LMT")
                                                          and order.get("modifiable", True))))

    def modify_fingerprint(self):
        order = self.selected_order()
        return json.dumps((order.get("order_id"), order.get("client_id"), self.modify_mode.get(), self.modify_limit.get()))

    def request_modify(self):
        if not self.order_edit_ready():
            self.status.set("请选择已接受的普通市价/限价挂单；止损和移动单可先撤销，再在持仓页重设。")
            return
        try:
            fields = build_entry_fields(ENTRY_MODES[self.modify_mode.get()], self.modify_limit.get())
        except (ValueError, KeyError) as exc:
            self.status.set(str(exc))
            return
        self.submit("preview_modify", order_id=self.selected_order()["order_id"],
                    order_mode=fields["order_mode"], limit_price=fields["limit_price"])

    def confirm_modify(self, preview, context):
        if (self.modify_confirmation_open or self.has_pending(ORDER_REQUESTS | CONTROL_REQUESTS)
                or not self.context_is_current("preview_modify", context)):
            return
        self.modify_confirmation_open = True
        self.update_enabled()
        try:
            account = preview.get("account") or self.snapshot_data.get("account", "")
            title = "IBKR 实盘改单" if self.session_mode == "LIVE" else "确认改单"
            accepted = messagebox.askyesno(title, f'账户：{account}\n' + modify_order_confirmation(preview), parent=self.root)
            if not accepted:
                self.status.set("未提交改单，原订单保持不变。")
                return
            if (not self.context_is_current("preview_modify", context) or not self.connected or self.locked
                    or self.has_pending(ORDER_REQUESTS | CONTROL_REQUESTS)
                    or time.time() - preview["created_at"] > 10):
                self.status.set("改单预览已失效，请重新预览。")
                return
            self.mutate("modify_order", token=preview["token"])
        finally:
            self.modify_confirmation_open = False
            self.update_enabled()

    def cancel_selected_order(self):
        if not self.order_edit_ready(cancel=True):
            return
        order = dict(self.selected_order())
        self.mutate("cancel_order", order_id=order["order_id"])

    def build_close_order_panel(self):
        panel = ttk.LabelFrame(self.position_tab, text="止盈 / 止损委托（仅所选多头持仓）", padding=6)
        self.protection_feedback = self.var('')
        self.close_panel = panel
        panel.grid(row=1, column=0, sticky="ew", pady=(0, 5))
        top = ttk.Frame(panel)
        top.pack(fill="x")
        self.close_type = self.var(next(iter(CLOSE_TYPES)))
        self.close_percent, self.close_tif = self.var(100), self.var("DAY")
        self.close_type_box = ttk.Combobox(top, textvariable=self.close_type, values=tuple(CLOSE_TYPES), state="readonly", width=25)
        self.close_type_box.pack(side="left", padx=(0, 8))
        self.close_type_box.bind("<<ComboboxSelected>>", self.update_close_order_fields)
        ttk.Label(top, text="平仓比例 %").pack(side="left")
        self.close_percent_entry = ttk.Entry(top, textvariable=self.close_percent, width=6)
        self.close_percent_entry.pack(side="left", padx=(4, 12))
        ttk.Label(top, text="有效期").pack(side="left")
        self.close_tif_box = ttk.Combobox(top, textvariable=self.close_tif, values=("DAY", "GTC"), state="readonly", width=6)
        self.close_tif_box.pack(side="left", padx=4)
        self.close_order_btn = ttk.Button(top, text="提交所选卖单", command=self.submit_selected_close_order)
        self.close_order_btn.pack(side="right", padx=3)
        ttk.Button(top, text="收起", command=panel.grid_remove).pack(side="right", padx=3)
        fields = ttk.Frame(panel)
        fields.pack(fill="x", pady=(5, 3))
        self.close_spec_values = {key: self.var(value) for key, value in
                                  (("limit_price", ""), ("stop_price", ""), ("trail_mode", "百分比 %"),
                                   ("trail_value", ""), ("limit_offset", ""))}
        self.close_field_widgets = {}
        for col, (key, label) in enumerate((("limit_price", "限价"), ("stop_price", "止损触发价"),
                                           ("trail_mode", "移动单位"), ("trail_value", "移动幅度"), ("limit_offset", "移动限价偏移"))):
            frame = ttk.Frame(fields)
            frame.grid(row=0, column=col, sticky="w", padx=(0, 10))
            field_label = ttk.Label(frame, text=label)
            field_label.pack(side="left")
            if key == "stop_price":
                self.close_stop_label = field_label
            if key == "trail_mode":
                widget = ttk.Combobox(frame, textvariable=self.close_spec_values[key], values=("百分比 %", "价差 $"), state="readonly", width=9)
            else:
                widget = ttk.Entry(frame, textvariable=self.close_spec_values[key], width=9)
            widget.pack(side="left", padx=(4, 0))
            self.close_field_widgets[key] = (frame, widget)
        ttk.Label(panel, textvariable=self.display_text(self.protection_feedback), foreground='#234e70', wraplength=1100).pack(anchor='w')
        ttk.Label(panel, text="限价/触发价/价差均为期权报价（美元/份，标准合约 ×100），不是标的点数或总权利金。", foreground="#687680").pack(anchor="w")
        ttk.Label(panel, text="止损(STP)/移动止损(TRAIL)仅常规触发；限价型可申请延长时段，须 IBKR 支持。移动从提交后开始跟随，不是到盈利价才启动。已有挂单须先撤销并等取消回报，市价按钮不会绕过。", foreground="#945900", wraplength=1160).pack(anchor="w")
        self.update_close_order_fields()

    def update_close_order_fields(self, *_):
        kind = CLOSE_TYPES[self.close_type.get()]
        visible = close_order_fields(kind)
        self.close_stop_label.configure(text="初始止损价" if kind == "TRAIL LIMIT" else "止损触发价")
        for field, (frame, _) in self.close_field_widgets.items():
            frame.grid() if field in visible else frame.grid_remove()

    def submit_selected_close_order(self, cost_guard=None):
        if (not self.connected or self.locked or self.close_confirmation_open
                or self.has_pending(ORDER_REQUESTS | CONTROL_REQUESTS)):
            return
        selected = self.pos_tree.selection()
        position = dict(self.positions_by_id.get(selected[0], {})) if selected else {}
        if not position:
            self.status.set("请先在持仓表选择需要卖出的持仓。")
            return
        if position.get("quantity", 0) < 0:
            self.status.set("当前是空头持仓，请使用上方快捷平仓按钮买回；自选卖单仅用于平已有多头。")
            return
        try:
            percent = float(self.close_percent.get())
            quantity = close_quantity(position["quantity"], percent)
            values = {key: variable.get() for key, variable in self.close_spec_values.items()}
            values["trail_mode"] = {"百分比 %": "percent", "价差 $": "amount"}.get(values["trail_mode"])
            values["tif"] = self.close_tif.get()
            spec = build_close_order_spec(CLOSE_TYPES[self.close_type.get()], values)
        except (ValueError, KeyError, OverflowError) as exc:
            self.status.set(f"卖单参数无效：{exc}")
            if hasattr(self, 'protection_feedback'):
                self.protection_feedback.set(str(exc))
            return
        epoch = self.session_epoch
        self.close_confirmation_open = True
        self.update_enabled()
        try:
            mode = {"LIVE": "IBKR 实盘", "PAPER": "IBKR 模拟盘", "DEMO": "离线演示（虚构成交）"}.get(self.session_mode, "账户模式待确认")
            amend_stop = bool(cost_guard and cost_guard.get("order_id"))
            details = f'{mode} · 账户 {self.snapshot_data.get("account", "待确认")}\n' + close_order_confirmation(position, quantity, percent, spec, amend_stop=amend_stop)
            if amend_stop:
                details += f'\n上调原止损单 #{cost_guard["order_id"]}：{cost_guard["old_stop"]:g} → {cost_guard["stop_price"]:g} USD/份。'
            if cost_guard:
                details += (f'\n每份持仓成本 {cost_guard["unit_cost"]:g} × {cost_guard["multiple"]:g} 倍，触发价 {cost_guard["stop_price"]:g}，当前买一 {cost_guard["bid"]:g}。'
                            '\n触发后按市价卖出，滑点和平仓费用会影响实际盈亏。')
            confirmed = messagebox.askyesno("确认成本止损" if cost_guard else "确认提交所选卖单", details, parent=self.root)
        finally:
            self.close_confirmation_open = False
            self.update_enabled()
        if not confirmed:
            self.status.set("已取消卖单，未提交。")
            return
        if epoch != self.session_epoch or not self.connected:
            self.status.set("账户连接已改变，请重新选择持仓并确认。")
            return
        current = self.positions_by_id.get(str(position["conid"]))
        if (not current or current.get("quantity") != position.get("quantity")
                or self.pos_tree.selection() != selected or (current.get("close_pending") and not amend_stop)):
            self.status.set("持仓数量已变化，请重新确认卖单。")
            return
        if amend_stop:
            self.mutate("raise_cost_stop", cost_guard=cost_guard)
            return
        extra = {"cost_guard": cost_guard} if cost_guard else {}
        self.mutate("close", conid=int(position["conid"]), percent=percent, order_spec=spec, **extra)

    @staticmethod
    def make_tree(parent, columns, row=1):
        frame = ttk.Frame(parent)
        frame.grid(row=row, column=0, sticky="nsew")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        tree = ttk.Treeview(frame, columns=[c[0] for c in columns], show="headings", height=8, selectmode="browse")
        for label, width in columns:
            tree.heading(label, text=label)
            tree.column(label, width=width, minwidth=50)
        vertical = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        horizontal = ttk.Scrollbar(frame, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        tree.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        return tree

    @staticmethod
    def _clear_tree(tree):
        children = tree.get_children()
        if children:
            tree.delete(*children)

    def clear_preview(self, reason="参数已改变，等待重新预览。"):
        self.preview_data = self.preview_fingerprint = None
        self.match_text.set(reason)
        self.buy_btn.configure(text="买入（等待预览）")
        self.clear_entry_cards(reason)

    def clear_chain(self, reason=None):
        self._clear_tree(self.chain_tree)
        self.chain_rows_by_id = {}
        if hasattr(self.chain_tree, "set_context"):
            self.chain_tree.set_context({})
        self.chain_has_prev = self.chain_has_next = False
        self.chain_center_pending = True
        self.chain_render_key = None
        message = reason or empty_chain_message(self.connected, self.active_symbol, self.expiry.get())
        self.chain_caption.set(message)
        self.chain_status.set(message)
        if hasattr(self, "chain_heading"):
            self.chain_heading.set("期权链 · 等待数据")
            self.chain_status_label.grid()

    def apply_fund_cards(self, cards):
        for key, variable in self.fund_vars.items():
            variable.set(cards[key])
        self.fund_detail.set(cards["detail"])
        if hasattr(self, "daily_amount_label"):
            self.update_daily_pnl()
        self.banner.set(cards["connection"])

    def clear_session_view(self, reason, bump=True):
        if hasattr(self, "daily_account"):
            self.daily_account.set("—")
        if bump:
            self.session_epoch += 1
        self.pending_requests.clear()
        self.request_generations.clear()
        self.connected = self.financial_ready = self.locked = False
        if hasattr(self, "connection_panel"):
            self.show_connection_details(True)
        self.session_mode = None
        self.need_expiries = self.need_classes = self.preview_dirty = self.chain_dirty = False
        self.snapshot_data, self.positions_by_id = {}, {}
        self.orders_by_id = {}
        self.order_caption.set("未连接，订单显示已清空；已提交订单仍需在 TWS 管理。")
        self.edit_order_key = None
        self.expiry_symbol = None
        self.suppress_change = True
        self.expiry.set("")
        self.trading_class.set("")
        self.expiry_box.configure(values=())
        self.class_box.configure(values=())
        self.reference_input.set("")
        self.reference_price, self.reference_pending = None, False
        self.suppress_change = False
        self.clear_preview("未连接，旧预览已清空。")
        self.clear_chain("未连接，期权链已清空。")
        self._clear_tree(self.pos_tree)
        self._clear_tree(self.order_tree)
        self.position_note.set("未连接，持仓显示已清空；已提交订单仍需在 TWS 管理。")
        self.clear_position_display()
        self.clear_history_display()
        self.apply_fund_cards(build_fund_cards({"mode": MODES[self.mode.get()], "connected": False}))
        self.status.set(reason)
        window_title(self.root, f"IBKR 快捷期权 V{APP_VERSION} · 未连接")
        self.last_snapshot = self.last_funds = self.last_chain = self.last_preview = 0.0
        self.last_expiry_refresh = 0.0
        self.update_enabled()

    def mode_changed(self, *_):
        self.port.set(7496 if MODES[self.mode.get()] == "LIVE" else 7497)
        self.set_account_choices(())
        self.clear_session_view("模式已改变，请连接对应账户。")

    def audit_event(self, event, message="", **fields):
        journal = getattr(self, "usage_log", None)
        if journal is not None:
            metadata = dict(mode=getattr(self, "session_mode", None), symbol=getattr(self, "active_symbol", ""))
            metadata.update(fields)
            journal.record(event, message, **metadata)

    def log(self, text, *, event=None, level=None, **fields):
        text = sanitize(str(text))
        kind, inferred_level = message_kind(text)
        self.audit_event(event or kind, text, level=level or inferred_level, **fields)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        self.logs.append(text)
        self.logs[:] = self.logs[-1000:]
        self.log_box.configure(state="normal")
        line = f'{stamp}  {text}\n'
        if hasattr(self, "visible_log_lines"):
            self.visible_log_lines.extend(line.splitlines(keepends=True))
        self.log_box.insert("end", display(self.root, line))
        if int(self.log_box.index("end-1c").split(".")[0]) > 1000:
            self.log_box.delete("1.0", "201.0")
            if hasattr(self, "visible_log_lines"):
                del self.visible_log_lines[:200]
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def report_ui_exception(self, exc_type, exc, tb):
        text = "".join(traceback.format_exception(exc_type, exc, tb))
        if hasattr(self, "log_box"):
            self.log(text, event="ui_exception", level="ERROR")
            self.status.set("界面异常已记录，请查看运行日志。")
        else:
            self.audit_event("ui_exception", text, level="ERROR")

    def close_usage_log(self):
        if hasattr(self, "usage_handler"):
            logging.getLogger().removeHandler(self.usage_handler)
        if hasattr(self, "usage_log"):
            self.usage_log.close()

    def open_logs_folder(self):
        path = BASE / "logs"
        path.mkdir(exist_ok=True)
        os.startfile(str(path))

    def symbol_typed(self, *_):
        if self.suppress_change:
            return
        self.pending_symbol = self.symbol.get().strip().upper() != self.active_symbol
        if self.pending_symbol:
            self.reset_entry_prices()
            self.selected_conids = {}
        self.clear_preview("标的输入已改变，请按回车确认。" if self.pending_symbol else "等待重新预览。")
        if self.pending_symbol:
            self.clear_chain("标的输入尚未确认；按回车载入。")
        self.update_enabled()

    def commit_symbol(self, event=None, force=False):
        try:
            symbol = normalize_symbol(self.symbol.get())
        except ValueError as exc:
            self.pending_symbol = True
            self.status.set(str(exc))
            self.clear_preview("标的代码无效，请修改后按回车。")
            self.update_enabled()
            return
        changed, was_pending = symbol != self.active_symbol, self.pending_symbol
        self.suppress_change = True
        self.symbol.set(symbol)
        self.suppress_change = False
        self.pending_symbol = False
        if changed or force:
            self.reset_entry_prices()
            self.selected_conids = {}
            self.active_symbol, self.expiry_symbol = symbol, None
            self.suppress_change = True
            self.selection_mode.set("按档位")
            self.expiry.set("")
            self.trading_class.set("")
            self.expiry_box.configure(values=())
            self.class_box.configure(values=())
            if changed:
                self.reference_input.set("")
                self.reference_price, self.reference_pending = None, False
            self.suppress_change = False
            self.chain_offset = 0
            self.need_expiries = self.connected
            self.need_classes = self.preview_dirty = self.chain_dirty = False
            self.clear_preview("正在载入到期日与交易类别。" if self.connected else "连接后载入此标的。")
            self.clear_chain()
        elif was_pending:
            self.preview_dirty = self.connected
            self.chain_dirty = self.connected and bool(self.expiry.get())
        self.flatten_btn.configure(text=f"平全部 {self.active_symbol} 持仓")
        self.update_enabled()

    def expiry_changed(self, *_):
        if self.suppress_change:
            return
        self.reset_entry_prices()
        self.selected_conids = {}
        self.clear_preview("到期日已改变，等待交易类别。")
        self.suppress_change = True
        self.trading_class.set("")
        self.class_box.configure(values=())
        self.suppress_change = False
        self.need_classes = self.connected and bool(self.expiry.get())
        self.chain_offset = 0
        self.chain_dirty = self.preview_dirty = False
        self.clear_chain()
        self.update_enabled()

    def class_changed(self, *_):
        if self.suppress_change:
            return
        self.reset_entry_prices()
        self.selected_conids = {}
        self.chain_offset = 0
        self.chain_dirty = self.connected and bool(self.trading_class.get())
        self.clear_chain()
        self.changed()

    def moneyness_changed(self, *_):
        if self.suppress_change:
            return
        category = self.moneyness.get()
        if category != self.previous_moneyness:
            self.previous_moneyness = category
            self.suppress_change = True
            self.depth.set(default_depth(MONEYNESS[category]))
            self.suppress_change = False
        self.contract_input_changed()

    def changed(self, *_):
        if not self.suppress_change:
            self.clear_preview()
            self.preview_dirty = self.connected
            self.update_enabled()

    def percent_changed(self, *_):
        number = round(self.percent.get(), 2)
        self.percent_text.set(f"使用现金 {number:g}%")
        if hasattr(self, "percent_input"):
            old = self.suppress_change
            self.suppress_change = True
            self.percent_input.set(f"{number:g}")
            self.suppress_change = old
        if hasattr(self, "buy_btn"):
            self.changed()

    def set_percent(self, percent):
        self.budget_mode.set("比例 %")
        self.budget_mode_changed()
        self.percent.set(percent)
        self.percent_changed()
        self.exit_budget_match()

    def settings(self):
        if self.pending_symbol:
            raise ValueError("标的输入尚未确认，请按回车。")
        extras = self.extra_entry_settings()
        return dict(symbol=self.active_symbol, expiry=self.expiry.get(), trading_class=self.trading_class.get(), right=self.right.get(),
                    moneyness=MONEYNESS[self.moneyness.get()], depth=1 if extras["selection_mode"] == "strike" or MONEYNESS[self.moneyness.get()] == "ATM" else int(self.depth.get()),
                    slippage_pct=float(self.slippage.get()), commission_per_contract=float(self.fee.get()),
                    min_commission=float(self.min_fee.get()), max_contracts=int(self.cap.get()),
                    order_mode=ENTRY_MODES[self.entry_mode.get()], limit_price=extras["limit_prices"].get(self.right.get()),
                    rounding_mode="floor" if extras["selection_mode"] == "budget" else ROUNDING_MODES[self.rounding_mode.get()], **extras)

    def fingerprint(self):
        return json.dumps(self.settings(), sort_keys=True)

    def context(self):
        return {"session": self.session_epoch, "symbol": self.active_symbol, "expiry": self.expiry.get(),
                "trading_class": self.trading_class.get(), "reference_price": self.reference_price}

    def context_is_current(self, name, context):
        if not context or context.get("session") != self.session_epoch:
            return False
        if "generation" in context and context["generation"] != self.request_generations.get(name):
            return False
        if name in ("expirations", "trading_classes", "chain", "preview", "preview_pair") and (self.pending_symbol or context.get("symbol") != self.active_symbol):
            return False
        if name in ("trading_classes", "chain", "preview", "preview_pair") and context.get("expiry") != self.expiry.get():
            return False
        if name in ("chain", "preview", "preview_pair") and context.get("trading_class") != self.trading_class.get():
            return False
        if name in ("preview", "preview_pair"):
            try:
                return context.get("form") == self.fingerprint()
            except (ValueError, tk.TclError):
                return False
        if name == "prepare_cost_stop":
            return (bool(self.close_panel.grid_info()) and self.notebook.select() == str(self.position_tab)
                    and context.get("cost_form") == self.cost_stop_form())
        if name == "preview_add":
            return bool(self.position_add_dialog and context.get("add_form") == self.add_form_fingerprint())
        if name == "preview_modify":
            return context.get("modify_form") == self.modify_fingerprint()
        if name == "preview_close":
            return self.position_close_dialog is not None and context.get("close_form") == self.close_form_fingerprint()
        if name == "execution_history":
            return context.get("history_form") == self.history_fingerprint()
        return name != "chain" or (context.get("offset") == self.chain_offset
                                   and context.get("limit", self.chain_limit()) == self.chain_limit()
                                   and not self.reference_pending and context.get("reference_price") == self.reference_price)

    def set_account_choices(self, accounts):
        self.account.set("")
        self.account_box.configure(values=accounts)
        if accounts:
            self.account_picker.grid()
        else:
            self.account_picker.grid_remove()

    def connect(self):
        if self.has_pending(CONTROL_REQUESTS) or self.has_pending(ORDER_REQUESTS):
            return
        self.clear_session_view("正在核对连接参数。")
        try:
            args = dict(mode=MODES[self.mode.get()], host=self.host.get().strip(), port=int(self.port.get()), client_id=int(self.client_id.get()),
                        account=self.account.get())
            if self.account_box["values"] and not args["account"]:
                self.status.set("请选择本次要连接的 TWS 账户。")
                return
            self.set_account_choices(())
            self.submit("connect", **args)
        except ValueError as exc:
            self.status.set(str(exc))

    def disconnect(self):
        if self.connected and not self.has_pending({"disconnect"}):
            request_id = self.active_request
            self.clear_session_view("正在断开；已提交订单不会自动撤销。")
            self.submit("disconnect")
            if request_id is not None:
                self.worker.interrupt_read(request_id)

    def has_pending(self, names):
        return bool((self.active_job and self.active_job["name"] in names)
                    or any(name in names for name in self.pending_requests))

    def submit(self, name, *, background=False, **args):
        if name in ("connect", "disconnect", "auto_mid_stop", "cancel_order", "cancel_own", "preview_modify", "modify_order"):
            self.worker.request_auto_stop(args.get("order_id"))
        """Queue a user action once; read jobs coalesce to their newest context."""
        if name not in ("connect", "disconnect") and not self.connected:
            self.status.set("请先连接账户。")
            return False
        if name in ORDER_REQUESTS and self.has_pending(ORDER_REQUESTS | CONTROL_REQUESTS):
            return False
        if name in READ_REQUESTS and self.has_pending(CONTROL_REQUESTS):
            return False
        if background and self.busy:
            return False
        context = self.context()
        if name in ("preview", "preview_pair"):
            context["form"] = json.dumps(args["settings"], sort_keys=True)
        if name == "prepare_cost_stop":
            context["cost_form"] = self.cost_stop_form()
            context["loss_preset"] = args.pop("loss_preset", None)
        if name == "preview_add":
            context["add_form"] = self.add_form_fingerprint()
        if name == "preview_modify":
            context["modify_form"] = self.modify_fingerprint()
        if name == "preview_close":
            context["close_form"] = self.close_form_fingerprint()
        if name == "execution_history":
            context["history_form"] = self.history_fingerprint()
        if name == "chain":
            context["offset"] = args.get("offset", 0)
            context["limit"] = args.get("limit", self.chain_limit())
        # Identical clicks while the same request is running/queued are one job.
        queued = self.pending_requests.get(name)
        for job in ((queued,) if queued else (self.active_job,)):
            if (job and job["name"] == name and job["args"] == args
                    and all(job["context"].get(key) == value for key, value in context.items())):
                return True
        self.request_sequence += 1
        context["id"] = self.request_sequence
        context["background"] = background
        if name in READ_REQUESTS:
            generation = self.request_generations.get(name, 0) + 1
            self.request_generations[name] = context["generation"] = generation
        self.pending_requests[name] = dict(name=name, args=args, context=context, background=background)
        if not background:
            self.status.set("操作已排队，正在等待当前查询完成。" if self.busy else "正在处理…")
        self.dispatch_pending()
        self.update_enabled()
        return True

    def dispatch_pending(self):
        """Only one broker method runs; explicit trades precede queued reads."""
        if self.busy:
            return
        priority = lambda name: 0 if name in CONTROL_REQUESTS else 1 if name in ORDER_REQUESTS else 2
        while self.pending_requests:
            name = min(self.pending_requests, key=priority)
            job = self.pending_requests.pop(name)
            if not self.context_is_current(name, job["context"]):
                continue
            self.active_job = job
            self.active_request = job["context"]["id"]
            self.busy = True
            self.audit_event("request_dispatched", "请求交后台处理", action=name,
                             background=job["background"], request_id=job["context"]["id"],
                             symbol=job["context"].get("symbol", self.active_symbol),
                             mode=job["args"].get("mode", self.session_mode))
            self.inbox.put((name, job["args"], job["context"]))
            if not job["background"]:
                self.set_request_status(name)
            break

    def set_request_status(self, name):
        labels = {"preview_add": "正在核对持仓加仓预览…", "add_confirm": "正在核对并提交加仓委托…", "connect": "正在连接账户…", "disconnect": "正在断开…", "refresh_funds": "正在刷新资金…", "preview": "正在匹配合约并核对资金与行情…", "chain": "正在读取期权链…", "expirations": "正在读取到期日…", "trading_classes": "正在读取交易类别…", "buy": "正在核对并提交买单…", "close": "正在提交平仓单…", "close_all": "正在逐笔平仓…"}
        if name != "snapshot":
            self.status.set(labels.get(name, "正在处理…"))

    def request_preview(self, *, background=False):
        if not self.connected or self.need_expiries or self.need_classes or self.pending_symbol or self.has_pending(ORDER_REQUESTS | CONTROL_REQUESTS):
            return
        if not self.financial_ready or self.locked:
            self.status.set("资金未完成核验或发单已锁定；仍可查看期权链。")
            self.preview_dirty = False
            return
        try:
            settings = self.settings()
        except (ValueError, tk.TclError) as exc:
            self.status.set(f"请输入有效参数：{exc}")
            self.preview_dirty = False
            return
        if not settings["expiry"] or not settings["trading_class"]:
            return
        # A still-valid preview remains usable while its replacement downloads.
        # Parameter edits already clear it through changed().
        if not self.preview_data:
            self.match_text.set("正在匹配合约…")
        self.preview_dirty = False
        self.last_preview = time.monotonic()
        self.submit("preview_pair", background=background, settings=settings)

    def request_chain(self, *, background=False):
        if not self.connected or self.pending_symbol or self.reference_pending or self.need_expiries or self.need_classes:
            return
        if self.expiry.get() and self.trading_class.get():
            self.chain_dirty = False
            self.last_chain = time.monotonic()
            self.submit("chain", background=background, symbol=self.active_symbol, expiry=self.expiry.get(), trading_class=self.trading_class.get(),
                        offset=self.chain_offset, limit=self.chain_limit(), reference_price=self.reference_price,
                        visible_strikes=self.visible_chain_strikes())

    def reference_typed(self, *_):
        if not self.suppress_change:
            self.reference_pending = True
            self.chain_status.set("备用参考价尚未确认；按回车应用，或清空后按回车取消。")
            self.update_enabled()

    def commit_reference(self, event=None):
        text = self.reference_input.get().strip()
        try:
            value = float(text) if text else None
            if value is not None and (not math.isfinite(value) or value <= 0):
                raise ValueError
        except (ValueError, OverflowError):
            self.reference_pending = True
            self.chain_status.set("备用参考价必须为有效正数；不参与下单。")
            self.update_enabled()
            return
        changed = value != self.reference_price
        was_pending = self.reference_pending
        self.reference_price, self.reference_pending = value, False
        if changed:
            self.chain_offset = 0
            self.clear_chain("备用参考价已更新，正在重新载入；仅用于查看期权链。")
            self.chain_dirty = self.connected
            self.request_chain()
        elif was_pending:
            self.chain_dirty = self.connected
            self.chain_status.set("备用参考价已确认，等待期权链更新。")
            self.request_chain()
        self.update_enabled()

    def chain_limit(self):
        return self.committed_chain_limit

    def chain_width_changed(self, *_):
        try:
            value = parse_chain_limit(self.chain_width.get())
        except ValueError as exc:
            self.status.set(str(exc))
            return
        if value == self.committed_chain_limit:
            return
        self.committed_chain_limit = value
        self.chain_offset = 0
        self.clear_chain("显示档数已改变，正在按参考价重新载入。")
        self.chain_dirty = self.connected
        self.request_chain()

    def page_chain(self, delta):
        if self.connected:
            self.chain_offset = 0 if delta is None else self.chain_offset + delta
            self.chain_center_pending = delta is None
            self.chain_dirty = True
            self.request_chain()

    def tab_changed(self, *_):
        self.update_focus_title()
        if hasattr(self, "notebook") and self.notebook.select():
            self.audit_event("page_view", self.notebook.tab(self.notebook.select(), "text"))
        if hasattr(self, "chain_tab") and self.connected and self.notebook.select() == str(self.chain_tab):
            self.chain_dirty = True
        if hasattr(self, "history_tab") and self.connected and self.notebook.select() == str(self.history_tab):
            self.history_dirty = True
        if hasattr(self, "position_tab") and self.connected and self.notebook.select() == str(self.position_tab):
            self.last_snapshot = 0

    def buy(self):
        preview = self.preview_data
        if not preview or self.has_pending(ORDER_REQUESTS | CONTROL_REQUESTS) or not self.financial_ready or self.locked:
            return
        try:
            valid = self.fingerprint() == self.preview_fingerprint and time.time() - preview["created_at"] <= 10
        except (ValueError, tk.TclError):
            valid = False
        if not valid:
            self.clear_preview("预览已失效，等待重新匹配。")
            self.preview_dirty = True
            return
        self.clear_preview("买入请求正在处理，请查看订单状态。")
        self.log(f'用户点击买入：{preview["contract"]["local_symbol"]}，预览 {preview["quantity"]} 张，模式 {self.session_mode}')
        self.submit("buy", token=preview["token"])

    def mutate(self, name, **args):
        if self.connected and not self.has_pending(ORDER_REQUESTS | CONTROL_REQUESTS) and not self.locked:
            self.clear_preview("账户操作正在处理，旧预览已作废。")
            self.log(f"用户操作：{name} {args}")
            self.submit(name, **args)

    def close_selected(self, percent):
        selected = self.pos_tree.selection()
        if selected:
            position = self.positions_by_id.get(selected[0], {})
            extra = {"protection_id": position["protection_id"]} if position.get("protection_id") else {}
            self.mutate("close", conid=int(selected[0]), percent=percent, **extra)
        else:
            self.status.set("请先在持仓表选择一行。")

    def close_active_symbol(self):
        if not self.pending_symbol:
            self.mutate("close_all", symbol=self.active_symbol)

    def render_snapshot(self, data):
        if not data.get("connected", False):
            self.clear_session_view("连接已断开；旧账户资金、预览和行情已清空。")
            return
        if not self.connected and hasattr(self, "connection_panel"):
            self.show_connection_details(False)
        self.connected, self.snapshot_data = True, data
        self.session_mode = data.get("mode")
        self.locked = bool(data.get("locked", False))
        financial = data.get("financial") or {}
        self.financial_ready = bool(financial.get("ready", False)) and not self.locked
        self.apply_fund_cards(build_fund_cards(data))
        if self.locked:
            self.fund_detail.set("发单结果需要在 TWS 核对，当前已锁定发单；不会自动重发。")
        if not self.financial_ready:
            self.clear_preview("账户已连接，资金尚未完成核验。可查看期权链，暂不能开仓。" if not self.locked else "发单已锁定，请在 TWS 核对订单。")
        window_title(self.root, f'IBKR 快捷期权 V{APP_VERSION} · {self.session_mode} · {data.get("account", "")}')
        self.render_positions(data.get("positions", []))
        self.position_note.set(f"平全部只处理 {self.active_symbol}；多头卖出平仓、空头买入回补。盈亏按标明来源估值，未扣未来平仓费；最新成交价可能陈旧。止盈/止损为独立挂单，不表示 OCO。")
        self.render_orders(data.get("orders", []))

    def render_orders(self, orders):
        selected_orders = self.order_tree.selection()
        order_view = self.order_tree.yview()
        self._clear_tree(self.order_tree)
        self.orders_by_id = {}
        choice = self.order_filter.get() if hasattr(self, "order_filter") else "全部"
        rows = [(o, order_display(o)) for o in orders]
        rows.sort(key=lambda pair: (pair[1]["active"], pair[0].get("placed_at") or pair[0].get("observed_at") or 0,
                                    pair[0]["order_id"]), reverse=True)
        for order, view in rows:
            if choice != "全部" and view["active"] != (choice == "挂单"):
                continue
            key = f'{order.get("client_id", "")}|{order["order_id"]}'
            if not order.get("owned", True):
                key += f'|{order.get("perm_id", 0)}'
            self.orders_by_id[key] = order
            self.order_tree.insert("", "end", iid=key, values=(order["order_id"], view["contract"], view["side"],
                order.get("order_type") or "—", order_number_text(order.get("limit_price")),
                order_parameters_text(order), order.get("tif") or "—", f'{order["filled"]:g} / {order["quantity"]:g}',
                f'{order["remaining"]:g}', view["status"], order_number_text(order.get("avg_fill_price") or None),
                view["time"], view["dte"], view["book"], view["last"], view["source"], view["midpoint"]))
        if selected_orders and selected_orders[0] in self.orders_by_id:
            self.order_tree.selection_set(selected_orders[0])
        if order_view:
            self.order_tree.yview_moveto(order_view[0])
        self.order_selected()

    def render_preview(self, preview):
        c = preview["contract"]
        order_type = preview.get("order_type", "MKT")
        execution = "市价" if order_type == "MKT" else f'限价 ≤ {self.price_text(preview.get("limit_price"))}'
        if preview.get("price_mode") == "MID":
            execution = "中间价固定" + execution
        session = preview.get("session_label") or "按合约交易时段核验"
        source = preview.get("spot_source") or ""
        if preview.get("spot_estimated"):
            source = "估算参考，仅用于选档 · " + source
        self.match_text.set(f'{c["local_symbol"]}\n到期 {c["expiry"]}  {c["right"]}  行权价 {c["strike"]:g}  乘数 {c["multiplier"]}\n'
                            f'标的参考 {preview["spot"]:.3f} {source}\n买一 {preview["bid"]:.3f} / 卖一 {preview["ask"]:.3f}  [{preview["quote_type"]}]\n'
                            f'比例预算 ${preview["budget"]:,.2f}  →  可买 {preview["quantity"]} 张'
                            f'（{"四舍五入" if preview.get("rounding_mode", "nearest") == "nearest" else "向下取整"}，原始约 {preview.get("raw_quantity", preview["quantity"]):.4f}）\n'
                            f'权利金约 ${preview["estimated_premium"]:,.2f}  |  手续费预留 ${preview["fee_reserve"]:,.2f}\n'
                            f'权利金占现金 {preview.get("actual_percent", 0):.1f}% · 与比例预算差 ${preview.get("budget_difference", 0):+,.2f}\n'
                            f'含滑点预留 ${preview["buffered_cost"]:,.2f}  ·  {time.strftime("%H:%M:%S", time.localtime(preview["created_at"]))} 预览\n'
                            f'{session} · {execution} · DAY')
        self.buy_btn.configure(text=f'{execution}买入 {preview["quantity"]} 张')

    @staticmethod
    def price_text(value):
        try:
            number = float(value)
            if number != number or number < 0 or abs(number) == float("inf"):
                return "—"
            return f"{number:.3f}".rstrip("0").rstrip(".")
        except (TypeError, ValueError):
            return "—"

    def render_chain(self, data):
        # Keep stable row IDs and mutate values in place. A stream refresh must
        # not repeatedly delete/reinsert the table and reset the user's scroll.
        tree = self.chain_tree
        old_ids = tree.get_children()
        old_view = tree.yview()
        old_fraction = old_view[0] if old_view else 0.0
        anchor = old_ids[min(len(old_ids) - 1, int(old_fraction * len(old_ids)))] if old_ids else None
        reference = data.get("spot")
        entries = [] if data.get("awaiting_reference") else chain_rows_with_reference(data.get("rows", []), reference)
        self.chain_rows_by_id = {entry["iid"]: entry["row"] for entry in entries if entry["kind"] == "contract"}
        if hasattr(tree, "set_context"):
            tree.set_context(self.chain_rows_by_id, data.get("spot_ref"))
        wanted = {entry["iid"] for entry in entries}
        for iid in old_ids:
            if iid not in wanted:
                tree.delete(iid)
        selected_strike = self.preview_data["contract"]["strike"] if self.preview_data else None
        existing = set(tree.get_children())
        for index, entry in enumerate(entries):
            if entry["kind"] == "reference":
                values = ("────", "────", "────", "参考价  →", self.price_text(reference), "────", "────", "────", "←  参考价")
                tags = ("reference",)
            else:
                row = entry["row"]
                call, put = row.get("call") or {}, row.get("put") or {}
                values = (self.price_text(call.get("bid")), self.price_text(call.get("ask")), self.price_text(call.get("last")),
                          entry["call_zone"] + " · " + quote_age_text(call), self.price_text(entry["strike"]),
                          self.price_text(put.get("bid")), self.price_text(put.get("ask")), self.price_text(put.get("last")),
                          entry["put_zone"] + " · " + quote_age_text(put))
                tags = ("matched",) if entry["strike"] == selected_strike else ()
            if entry["iid"] in existing:
                tree.item(entry["iid"], values=values, tags=tags)
                tree.move(entry["iid"], "", index)
            else:
                tree.insert("", index, iid=entry["iid"], values=values, tags=tags)
        self.chain_has_prev, self.chain_has_next = bool(data.get("has_prev")), bool(data.get("has_next"))
        self.chain_offset = int(data.get("offset", self.chain_offset))
        key = (data.get("symbol"), data.get("expiry"), data.get("trading_class"), self.chain_limit(), self.chain_offset)
        new_ids = tuple(entry["iid"] for entry in entries)
        if entries:
            if self.chain_center_pending or not old_ids:
                visible = tree.visible_rows() if hasattr(tree, "visible_rows") else max(1, (tree.winfo_height() - 48) // self.chain_row_height)
                tree.yview_moveto(centered_chain_fraction(new_ids.index("reference-price"), len(new_ids), visible))
                self.chain_center_pending = False
            elif self.chain_render_key != key:
                tree.yview_moveto(0.0)  # Explicit pagination starts at the new page.
            elif old_ids != new_ids:
                # If the reference crosses a strike, preserve the top contract,
                # rather than dragging a manually scrolled view back to ATM.
                tree.yview_moveto(new_ids.index(anchor) / len(new_ids) if anchor in new_ids else old_fraction)
        else:
            self.chain_center_pending = True
        self.chain_render_key = key
        demo = " · 演示虚构" if data.get("mode") == "DEMO" else ""
        metadata = data.get("spot_ref") or {}
        metadata = metadata if isinstance(metadata, dict) else {}
        source = metadata.get("kind") or metadata.get("label") or data.get("spot_kind") or metadata.get("source") or ""
        if metadata.get("estimated") and metadata.get("basis_date"):
            source += " · 校准日 " + str(metadata["basis_date"])
        self.chain_caption.set(f'{data.get("symbol", self.active_symbol)} {data.get("expiry", "")} {data.get("trading_class", "")} · 参考 {self.price_text(reference)} {source}{demo}')
        if hasattr(self, "chain_heading"):
            expiry = str(data.get("expiry", ""))
            expiry = f"{expiry[4:6]}/{expiry[6:8]}" if len(expiry) == 8 else expiry
            count = sum(entry["kind"] == "contract" for entry in entries)
            self.chain_heading.set(f'{data.get("symbol", self.active_symbol)} · {expiry} · {data.get("trading_class", "")} · {count} 档')
            if data.get("awaiting_reference") or data.get("error") or not entries:
                self.chain_status_label.grid()
            else:
                self.chain_status_label.grid_remove()
        if data.get("awaiting_reference") or (not entries and self.price_text(reference) == "—"):
            self.chain_status.set(str(data.get("reason") or metadata.get("error") or metadata.get("reason") or "正在等待可用参考价；未按任意行权价居中。"))
        elif data.get("error"):
            self.chain_status.set(str(data["error"]))
        else:
            count = sum(entry["kind"] == "contract" for entry in entries)
            status = f"显示 {count} 档；实时订阅优先可见区域（最多 80 腿）；双击 Call / Put 报价选合约。" if count else "该页没有可显示的有效合约。"
            if metadata.get("error"):
                status += " 参考价说明：" + str(metadata["error"])
            self.chain_status.set(status)

    def update_enabled(self):
        control_pending = self.has_pending(CONTROL_REQUESTS)
        order_pending = self.has_pending(ORDER_REQUESTS)
        ready = self.connected and not control_pending
        self.entry_mode_box.configure(state="readonly" if not order_pending and not control_pending else "disabled")
        self.entry_limit_entry.configure(state="normal" if self.entry_mode.get() == "限价" and not order_pending and not control_pending else "disabled")
        for entry in self.entry_limit_entries.values():
            entry.configure(state="normal" if self.entry_mode.get() == "限价" and not order_pending and not control_pending else "disabled")
        self.exact_strike_entry.configure(state="normal" if self.selection_mode.get() == "指定行权价" else "disabled")
        self.seller_quantity_entry.configure(state="normal" if OPEN_SIDES[self.open_side.get()] == "SELL" and not order_pending else "disabled")
        edit_ready = self.order_edit_ready()
        self.modify_btn.configure(state="normal" if edit_ready else "disabled")
        self.cancel_selected_btn.configure(state="normal" if self.order_edit_ready(cancel=True) else "disabled")
        self.modify_mode_box.configure(state="readonly" if edit_ready else "disabled")
        self.modify_limit_entry.configure(state="normal" if edit_ready and self.modify_mode.get() == "限价" else "disabled")
        for widget, state in self.connect_controls:
            widget.configure(state=state if not self.connected and not control_pending and not order_pending else "disabled")
        self.connect_btn.configure(state="normal" if not self.connected and not control_pending and not order_pending else "disabled")
        self.disconnect_btn.configure(state="normal" if self.connected and not self.has_pending({"disconnect"}) else "disabled")
        self.funds_btn.configure(state="normal" if ready else "disabled")
        self.expiry_box.configure(state="readonly" if self.connected and not self.pending_symbol else "disabled")
        self.class_box.configure(state="readonly" if self.connected and self.expiry.get() and not self.pending_symbol else "disabled")
        self.depth_box.configure(state="disabled" if MONEYNESS[self.moneyness.get()] == "ATM" or self.selection_mode.get() == "指定行权价" else "normal")
        self.preview_btn.configure(state="normal" if ready and not order_pending and self.financial_ready and not self.locked and not self.pending_symbol else "disabled")
        if hasattr(self, "budget_summary"):
            summary = self.budget_summary_text()
            if self.budget_summary.get() != summary:
                self.budget_summary.set(summary)
        if hasattr(self, "budget_match_button"):
            searching = self.selection_mode.get() == BUDGET_SELECTION
            self.preview_btn.configure(text="重新找档" if searching else "匹配 / 刷新")
            can_search = str(self.preview_btn["state"]) == "normal" and OPEN_SIDES[self.open_side.get()] == "BUY"
            self.budget_match_button.configure(text="退出预算找档" if searching else "按预算找档",
                                               state="normal" if searching or can_search else "disabled")
        can_buy = bool(ready and not order_pending and self.financial_ready and not self.locked and not self.pending_symbol and self.preview_data
                       and self.preview_data.get("quantity", 0) > 0 and time.time() - self.preview_data["created_at"] <= 10)
        self.buy_btn.configure(state="normal" if can_buy else "disabled")
        for side, button in self.side_buttons.items():
            preview = self.side_previews.get(side)
            can_submit = (ready and not order_pending and not self.entry_confirmation_open and self.financial_ready
                          and not self.locked and not self.pending_symbol and preview
                          and preview.get("quantity", 0) > 0 and time.time() - preview["created_at"] <= 10)
            button.configure(state="normal" if can_submit else "disabled")
        for button in self.close_buttons + [self.cancel_btn]:
            button.configure(state="normal" if ready and not order_pending and not self.locked else "disabled")
        selected_position = self.pos_tree.selection()
        position = self.positions_by_id.get(selected_position[0], {}) if selected_position else {}
        short_position = position.get("quantity", 0) < 0
        close_ready = bool(ready and not order_pending and not self.locked and position and not short_position
                           and not position.get("close_pending"))
        self.close_order_btn.configure(state="normal" if close_ready and not self.close_confirmation_open else "disabled",
                                       text="空头请用上方快捷买回平仓" if short_position else "提交所选卖单")
        self.close_type_box.configure(state="readonly" if close_ready else "disabled")
        self.close_percent_entry.configure(state="normal" if close_ready else "disabled")
        self.close_tif_box.configure(state="readonly" if close_ready else "disabled")
        visible = close_order_fields(CLOSE_TYPES[self.close_type.get()])
        for field, (_, widget) in self.close_field_widgets.items():
            widget.configure(state=("readonly" if field == "trail_mode" else "normal") if close_ready and field in visible else "disabled")
        self.flatten_btn.configure(state="normal" if ready and not order_pending and not self.locked and not self.pending_symbol else "disabled")
        chain_ready = ready and not self.pending_symbol and not self.reference_pending and bool(self.expiry.get()) and bool(self.trading_class.get())
        self.chain_refresh_btn.configure(state="normal" if chain_ready else "disabled")
        self.chain_width_box.configure(state="normal" if self.connected and not self.pending_symbol else "disabled")
        self.chain_atm_btn.configure(state="normal" if chain_ready else "disabled")
        self.chain_prev_btn.configure(state="normal" if chain_ready and self.chain_has_prev else "disabled")
        self.chain_next_btn.configure(state="normal" if chain_ready and self.chain_has_next else "disabled")
        self.update_position_controls(ready, order_pending)
        if hasattr(self, "auto_mid_start_btn"):
            order = self.selected_order() or {}
            auto_ready = (ready and not order_pending and not self.locked and order.get("modifiable")
                          and order.get("order_type") == "LMT" and not order.get("auto_mid_active"))
            self.auto_mid_start_btn.configure(state="normal" if auto_ready else "disabled")
            self.auto_mid_stop_btn.configure(state="normal" if self.connected and order.get("auto_mid_active") else "disabled")
        if hasattr(self, "history_refresh_btn"):
            for button in (self.history_refresh_btn, self.history_import_btn):
                button.configure(state="normal" if ready and not order_pending else "disabled")
        if hasattr(self, "flex_sync_btn"):
            self.flex_sync_btn.configure(state="normal" if self.connected and self.session_mode != "DEMO" and not self.flex_busy else "disabled")

    def handle_result(self, name, result, error, context):
        if name == "flex_progress":
            self.handle_flex_progress(result, context)
            return
        if name == "flex_download":
            self.handle_flex_download(result, error, context)
            return
        if context and context.get("id") == self.active_request:
            self.busy, self.active_request, self.active_job = False, None, None
        if not self.context_is_current(name, context):
            return
        if name == "import_flex_reports":
            if not self.finish_flex_import(result, error):
                self.history_dirty = True
                if error:
                    self.log(error, event="request_error", level="ERROR", action=name)
                return
        if name in ("preview", "preview_pair", "preview_modify", "preview_close", "preview_add") and self.has_pending(ORDER_REQUESTS):
            return  # Do not replace the user's clicked preview during its queued trade.
        now = time.monotonic()
        if name == "snapshot":
            self.last_snapshot = now
        elif name == "refresh_funds":
            self.last_funds = now
        elif name == "chain":
            self.last_chain = now
        elif name in ("preview", "preview_pair"):
            self.last_preview = now
        if error:
            self.log(f"{name}：{error}", event="request_error", level="ERROR", action=name,
                     background=bool(context and context.get("background")))
            if name in ORDER_REQUESTS:
                self.last_snapshot = self.last_funds = 0.0
                self.clear_preview("账户操作需要重新核验，正在刷新订单与资金。")
            if name == "connect":
                self.clear_session_view(f"连接失败：{error}")
            elif name == "snapshot":
                self.clear_session_view(f"连接状态不可确认：{error}")
            elif name in ("preview", "preview_pair"):
                self.clear_preview("暂不可开仓\n" + error)
                self.status.set(error)
            elif name == "chain":
                self.chain_status.set("期权链更新失败，表内如有数值为上次快照：" + error)
                self.status.set(error)
            elif name in ("prepare_cost_stop", "raise_cost_stop", "set_breakeven_stop", "close") and hasattr(self, "protection_feedback"):
                self.protection_feedback.set(error)
                self.status.set(error)
            elif name in ("preview_add", "add_confirm") and self.position_add_dialog:
                self.position_add_changed()
                from privacy_view import mask_money
                self.position_add_dialog["text"].set(mask_money(error) if getattr(self, "bean_mode", False) else error)
                self.status.set(error)
            elif name in ("preview_close", "close_confirm") and self.position_close_dialog:
                from privacy_view import mask_money
                self.position_close_dialog["text"].set(mask_money(error) if getattr(self, "bean_mode", False) else error)
                self.position_close_dialog["preview"] = None
                self.position_close_dialog["submit_btn"].configure(text="等待新的平仓预览", state="disabled")
                self.status.set(error)
            elif name in ("import_execution_history", "import_flex_reports"):
                self.set_flex_feedback("本次报表未导入。" + error)
                self.status.set(error)
            elif name == "execution_history":
                self.history_coverage.set("记录刷新失败，当前列表如有记录为此前结果：" + error)
                self.status.set(error)
            elif name == "expirations":
                self.need_expiries = False
                self.clear_chain("账户已连接；到期日载入失败：" + error)
                self.status.set(error + "；可点击“重新载入标的”重试。")
            elif name == "trading_classes":
                self.need_classes = False
                self.clear_chain("账户已连接；交易类别载入失败：" + error)
                self.status.set(error + "；可重新选择到期日重试。")
            else:
                if name == "refresh_funds":
                    self.financial_ready = False
                    self.clear_preview("资金刷新失败，暂不能开仓。")
                    self.fund_vars["order_cash"].set("暂停开仓")
                    self.fund_detail.set(error)
                self.status.set(error)
            return
        if name in ORDER_REQUESTS | CONTROL_REQUESTS | {"import_execution_history", "import_flex_reports"}:
            self.audit_event("request_returned", "后台操作已返回；不代表成交", action=name,
                             request_id=context.get("id") if context else None)
        if name in ("connect", "snapshot", "refresh_funds", "refresh_positions"):
            was_ready = self.financial_ready
            self.render_snapshot(result)
            if name == "connect" and result.get("account_choices"):
                self.set_account_choices(result["account_choices"])
                self.status.set("TWS 返回多个账户。请选择本次使用的账户，再点「连接 / 开始」。")
            elif name == "connect" and self.connected:
                self.last_snapshot = self.last_funds = now
                self.need_expiries = True
                self.clear_chain("账户已连接；正在载入到期日。")
                self.status.set("账户已连接，正在读取到期日。" if self.financial_ready else "账户已连接；资金待核验，可先查看期权链。")
            elif name == "refresh_funds" and self.connected:
                self.status.set("资金已更新。" if self.financial_ready else str((result.get("financial") or {}).get("error") or "资金待核验。"))
            elif name == "refresh_positions":
                self.status.set("持仓、委托和成交回报已重新核对。")
                self.history_dirty = True
            if self.financial_ready and not was_ready:
                self.preview_dirty = True
        elif name == "disconnect":
            self.clear_session_view("已断开；已提交订单仍需在 TWS 管理。")
        elif name == "expirations":
            self.need_expiries, self.expiry_symbol = False, self.active_symbol
            self.last_expiry_refresh = now
            old_expiry = self.expiry.get()
            self.suppress_change = True
            self.expiry_box.configure(values=result)
            self.expiry.set(self.expiry.get() if self.expiry.get() in result else (result[0] if result else ""))
            self.suppress_change = False
            if old_expiry == self.expiry.get() and self.trading_class.get():
                return  # Periodic expiry refresh must not reset the user's form.
            self.reset_entry_prices()
            self.selected_conids = {}
            self.clear_preview("到期日已更新，正在载入可交易合约。")
            self.clear_chain("到期日已更新，等待交易类别。")
            self.need_classes = bool(result)
            self.status.set("到期日已载入，默认最近到期日，请核对。" if result else "该标的没有可用到期日。")
        elif name == "trading_classes":
            self.need_classes = False
            self.suppress_change = True
            self.class_box.configure(values=result)
            preferred = "SPXW" if self.active_symbol == "SPX" and "SPXW" in result else (result[0] if result else "")
            self.trading_class.set(self.trading_class.get() if self.trading_class.get() in result else preferred)
            self.suppress_change = False
            self.chain_offset = 0
            self.chain_dirty = self.preview_dirty = bool(result)
            self.status.set("交易类别已载入，请核对类别和到期日。" if result else "该到期日没有可用交易类别。")
        elif name == "chain":
            self.render_chain(result)
            self.status.set("等待可用参考价，详见期权链状态。" if result.get("awaiting_reference") else "期权链已更新；行情缺失会明确标注。")
        elif name == "preview":
            self.preview_data, self.preview_fingerprint = result, context["form"]
            self.render_preview(result)
            self.status.set("预览就绪，点击买入前会再次核对资金和行情。")
        elif name == "preview_pair":
            for side, preview in result.items():
                if isinstance(preview, dict) and preview.get("error") and not preview.get("scan_pending"):
                    self.audit_event("preview_blocked", f'{side}：{preview["error"]}', level="WARNING",
                                     action=name, background=bool(context and context.get("background")))
            self.render_pair(result, context)
        elif name == "preview_modify":
            self.confirm_modify(result, context)
        elif name == "prepare_cost_stop":
            self.close_spec_values["stop_price"].set(str(result["stop_price"]))
            self.protection_feedback.set(f'成本止损触发价 {result["stop_price"]:g}，买一 {result["bid"]:g}。')
            loss = (context or {}).get("loss_preset")
            if loss is None:
                self.submit_selected_close_order(cost_guard=result)
            elif (loss in range(10, 71, 10) and result["multiple"] == (100-loss)/100
                    and self.selected_position().get("conid") == result["conid"]):
                if result.get("order_id"):
                    self.mutate("raise_cost_stop", cost_guard=result)
                else:
                    self.mutate("close", conid=result["conid"], percent=100,
                                order_spec=dict(type="STP", tif="DAY", stop_price=result["stop_price"]),
                                cost_guard=result)
        elif name == "preview_add":
            self.render_position_add(result)
        elif name == "preview_close":
            self.render_position_close(result)
        elif name == "execution_history":
            self.render_history(result)
        elif name in ("import_execution_history", "import_flex_reports"):
            message = (f'报表已导入：新增 {result["imported"]} 笔成交，匹配已有成交 {result["duplicates"]} 笔。'
                       f'记账记录 {result.get("book_rows", 0)} 行，本次新增 {result.get("new_book_entries", 0)} 行。')
            if result.get("skipped"):
                message += f' 跳过 {result["skipped"]} 行，其中 {result.get("missing_execution_ids", 0)} 行缺少执行编号。'
            if result.get("coverage_note"):
                message += " " + result["coverage_note"]
            self.set_flex_feedback(message)
            self.status.set(message)
            self.log(message, event="flex_import", action=name)
            self.history_dirty = True
            self.request_history()
        else:
            self.log(f"{name} 返回：{json.dumps(result, ensure_ascii=False, default=str)}")
            self.status.set("操作已返回，请查看订单状态；已提交不代表已成交。")
            if isinstance(result, dict) and result.get("closed_by_stop"):
                self.status.set("止损成交已达到本次平仓张数。")
            if name == "set_breakeven_stop":
                if result.get("closed_by_stop"):
                    message = "旧止损已平掉全部持仓。"
                elif result.get("stop_unchanged"):
                    message = "现有止损已按当前成本覆盖全部持仓。"
                else:
                    message = f'保本止损 #{result["order_id"]} · {result["status"]}'
                self.status.set(message)
                self.protection_feedback.set(message)
            if name == "add_confirm":
                self.dismiss_position_add()
            if name == "close_confirm":
                self.dismiss_position_close()
            if name == "close" and hasattr(self, "protection_feedback") and isinstance(result, dict) and "order_id" in result:
                self.protection_feedback.set(f'订单 {result.get("order_id", "—")} · {result.get("status", "待回报")}')
            if name in ("modify_order", "raise_cost_stop") and isinstance(result, dict):
                labels = {"amend_acknowledged": "已收到新限价回报，请查看订单状态。",
                          "amend_requested": "改单已请求，尚未确认新价格；请在 TWS 核对，勿重复提交。",
                          "replacement_submitted": "原单已撤销，已提交剩余数量的新订单；请查看成交状态。",
                          "filled_before_replace": "原单在撤单期间已全部成交，没有再发新单。"}
                self.status.set(labels.get(result.get("modification"), "改单操作已返回，请核对订单类型、价格和剩余数量。"))
            self.last_snapshot = self.last_funds = 0.0
            self.preview_dirty = True
            if name in ORDER_REQUESTS:
                self.clear_preview("账户操作已返回，正在重新核验资金并匹配。")
                self.history_dirty = True
                if name != "set_breakeven_stop":
                    self.notebook.select(self.order_tab)

    def poll(self):
        if hasattr(self, "usage_status"):
            journal = self.usage_log
            self.usage_status.set(journal.last_error or ("自动日志：" + str(BASE / "logs") + "\n汇总更新：" + (journal.last_refresh or "初始化中") + " · 每分钟自动更新"))
        try:
            while True:
                item = self.outbox.get_nowait()
                name, result, error = item[:3]
                context = item[3] if len(item) > 3 else None
                if name == "log":
                    self.log(result)
                else:
                    self.handle_result(name, result, error, context)
        except queue.Empty:
            pass
        self.dispatch_pending()
        if getattr(self, "position_close_dialog", None) or getattr(self, "position_add_dialog", None):
            # Give an open close ticket the worker before launching another chain or entry preview.
            self.update_position_controls(self.connected and not self.has_pending(CONTROL_REQUESTS), self.has_pending(ORDER_REQUESTS))
            if self.connected and not self.busy and time.monotonic() - self.last_snapshot >= 2:
                self.last_snapshot = time.monotonic()
                self.submit("snapshot", background=True)
            self.root.after(200, self.poll)
            return
        if self.connected and not self.busy:
            now = time.monotonic()
            from datetime import datetime
            from zoneinfo import ZoneInfo
            today = datetime.now(ZoneInfo("America/New_York")).date()
            if today != self.expiry_day:
                self.expiry_day = today
                self.need_expiries = True
                self.clear_preview("纽约日期已变化，正在更新到期日。")
            if self.need_expiries and not self.pending_symbol:
                self.submit("expirations", background=True, symbol=self.active_symbol)
            elif self.need_classes and not self.pending_symbol:
                self.submit("trading_classes", background=True, symbol=self.active_symbol, expiry=self.expiry.get())
            elif now - self.last_expiry_refresh >= 60 and not self.pending_symbol:
                self.last_expiry_refresh = now
                self.submit("expirations", background=True, symbol=self.active_symbol)
            elif now - self.last_snapshot >= 2:
                self.last_snapshot = now
                self.submit("snapshot", background=True)
            elif now - self.last_funds >= 15:
                self.last_funds = now
                self.submit("refresh_funds", background=True)
            elif (hasattr(self, "history_tab") and self.notebook.select() == str(self.history_tab)
                  and (self.history_dirty or now - self.last_history >= 5)):
                self.request_history(background=True)
            elif (self.financial_ready and not self.locked and not self.pending_symbol and self.expiry.get() and self.trading_class.get()
                  and (self.preview_dirty or (self.auto.get() and now - self.last_preview >= 5))):
                self.request_preview(background=True)
            elif (not self.pending_symbol and not self.reference_pending and self.expiry.get() and self.trading_class.get()
                  and (self.chain_dirty or (self.notebook.select() == str(self.chain_tab) and now - self.last_chain >= 1))):
                self.request_chain(background=True)
        self.update_enabled()
        self.root.after(100, self.poll)

    def shutdown(self):
        self.cancel_flex()
        self.worker.request_auto_stop()
        self.pending_requests.clear()
        if self.active_request is not None:
            self.worker.interrupt_read(self.active_request)
        self.inbox.put(("shutdown", {}, None))
        self.close_usage_log()
        self.root.destroy()


def main():
    logging.basicConfig(level=logging.WARNING)
    root = tk.Tk()
    try:
        App(root)
        root.mainloop()
    except Exception:
        logging.exception("程序未处理异常")
        raise
    finally:
        if hasattr(root, "usage_handler"):
            logging.getLogger().removeHandler(root.usage_handler)
        if hasattr(root, "usage_journal"):
            root.usage_journal.close()


if __name__ == "__main__":
    main()
