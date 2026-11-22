"""Date-filtered execution history backed by the broker's local audit store."""
import json
import time
import threading
import tkinter as tk
from collections import defaultdict
from datetime import timedelta
from math import ceil
import localized_widgets as ttk
from tkinter import filedialog

from positions_ui import number
from contract_display import pnl_color
from popup_position import PointerPopup, messagebox

PERIODS = {"今天": "today", "最近 7 日": "7d", "最近 30 日": "30d", "最近 60 日": "60d", "最近 90 日": "90d", "本月至今": "mtd", "年初至今": "ytd"}
TIMEZONES = {"北京时间": "Asia/Shanghai", "纽约时间": "America/New_York"}
CUSTOM_PERIOD = "自选日期…"


def history_money(value, currency="USD", signed=False):
    text = number(value, signed=signed)
    return text if text == "—" else f'{text} {currency or "?"}'


def flex_retry_info(error):
    shared = getattr(error, "code", None) in ("1018", "1025")
    seconds = getattr(error, "retry_after", 0) if shared or getattr(error, "auto_retry", False) else 0
    return seconds, shared


class HistoryUiMixin:
    def init_history_ui_state(self):
        self.history_dirty = False
        self.last_history = 0.0
        self.history_data = None
        self.history_needs_sync = True
        self.flex_cancel = None
        self.flex_busy = False
        self.flex_config = {}
        self.flex_feedback = self.history_coverage_text = ""
        self.history_applied_period = "今天"
        self.history_applied_timezone = "北京时间"
        self.history_custom_dates = None
        self.flex_auto_timer = None
        self.flex_auto_pending = False
        self.history_tws_pending = False
        self.history_tws_refresh = None
        self.history_day_key = None
        self.flex_work = None
        self.flex_config_scope = None
        self.flex_windows = []
        self.flex_config_revision = 0
        self.flex_retry_at = 0.0
        self.flex_rate_failures = 0
        self.flex_retry_form = None

    def set_flex_feedback(self, message):
        self.flex_feedback = message
        if hasattr(self, "history_coverage"):
            self.history_coverage.set("\n".join(filter(None, (message, self.history_coverage_text))))

    def build_history_panel(self):
        self.history_tab = ttk.Frame(self.notebook, padding=6)
        self.notebook.insert(self.log_tab, self.history_tab, text="成交记录 / 盈亏")
        self.history_tab.columnconfigure(0, weight=1)
        self.history_tab.rowconfigure(3, weight=1)
        top = ttk.Frame(self.history_tab)
        top.grid(row=0, column=0, sticky="ew", pady=(0, 5))
        online = ttk.Frame(top)
        online.pack(side="bottom", fill="x", pady=4)
        ttk.Button(online, text="Flex 在线同步设置…", command=self.configure_flex).pack(side="left")
        self.flex_sync_btn = ttk.Button(online, text="重新同步所选范围", command=self.sync_flex)
        self.flex_sync_btn.pack(side="left", padx=5)
        ttk.Button(online, text="停止同步", command=self.cancel_flex).pack(side="left")
        ttk.Label(online, text="包含手机 / TWS / API 成交；Flex 查询请勾选全部品种与渠道。", foreground="#687680").pack(side="left", padx=6)
        self.history_view = self.var("卡片")
        view_box = ttk.Combobox(online, textvariable=self.history_view, values=("卡片", "表格", "记账明细"), state="readonly", width=10)
        view_box.pack(side="right")
        view_box.bind("<<ComboboxSelected>>", self.change_history_view)
        self.history_period, self.history_timezone = self.var("今天"), self.var("北京时间")
        for text, variable, values, callback in (
            ("范围", self.history_period, (*PERIODS, CUSTOM_PERIOD), self.choose_history_period),
            ("显示时区", self.history_timezone, tuple(TIMEZONES), self.history_filter_changed),
        ):
            ttk.Label(top, text=text).pack(side="left", padx=(0, 4))
            box = ttk.Combobox(top, textvariable=variable, values=values, state="readonly", width=12)
            box.pack(side="left", padx=(0, 10))
            box.bind("<<ComboboxSelected>>", callback)
        self.history_refresh_btn = ttk.Button(top, text="刷新成交记录", command=lambda: self.request_history(refresh=True))
        self.history_refresh_btn.pack(side="left", padx=3)
        self.history_import_btn = ttk.Button(top, text="导入 IBKR Flex 报表…", command=self.import_history_file)
        self.history_import_btn.pack(side="right", padx=3)
        self.history_report_timezone = self.var("纽约时间")
        ttk.Combobox(top, textvariable=self.history_report_timezone, values=tuple(TIMEZONES), width=11, state="readonly").pack(side="right", padx=4)
        ttk.Label(top, text="导入文件时区").pack(side="right")
        self.history_summary = self.var("尚未载入成交记录。")
        summary = ttk.Frame(self.history_tab)
        summary.grid(row=1, column=0, sticky="ew", pady=8)
        self.history_range = self.var("尚未载入成交记录。")
        self.history_pnl = self.var("已实现盈亏\n—")
        self.history_summary_label = ttk.Label(summary, textvariable=self.history_range, font=("Microsoft YaHei UI", 14, "bold"), foreground="#173f55")
        self.history_summary_label.pack(side="left")
        self.history_pnl_label = ttk.ClassicLabel(summary, textvariable=self.history_pnl, font=("Microsoft YaHei UI", 14, "bold"), background="#315569", foreground="white", padx=14, pady=7)
        self.history_pnl_label.pack(side="right")
        self.history_coverage = self.var("成交按账户分别保存；未返回的费用或已实现盈亏显示 —。")
        ttk.Label(self.history_tab, textvariable=self.history_coverage, wraplength=1160, foreground="#687680").grid(row=2, column=0, sticky="ew", pady=(0, 5))
        self.history_tree = self.make_tree(self.history_tab, (("成交时间", 155), ("合约 / 品种", 255), ("买卖", 65), ("数量", 85),
                                                           ("成交价", 146), ("成交金额", 120), ("佣金", 110), ("已实现盈亏", 135), ("来源", 85), ("分享", 105)), row=3)
        self.history_tree.tag_configure("day", background="#e6eff4", foreground="#173f55", font=("Microsoft YaHei UI", 10, "bold"))
        self.history_tree.tag_configure("profit", foreground="#bf3548")
        self.history_tree.tag_configure("loss", foreground="#008653")
        from history_cards import HistoryCards
        self.history_cards = HistoryCards(self.history_tab, self.share_history_group)
        self.history_tree.bind("<ButtonRelease-1>", self.history_table_click)
        self.history_table_keys = {}
        self.history_cards.grid(row=3, column=0, sticky="nsew")
        self.history_tree.master.grid_remove()
        ttk.Label(self.history_tab, text="当前账户全部品种成交，费用/盈亏按币种分开；未知值显示 —。历史范围依据已取得的报表，Flex 可滞后于当日实时成交。", wraplength=1160, foreground="#687680").grid(row=4, column=0, sticky="ew", pady=(4, 0))

    def change_history_view(self, *_):
        cards = self.history_view.get() == "卡片"
        (self.history_tree.master.grid_remove if cards else self.history_tree.master.grid)()
        (self.history_cards.grid if cards else self.history_cards.grid_remove)()
        if self.history_data:
            self.render_history(self.history_data)

    def selected_history_period(self):
        name = self.history_period.get()
        return self.history_custom_dates if name == CUSTOM_PERIOD else PERIODS[name]

    def choose_history_period(self, *_):
        from trade_history import HistoryError, period_dates
        name = self.history_period.get()
        if name != CUSTOM_PERIOD:
            self.history_applied_period = name
            self.history_filter_changed()
            return
        # Keep using the committed range while the two date entries are edited.
        self.history_period.set(self.history_applied_period)
        display_zone = TIMEZONES[self.history_timezone.get()]
        initial = self.history_custom_dates or period_dates(self.selected_history_period(), tz_name=display_zone)
        dialog = ttk.Toplevel(self.root)
        placement = PointerPopup(dialog)
        dialog.title("自选日期范围")
        dialog.transient(self.root)
        frame = ttk.Frame(dialog, padding=14)
        frame.pack(fill="both", expand=True)
        begin, end = (self.var(str(day)) for day in initial)
        for index, (label, variable) in enumerate((("开始日期", begin), ("结束日期", end))):
            ttk.Label(frame, text=label).grid(row=index, column=0, sticky="w", padx=(0, 10), pady=4)
            field = ttk.Entry(frame, textvariable=variable, width=16)
            field.grid(row=index, column=1, sticky="ew")
            if index == 0:
                field.focus_set()
        ttk.Label(frame, text="YYYY-MM-DD，含起止两天").grid(row=2, columnspan=2, sticky="w", pady=4)
        def apply_dates(*_):
            dates = (begin.get().strip(), end.get().strip())
            try:
                period_dates(dates, tz_name=display_zone)
            except HistoryError as exc:
                messagebox.showerror("日期范围无效", str(exc), parent=dialog)
                return
            self.history_custom_dates = dates
            self.history_applied_period = CUSTOM_PERIOD
            self.history_period.set(CUSTOM_PERIOD)
            dialog.destroy()
            self.history_filter_changed()
        ttk.Button(frame, text="应用日期", command=apply_dates).grid(row=3, column=1, sticky="e", pady=(6, 0))
        ttk.Button(frame, text="取消", command=dialog.destroy).grid(row=3, column=0, sticky="w", pady=(6, 0))
        dialog.bind("<Return>", apply_dates)
        dialog.bind("<Escape>", lambda _: dialog.destroy())
        placement.show()
        dialog.grab_set()

    def cancel_flex(self):
        pending = self.flex_auto_pending or self.flex_auto_timer is not None
        self.flex_auto_pending = False
        self.history_tws_pending = False
        if self.flex_auto_timer is not None:
            self.root.after_cancel(self.flex_auto_timer)
            self.flex_auto_timer = None
        if self.flex_cancel:
            self.flex_cancel.set()
        elif pending:
            self.set_flex_feedback("Flex 同步已停止。")

    def queue_auto_flex(self):
        if not self.connected or self.session_mode == "DEMO":
            return
        if self.flex_auto_timer is not None:
            self.root.after_cancel(self.flex_auto_timer)
        self.flex_auto_pending = True
        self.history_tws_pending = True
        self.flex_auto_timer = self.root.after(500, self.run_auto_flex)

    def run_auto_flex(self):
        from trade_history import period_dates
        self.flex_auto_timer = None
        if self.flex_auto_pending and self.history_tws_pending and self.connected:
            self.history_tws_pending = False
            zone = TIMEZONES[self.history_timezone.get()]
            _, end = period_dates(self.selected_history_period(), tz_name=zone)
            today, _ = period_dates("today", tz_name=zone)
            recent = self.history_tws_refresh
            if (end >= today - timedelta(days=7) and
                    (not recent or recent[0] != self.history_fingerprint() or time.monotonic() - recent[1] >= 5)):
                self.request_history(refresh=True)
        if self.flex_auto_pending and not self.flex_busy:
            self.flex_auto_pending = False
            self.sync_flex(automatic=True)

    def finish_flex_work(self):
        self.flex_busy = False
        self.flex_work = self.flex_cancel = None
        if self.flex_auto_pending and self.flex_auto_timer is None:
            self.flex_auto_timer = self.root.after_idle(self.run_auto_flex)

    def remember_flex_windows(self, windows):
        merged = []
        for start, end in sorted([*self.flex_windows, *windows]):
            if merged and (start - merged[-1][1]).days <= 1:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            else:
                merged.append((start, end))
        self.flex_windows = merged

    def saved_flex_settings(self, path, account, mode):
        from flex_sync import load_credentials
        scope = (mode, account)
        if self.flex_config_scope != scope:
            self.flex_config_scope, self.flex_config = scope, {}
            self.flex_windows.clear()
            self.flex_config_revision += 1
            self.flex_rate_failures, self.flex_retry_at = 0, 0.0
        if not self.flex_config:
            try:
                self.flex_config = load_credentials(path, account, mode)
                if self.flex_config:
                    from trade_history import saved_report_windows
                    self.remember_flex_windows(saved_report_windows(path.with_name("trade_history.sqlite3"), account, mode,
                                                self.flex_config["query_id"], self.flex_config["timezone"]))
            except (OSError, ValueError, KeyError):
                self.flex_config = {}
        return self.flex_config

    def configure_flex(self):
        from app import BASE
        from flex_sync import save_credentials
        if not self.connected or self.session_mode == "DEMO":
            self.status.set("请先连接要同步的 IBKR 账户。")
            return
        account, mode = self.snapshot_data.get("account"), self.session_mode
        path = BASE / "data" / "flex_credentials.json"
        config = self.saved_flex_settings(path, account, mode)
        dialog = ttk.Toplevel(self.root)
        placement = PointerPopup(dialog)
        dialog.title("IBKR Flex 在线同步设置")
        dialog.transient(self.root)
        frame = ttk.Frame(dialog, padding=14)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="在 IBKR 客户端门户启用 Flex Web Service，创建 Activity Flex Query。\n选 Trades → Executions、全部品种/渠道、XML 格式。\n此处只读取报表；Token 使用 Windows 当前用户加密后保存在本机。", wraplength=540).grid(row=0, column=0, columnspan=2, pady=6)
        token, query = self.var(config.get("token", "")), self.var(config.get("query_id", ""))
        zone = self.var(config.get("timezone", "America/New_York"))
        for row, (label, variable) in enumerate((("Flex Token", token), ("Query ID", query)), start=1):
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", pady=4)
            ttk.Entry(frame, textvariable=variable, show="*" if row == 1 else "", width=40).grid(row=row, column=1)
        ttk.Label(frame, text="Flex 报表原始时区").grid(row=3, column=0)
        ttk.Combobox(frame, textvariable=zone, values=tuple(TIMEZONES.values()), state="readonly", width=32).grid(row=3, column=1)
        ttk.Label(frame, text="按 IBKR 报表记载时间选择。成交列表可另选北京时间显示。",
                  wraplength=460).grid(row=4, columnspan=2, sticky="w", pady=4)
        def save():
            if not self.connected or account != self.snapshot_data.get("account") or mode != self.session_mode:
                dialog.destroy()
                return
            import re
            if not re.fullmatch(r"\d+", token.get().strip()) or not re.fullmatch(r"\d+", query.get().strip()):
                messagebox.showerror("配置未保存", "Token 和 Query ID 须为数字。", parent=dialog)
                return
            try:
                save_credentials(path, account, mode, token.get().strip(), query.get().strip(), zone.get())
            except Exception:
                messagebox.showerror("配置未保存", "Windows 加密保存失败，未保存 Token。", parent=dialog)
                return
            if token.get().strip() != config.get("token"):
                self.flex_rate_failures, self.flex_retry_at = 0, 0.0
            self.flex_config = {}
            self.flex_config_revision += 1
            self.flex_windows.clear()
            if self.flex_cancel:
                self.flex_cancel.set()
            self.status.set("Flex 设置已保存，正在同步所选日期。")
            dialog.destroy()
            self.queue_auto_flex()
        ttk.Button(frame, text="保存设置", command=save).grid(row=5, column=1, sticky="e", pady=8)
        placement.show()

    def sync_flex(self, automatic=False):
        from app import BASE
        from flex_sync import fetch_reports, report_date_bounds
        from trade_history import HistoryError
        if not self.connected or self.session_mode == "DEMO" or self.flex_busy:
            self.status.set("请连接 IBKR 账户；同一时间只进行一次 Flex 同步。")
            return
        if not automatic and self.flex_auto_timer is not None:
            self.root.after_cancel(self.flex_auto_timer)
            self.flex_auto_timer = None
            self.flex_auto_pending = False
        account = self.snapshot_data.get("account")
        config = self.saved_flex_settings(BASE / "data" / "flex_credentials.json", account, self.session_mode)
        if not config:
            if automatic:
                self.set_flex_feedback("当前显示本地记录。要自动同步，请先保存 Flex 在线同步设置。")
            else:
                self.configure_flex()
            return
        period, zone = self.selected_history_period(), TIMEZONES[self.history_timezone.get()]
        try:
            start, requested_end, end = report_date_bounds(period, zone, config["timezone"])
        except HistoryError as exc:
            self.set_flex_feedback(str(exc))
            return
        if start > end:
            self.set_flex_feedback(f"所选日期的 Flex 日报尚未出具，当前可请求至 {end}。可先查看本地或 TWS 成交。")
            return
        if automatic and any(left <= start and end <= right for left, right in self.flex_windows):
            summary = f"所选范围的可用报表已同步（{start} 至 {end}）。"
            if end < requested_end:
                summary += f" {end + timedelta(days=1)} 至 {requested_end} 的 Flex 日报尚未出具。"
            self.set_flex_feedback(summary)
            return
        if self.flex_retry_at > time.monotonic():
            if self.flex_retry_form is None or self.flex_retry_form == self.history_fingerprint():
                self.schedule_flex_retry()
                return
            self.flex_rate_failures, self.flex_retry_at = 0, 0.0
        context = self.context()
        context.update(flex_form=self.history_fingerprint(), flex_config_revision=self.flex_config_revision,
                       flex_account=account)
        self.flex_work = context
        self.flex_cancel = cancel = threading.Event()
        self.flex_busy = True
        self.status.set("正在读取 IBKR Flex 报表；交易界面仍可使用。")
        self.set_flex_feedback("正在读取 IBKR Flex 报表…")
        known = tuple(self.flex_windows) if automatic else ()
        outbox = self.outbox
        def download():
            try:
                coverage, windows, deferred = [], [], []
                def received_window(first, last):
                    windows.append((first, last))
                    outbox.put(("flex_progress", (first, last), None, context))
                reports = fetch_reports(config["token"], config["query_id"], period, zone, config["timezone"],
                                        cancel, on_coverage=coverage.append, account=account,
                                        on_windows=received_window,
                                        known_windows=known, on_deferred=deferred.append,
                                        hold_path=BASE / "data" / "flex_retry.sqlite3")
                result = dict(reports=reports, tz_name=config["timezone"], covered_windows=windows,
                              query_id=config["query_id"], report_windows=windows)
                if deferred:
                    retry_after, shared = flex_retry_info(deferred[0])
                    if retry_after:
                        result["deferred_retry"] = (str(deferred[0]), retry_after, shared)
                if coverage:
                    result["coverage_note"] = " ".join(coverage)
                outbox.put(("flex_download", result, None, context))
            except Exception as exc:
                retry_after, shared = flex_retry_info(exc)
                outbox.put(("flex_download", None, str(exc), dict(context, retry_after=retry_after, shared_retry=shared)))
        threading.Thread(target=download, daemon=True).start()

    def schedule_flex_retry(self, message=""):
        if self.flex_auto_timer is not None:
            self.root.after_cancel(self.flex_auto_timer)
        delay = max(1, ceil(self.flex_retry_at - time.monotonic()))
        self.flex_auto_pending = True
        self.flex_auto_timer = self.root.after(delay * 1000, self.run_auto_flex)
        next_try = time.strftime("%H:%M:%S", time.localtime(time.time() + delay))
        note = (message + " " if message else "当前显示本地成交。") + f"将在 {next_try}（本机时间）自动重试当前范围。"
        self.set_flex_feedback(note)
        return note

    def apply_flex_backoff(self, retry_after, retry_form):
        if retry_form != self.flex_retry_form:
            self.flex_rate_failures = 0
        self.flex_retry_form = retry_form
        self.flex_rate_failures = min(7, self.flex_rate_failures + 1)
        delay = max(retry_after, min(3600, 60 * 2 ** (self.flex_rate_failures - 1)))
        self.flex_retry_at = time.monotonic() + delay

    def current_flex_config(self, context):
        return (context.get("flex_config_revision", self.flex_config_revision) == self.flex_config_revision
                and ("flex_account" not in context or self.snapshot_data.get("account") == context["flex_account"]))

    def handle_flex_progress(self, window, context):
        if (self.context_is_current("flex_progress", context) and self.current_flex_config(context)
                and context.get("flex_form") == self.history_fingerprint()
                and self.flex_cancel and not self.flex_cancel.is_set()):
            self.set_flex_feedback(f"已取得 {window[0]} 至 {window[1]} 的报表，正在核对所选范围…")

    def handle_flex_download(self, result, error, context):
        if not self.context_is_current("flex_download", context):
            return
        current_range = context.get("flex_form") == self.history_fingerprint()
        if not self.current_flex_config(context) or (self.flex_cancel and self.flex_cancel.is_set()):
            if current_range and not self.flex_auto_pending:
                self.set_flex_feedback("Flex 同步已停止。")
            self.finish_flex_work()
            return
        if error:
            if current_range:
                self.status.set(error)
                self.set_flex_feedback(error)
            retry_after = context.get("retry_after", 0)
            shared = context.get("shared_retry")
            if retry_after and (shared or current_range):
                self.apply_flex_backoff(retry_after, None if shared else context.get("flex_form"))
                # A superseding range might already be covered. Give it the
                # normal cache check before deferring another network request.
                if current_range:
                    error = self.schedule_flex_retry(error)
            self.log(error, event="request_error", level="WARNING" if retry_after else "ERROR", action="flex_download")
            self.finish_flex_work()
            return
        deferred = result.pop("deferred_retry", None)
        if deferred and (deferred[2] or current_range):
            self.apply_flex_backoff(deferred[1], None if deferred[2] else context.get("flex_form"))
        else:
            self.flex_rate_failures, self.flex_retry_at = 0, 0.0
        self.flex_work = dict(context, covered_windows=result.pop("covered_windows", []), deferred=deferred)
        if not result["reports"]:
            if current_range:
                self.set_flex_feedback(result.get("coverage_note") or "所选范围已同步。")
            self.finish_flex_work()
            return
        # The downloaded report belongs to this session even if the date
        # selection moved on. Commit once and re-use its confirmed coverage.
        if self.submit("import_flex_reports", **result):
            self.flex_busy = True
            if current_range:
                self.set_flex_feedback("Flex 报表已下载，正在核对成交。")
        else:
            if current_range:
                self.set_flex_feedback("报表未导入，请在账户连接稳定后重新同步。")
            self.finish_flex_work()

    def finish_flex_import(self, result, error):
        work = self.flex_work or {}
        matching = self.current_flex_config(work)
        if matching and not error and not result.get("missing_execution_ids"):
            self.remember_flex_windows(work.get("covered_windows", []))
        current_range = matching and work.get("flex_form") == self.history_fingerprint()
        if (not error and current_range and work.get("deferred")
                and not (self.flex_cancel and self.flex_cancel.is_set())):
            retry_note = self.schedule_flex_retry(work["deferred"][0])
            result["coverage_note"] = " ".join(filter(None, (result.get("coverage_note"), retry_note)))
        self.finish_flex_work()
        return current_range

    def history_fingerprint(self):
        if not hasattr(self, "history_period"):
            return None
        return json.dumps((self.selected_history_period(), self.history_timezone.get()))

    def history_filter_changed(self, *_):
        from trade_history import HistoryError, period_dates
        try:
            period_dates(self.selected_history_period(), tz_name=TIMEZONES[self.history_timezone.get()])
        except HistoryError as exc:
            self.history_timezone.set(self.history_applied_timezone)
            self.set_flex_feedback(str(exc))
            return
        self.history_applied_timezone = self.history_timezone.get()
        self.history_dirty = True
        self.history_data = None
        self._clear_tree(self.history_tree)
        self.history_summary.set("正在载入所选日期范围…")
        self.history_coverage_text = ""
        self.set_flex_feedback("正在等待同步所选日期。" if self.flex_busy else "")
        if hasattr(self, "history_cards"):
            self.history_cards.set_data([])
            self.history_range.set("正在载入所选日期范围…")
            self.history_pnl.set("已实现盈亏\n—")
        self.request_history()
        self.queue_auto_flex()

    def request_history(self, refresh=False, background=False):
        from trade_history import period_dates
        if not self.connected:
            self.status.set("请先连接对应账户后查看成交记录。")
            return
        self.history_dirty = False
        self.last_history = time.monotonic()
        zone = TIMEZONES[self.history_timezone.get()]
        day_key = (period_dates("today", tz_name=zone)[0], period_dates("today", tz_name="America/New_York")[0])
        if self.history_day_key and self.history_day_key[1] != day_key[1]:
            self.flex_windows.clear()
        if self.history_needs_sync or (self.history_day_key and day_key != self.history_day_key):
            self.queue_auto_flex()
        self.history_day_key = day_key
        refresh = refresh or self.history_needs_sync
        self.history_needs_sync = False
        if self.submit("execution_history", background=background, period=self.selected_history_period(),
                       tz_name=zone, refresh=refresh) and refresh:
            self.history_tws_refresh = (self.history_fingerprint(), time.monotonic())

    def import_history_file(self):
        if not self.connected:
            self.status.set("请先连接要导入的账户；仅导入同账户报表。")
            return
        path = filedialog.askopenfilename(parent=self.root, title=ttk.display(self.root, "选择 IBKR Flex Trades XML / CSV 报表"),
                                          filetypes=((ttk.display(self.root, "IBKR Flex 报表"), "*.xml *.csv"), ("XML", "*.xml"), ("CSV", "*.csv")))
        if not path:
            return
        self.status.set("正在核验报表账户、成交身份和时区并导入本地记录…")
        self.submit("import_execution_history", path=path, tz_name=TIMEZONES[self.history_report_timezone.get()])

    def render_history(self, data):
        self.history_data = data
        rows = data.get("rows", [])
        totals = data.get("totals_by_currency", {})
        parts = []
        for currency, total in sorted(totals.items()):
            label = "区间已实现盈亏" if total.get("pnl_complete") else "区间已知盈亏小计"
            parts.append(f'{label} {history_money(total.get("realized_pnl"), currency, signed=True)}')
        summary = f'{data.get("start_date", "")} 至 {data.get("end_date", "")}  ·  {len(rows)} 笔成交'
        self.history_summary.set(summary + ("  ·  " + "；".join(parts) if parts else "  ·  尚无记录"))
        if hasattr(self, "history_cards"):
            self.history_range.set(f'{data.get("start_date", "")} 至 {data.get("end_date", "")}\n{len(rows)} 笔成交')
            bounds = f'{data.get("start_date", "")} 至 {data.get("end_date", "")}'
            self.history_pnl.set(bounds + "\n" + ("\n".join(parts) if parts else "区间已实现盈亏 —"))
            values = [total.get("realized_pnl") for total in totals.values() if total.get("realized_pnl") is not None]
            color = pnl_color(values[0] if len(values) == 1 else None, getattr(self, "green_profit", False), neutral="#315569")
            self.history_pnl_label.configure(background=color)
            self.history_cards.set_data(rows, data.get("closing_trades", {}))
        coverage = data.get("coverage_label", "只显示已获取或导入的成交。")
        if rows and rows[-1].get("date") and rows[-1].get("time"):
            coverage = f'区间内首笔成交  {rows[-1]["date"]} {rows[-1]["time"]}。 ' + coverage
        if data.get("api_refresh_error"):
            coverage = "券商同步未成功（当前显示本地记录）：" + str(data["api_refresh_error"]) + "\n" + coverage
        if data.get("api_history_note"):
            coverage += "\n" + str(data["api_history_note"])
        if data.get("missing_pnl_count"):
            coverage += f'  {data["missing_pnl_count"]} 笔缺少已实现盈亏。'
        if data.get("missing_commission_count"):
            coverage += f'  {data["missing_commission_count"]} 笔佣金待回报。'
        if data.get("book_entries"):
            coverage += f'  另有 {len(data["book_entries"])} 行记账记录，可在「记账明细」查看。成交盈亏只汇总带执行编号的成交。'
        self.history_coverage_text = coverage
        self.set_flex_feedback(self.flex_feedback)
        if hasattr(self, "history_cards") and self.history_view.get() == "卡片":
            return  # Large YTD reports do not build an invisible table each refresh.
        tree = self.history_tree
        view = tree.yview()
        self._clear_tree(tree)
        if hasattr(self, "history_cards"):
            if self.history_view.get() == "记账明细":
                self.history_table_keys = {}
                entries = data.get("book_entries", [])
                self.history_range.set(f'{data.get("start_date", "")} 至 {data.get("end_date", "")}\n{len(entries)} 行记账记录')
                for entry in entries:
                    currency = entry["currency"]
                    tree.insert("", "end", values=(entry["date"] + " " + entry["time"], entry["symbol"],
                                "买入" if entry["action"] == "BUY" else "卖出", f'{entry["quantity"]:g}',
                                number(entry["price"], 4), history_money(entry["gross"], currency),
                                history_money(entry["commission"], entry["commission_currency"] or "?"),
                                history_money(entry["realized_pnl"], currency, True), "Flex BookTrade"))
            else:
                self.render_group_table(tree)
            if view:
                tree.yview_moveto(view[0])
            return
        days = defaultdict(list)
        for row in rows:
            days[row.get("date", "未知日期")].append(row)
        for day, entries in sorted(days.items(), reverse=True):
            day_totals = defaultdict(float)
            for row in entries:
                if row.get("realized_pnl") is not None:
                    day_totals[row.get("pnl_currency") or row.get("currency") or "?"] += row["realized_pnl"]
            subtotal = "；".join(history_money(value, currency, signed=True) for currency, value in sorted(day_totals.items())) or "—"
            tree.insert("", "end", iid="day:" + day, values=(day, f'{len(entries)} 笔成交 · 已知盈亏小计', "", "", "", "", "", subtotal, ""), tags=("day",))
            for index, row in enumerate(sorted(entries, key=lambda r: r.get("timestamp", ""), reverse=True)):
                pnl = row.get("realized_pnl")
                tags = ("profit",) if pnl is not None and pnl > 0 else (("loss",) if pnl is not None and pnl < 0 else ())
                tree.insert("", "end", iid=f'{day}:{index}:{row.get("exec_id", "")}', values=(
                    row.get("time", ""), (row.get("local_symbol") or row.get("symbol", "")) + " · " + row.get("sec_type", ""),
                    {"BUY": "买入", "SELL": "卖出", "BOT": "买入", "SLD": "卖出"}.get(row.get("action"), row.get("action", "")),
                    number(row.get("quantity"), 4).rstrip("0").rstrip("."), number(row.get("price"), 4),
                    history_money(row.get("gross"), row.get("currency")),
                    history_money(row.get("commission"), row.get("commission_currency")),
                    history_money(pnl, row.get("pnl_currency") or row.get("currency"), signed=True), row.get("source", "")), tags=tags)
        if view:
            tree.yview_moveto(view[0])

    def render_group_table(self, tree):
        from history_cards import quote_amount, average_quote
        cards = self.history_cards
        self.history_table_keys = {}
        for index, (kind, row) in enumerate(cards.records):
            iid = str(index)
            if kind == "day":
                total = " / ".join(history_money(v, c, True) for c, v in row["totals"].items())
                tree.insert("", "end", iid=iid, values=(row["date"], f'{row["count"]} 笔成交', "", "", "", "", "", total), tags=("day",))
                continue
            is_order = kind == "order"
            name = row.get("local_symbol") or row.get("symbol", "")
            price = quote_amount(row.get("price"), row["currency"])
            share = "分享收益图 ↗" if cards.recap_for(kind, row) else ""
            self.history_table_keys[iid] = (kind, row)
            if is_order:
                key = row["key"]
                if len(row["fills"]) > 1:
                    arrow = "▾" if key in cards.expanded else "▸"
                    name = f'{arrow} {name} · {len(row["fills"])} 笔成交'
                    price = "均价 " + average_quote(row["price"], row["currency"])
            else:
                name = "    " + name
            pnl = row.get("realized_pnl")
            tag = "profit" if pnl and pnl > 0 else "loss" if pnl and pnl < 0 else ""
            tree.insert("", "end", iid=iid, values=(row["date"] + " " + row.get("time", ""), name,
                        "买入" if row["action"] in ("BUY", "BOT") else "卖出", f'{row["quantity"]:g}', price,
                        history_money(row.get("gross"), row["currency"]), history_money(row.get("commission"), row.get("commission_currency")),
                        history_money(pnl, row.get("pnl_currency") or row["currency"], True), row.get("source", ""), share),
                        tags=(tag,) if tag else ())

    def history_table_click(self, event):
        tree = self.history_tree
        if tree.identify_region(event.x, event.y) != "cell":
            return
        item = self.history_table_keys.get(tree.identify_row(event.y))
        if item is None:
            return
        kind, row = item
        cards = self.history_cards
        if tree.column(tree.identify_column(event.x), "id") == "分享":
            recap = cards.recap_for(kind, row)
            if recap:
                self.share_history_group(row, recap)
        elif kind == "order" and len(row["fills"]) > 1:
            cards.toggle(row["key"])
            view = tree.yview()
            self._clear_tree(tree)
            self.render_group_table(tree)
            if view:
                tree.yview_moveto(view[0])

    def dismiss_share_card(self):
        window = getattr(self, "share_window", None)
        if window is not None and window.winfo_exists():
            window.destroy()
        self.share_window = None

    def share_history_group(self, group, result):
        from PIL import Image, ImageTk
        from hashlib import sha256
        from share_card import render_share_card, visibility_icon
        self.dismiss_share_card()
        window = self.share_window = ttk.Toplevel(self.root)
        placement = PointerPopup(window)
        window.title("分享收益图 · VincentWangCD")
        window.transient(self.root)
        picture = ttk.Label(window)
        picture.pack()
        controls = ttk.Frame(window)
        controls.pack(pady=8)
        hide_details = False
        eye_images = [ImageTk.PhotoImage(visibility_icon(hidden), master=window) for hidden in (False, True)]

        def toggle_details():
            nonlocal hide_details
            hide_details = not hide_details
            redraw_card()

        eye = ttk.Button(controls, command=toggle_details, compound="left")
        eye.pack(side="left", padx=4)

        def redraw_card():
            hidden = self.bean_mode or hide_details
            eye.configure(image=eye_images[int(hidden)], state="disabled" if self.bean_mode else "normal",
                          text=("欢乐豆模式已隐藏金额与张数" if self.bean_mode else
                                "显示金额与张数" if hidden else "隐藏金额与张数"))
            window.card_image = render_share_card(group, result, demo=self.session_mode == "DEMO",
                                                  private=hidden, language=self.language.code, green_profit=self.green_profit)
            preview = window.card_image.copy()
            preview.thumbnail((min(480, window.winfo_screenwidth() - 40),
                               max(200, window.winfo_screenheight() - 170)), Image.Resampling.LANCZOS)
            window.photo = ImageTk.PhotoImage(preview, master=window)
            picture.configure(image=window.photo)
        window.redraw_card = redraw_card
        redraw_card()
        def save():
            suffix = sha256(str(group.get("key", group["exec_id"])).encode()).hexdigest()[:8]
            path = filedialog.asksaveasfilename(parent=window, title=ttk.display(window, "保存收益图"), defaultextension=".png",
                        filetypes=[(ttk.display(window, "PNG 图片"), "*.png")], initialfile=f'QuickOptions-{group["conid"]}-{group["date"]}-{group["time"].replace(":", "")}-{suffix}.png')
            if path:
                try:
                    window.card_image.save(path, format="PNG")
                except OSError as exc:
                    messagebox.showerror("保存失败", str(exc), parent=window)
                    return
                self.status.set("收益图已保存到 " + path)
                window.destroy()
        ttk.Button(controls, text="保存 PNG…", command=save).pack(side="left", padx=4)
        placement.show()

    def clear_history_display(self):
        self.dismiss_share_card()
        self.cancel_flex()
        self.flex_busy, self.flex_config = False, {}
        self.flex_config_scope = None
        self.flex_work = self.flex_cancel = None
        self.flex_windows.clear()
        self.flex_retry_at, self.flex_rate_failures = 0.0, 0
        self.history_tws_refresh = self.history_day_key = None
        self.flex_feedback = self.history_coverage_text = ""
        self.history_dirty, self.history_data, self.last_history = False, None, 0.0
        self.history_needs_sync = True
        if hasattr(self, "history_tree"):
            self._clear_tree(self.history_tree)
            self.history_summary.set("未连接，成交显示已清空。")
            self.history_coverage.set("本地记录仍保留，连接对应账户后可查看。")
        if hasattr(self, "history_cards"):
            self.history_cards.set_data([])
            self.history_range.set("未连接")
            self.history_pnl.set("已实现盈亏\n—")
