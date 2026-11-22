"""Position monitoring and explicit, previewed close actions (Tk thread only)."""
import json
import math
import time
import uuid
import tkinter as tk
import localized_widgets as ttk
from tkinter import font as tkfont
from contract_display import option_label, option_parts, compact_exit, pnl_color
from popup_position import PointerPopup
from position_add_ui import PositionAddUiMixin


COST_STOP_PRESETS = ("1", "1.25", "1.5", "1.75", "2", "2.5", "3", "3.5", "4", "4.5", "5", "6", "7", "8", "9", "10")


def number(value, digits=2, signed=False):
    try:
        numeric = float(value)
        if not math.isfinite(numeric) or abs(numeric) > 1e100:
            return "—"
        return format(numeric, f"+,.{digits}f" if signed else f",.{digits}f")
    except (ValueError, TypeError):
        return "—"


def position_values(position):
    quantity = position.get("quantity", 0)
    side = "卖方持仓" if quantity < 0 else "买方持仓"
    right = option_parts(position)[3]
    label = option_label(position, multiline=True)
    if quantity and right in ("Call", "Put"):
        bullish = (quantity > 0) == (right == "Call")
        label += "   ↑ 做多" if bullish else "   ↓ 做空"
    return (label, f'{side}\n{abs(quantity):g} 张',
            number(position.get("avg_option_price"), 3), number(position.get("current_price"), 3),
            number(position.get("last"), 3),
            number(position.get("bid"), 3) + " / " + number(position.get("ask"), 3),
            number(position.get("unrealized_pnl"), signed=True),
            number(position.get("unrealized_pnl_pct"), signed=True) + ("%" if position.get("unrealized_pnl_pct") is not None else ""),
            number(position.get("market_value")),
            compact_exit(position, "take_profit"), compact_exit(position, "stop_loss"),
            ("平仓待同步" if "同步" in position.get("close_status", "") else "已有平仓挂单" if position.get("close_pending")
             else position.get("close_status") or position.get("quote_status") or "等待行情"))


class PositionsUiMixin(PositionAddUiMixin):
    def init_position_ui_state(self):
        self.protection_conid = None
        self.position_close_dialog = None
        self.position_add_dialog = None
        self.position_summary = None
        self.position_detail = None

    def build_position_panel(self):
        summary = ttk.Frame(self.position_tab)
        summary.grid(row=0, column=0, sticky="ew", pady=(0, 5))
        self.position_summary = self.var("尚未连接")
        self.position_summary_label = ttk.Label(summary, textvariable=self.position_summary, font=("Microsoft YaHei UI", 12, "bold"), foreground="#173f55")
        self.position_add_btn = ttk.Button(summary, text="＋ 加仓…", command=self.open_position_add)
        self.position_add_btn.pack(side="left", padx=(0, 10))
        self.position_summary_label.pack(side="left", anchor="w")
        bar = ttk.Frame(self.position_tab)
        bar.grid(row=1, column=0, sticky="ew", pady=(0, 5))
        self.close_buttons = []
        for text, percent in (("市价平所选 100%", 100), ("平 25%", 25), ("平 50%", 50)):
            button = ttk.Button(bar, text=text, command=lambda p=percent: self.close_selected(p))
            button.pack(side="left", padx=(0, 5))
            self.close_buttons.append(button)
        self.position_limit_btn = ttk.Button(bar, text="限价 / 中间价平仓…", command=lambda: self.open_position_close("LMT"))
        self.position_limit_btn.pack(side="left", padx=3)
        self.flatten_btn = ttk.Button(bar, text="平全部 SPX 持仓", command=self.close_active_symbol)
        self.flatten_btn.pack(side="left", padx=5)
        self.cancel_btn = ttk.Button(bar, text="撤本工具未成交单", command=lambda: self.mutate("cancel_own"))
        self.cancel_btn.pack(side="right", padx=3)
        self.position_refresh_btn = ttk.Button(bar, text="刷新持仓 / 回报", command=self.refresh_positions_view)
        self.position_refresh_btn.pack(side="right", padx=3)
        self.position_protect_btn = ttk.Button(bar, text="止盈 / 止损设置…", command=self.show_position_protection)
        self.position_protect_btn.pack(side="left", padx=3)
        cost_tools = ttk.Frame(summary)
        cost_tools.pack(side="right")
        ttk.Label(cost_tools, text="成本倍数").pack(side="left")
        self.stop_multiple = self.var("1")
        self.stop_multiple_box = ttk.Combobox(cost_tools, textvariable=self.stop_multiple,
                                              values=COST_STOP_PRESETS, height=len(COST_STOP_PRESETS), width=5)
        self.stop_multiple_box.pack(side="left", padx=4)
        self.cost_stop_btn = ttk.Button(cost_tools, text="一键保本损", command=self.request_cost_stop)
        self.cost_stop_btn.pack(side="left")
        self.stop_multiple.trace_add("write", lambda *_: self.update_enabled())
        self.loss_stop_btn = ttk.Menubutton(cost_tools, text="亏损止损 ▾")
        loss_menu = ttk.Menu(self.loss_stop_btn, tearoff=False)
        for loss in range(10, 71, 10):
            loss_menu.add_command(label=f"挂 {loss}% 止损 · 成本 × {(100-loss)/100:g}",
                                  command=lambda pct=loss: self.request_loss_stop(pct))
        self.loss_stop_btn.configure(menu=loss_menu)
        self.loss_stop_btn.pack(side="left", padx=3)
        self.pos_tree = self.make_tree(self.position_tab, (
            ("合约", 210), ("持仓 / 张数", 106), ("成本 $/份", 80), ("估值 $/份", 80),
            ("最新成交价", 85), ("买一 / 卖一", 110), ("浮动盈亏 $", 98), ("盈亏 %", 82),
            ("市值 $", 80), ("止盈 / 限价平仓", 128), ("止损", 120), ("平仓 / 行情状态", 140)), row=2)
        self.pos_tree.heading("合约", text="合约 / 标的方向")
        self.position_style = ttk.Style(self.root)
        self.position_font = tkfont.Font(family="Microsoft YaHei UI", size=9)
        self.position_style.configure("Positions.Treeview", rowheight=max(48, self.position_font.metrics("linespace") * 2 + 8), font=self.position_font)
        self.pos_tree.configure(style="Positions.Treeview")
        for column in self.pos_tree["columns"]:
            self.pos_tree.column(column, stretch=False, anchor="w" if column in ("合约", "止盈 / 限价平仓", "止损", "平仓 / 行情状态") else "center")
        self.pos_tree.column("平仓 / 行情状态", stretch=True)
        self.pos_tree.tag_configure("profit", foreground=pnl_color(1))
        self.pos_tree.tag_configure("loss", foreground=pnl_color(-1))
        self.pos_tree.bind("<<TreeviewSelect>>", self.position_selected)
        self.pos_tree.bind("<Button-3>", self.position_context_menu)
        self.pos_tree.bind("<Double-1>", lambda event: self.position_double_click(event))
        self.position_detail = self.var("右键持仓可选择市价、限价或中间价平仓，也可设置止盈止损。")
        ttk.Label(self.position_tab, textvariable=self.position_detail, wraplength=1160, foreground="#24506a").grid(row=3, column=0, sticky="ew", pady=5)
        self.build_close_order_panel()
        self.close_panel.grid(row=4, column=0, sticky="ew")
        self.close_panel.grid_remove()
        self.position_note = self.var("估值与最新成交价可能不同；盈亏尚未扣除未来平仓费用。缺行情时显示 —，不会按 0 计算。")
        ttk.Label(self.position_tab, textvariable=self.position_note, foreground="#687680", wraplength=1160).grid(row=5, column=0, sticky="ew", pady=(3, 0))
        for row in (0, 1, 3, 4, 5):
            self.position_tab.rowconfigure(row, weight=0)
        self.position_tab.rowconfigure(2, weight=1)
        self.position_menu = ttk.Menu(self.root, tearoff=False, postcommand=self.prepare_position_menu)
        self.position_menu.add_command(label="加仓…", command=self.open_position_add)
        self.position_menu.add_separator()
        for label, mode in (("市价平仓…", "MKT"), ("限价平仓…", "LMT"), ("中间价平仓…", "MID"), ("自动中间价平仓…", "AUTO_MID")):
            self.position_menu.add_command(label=label, command=lambda m=mode: self.open_position_close(m))
        self.position_menu.add_separator()
        self.position_menu.add_command(label="一键保本损", command=self.request_breakeven_stop)
        self.position_menu.add_command(label="按成本倍数设置止损…", command=self.request_cost_stop)
        for label, kind in (("设置限价止盈…", "LMT"), ("设置止损…", "STP"), ("设置止损限价…", "STP LMT"), ("设置移动止损…", "TRAIL")):
            self.position_menu.add_command(label=label, command=lambda k=kind: self.show_position_protection(k))
        self.position_menu.add_separator()
        self.position_menu.add_command(label="查看该合约挂单 / 改价 / 撤单", command=self.show_position_orders)
        self.position_menu.add_command(label="刷新持仓与成交回报", command=self.refresh_positions_view)

    def selected_position(self):
        selected = self.pos_tree.selection()
        return self.positions_by_id.get(selected[0], {}) if selected else {}

    def refresh_positions_view(self):
        if self.connected and not self.has_pending({"refresh_positions"}):
            self.status.set("正在向券商核对持仓、委托和成交回报…")
            self.submit("refresh_positions")

    def render_positions(self, positions):
        old_ids = self.pos_tree.get_children()
        old_view = self.pos_tree.yview()
        self.positions_by_id = {str(p["conid"]): p for p in positions}
        order_changed = tuple(self.positions_by_id) != old_ids
        for key in old_ids:
            if key not in self.positions_by_id:
                self.pos_tree.delete(key)
        for index, (key, position) in enumerate(self.positions_by_id.items()):
            pnl = position.get("unrealized_pnl")
            tags = ("profit",) if pnl is not None and pnl > 0 else (("loss",) if pnl is not None and pnl < 0 else ())
            options = dict(values=position_values(position), tags=tags)
            if key in old_ids:
                self.pos_tree.item(key, **options)
                if order_changed:
                    self.pos_tree.move(key, "", index)
            else:
                self.pos_tree.insert("", "end", iid=key, **options)
        if len(self.positions_by_id) == 1 and not self.selected_position():
            self.pos_tree.selection_set(next(iter(self.positions_by_id)))
        if old_view and order_changed:
            self.pos_tree.yview_moveto(old_view[0])
        known = [p for p in positions if p.get("unrealized_pnl") is not None]
        pnl = sum(p["unrealized_pnl"] for p in known)
        costs = sum(p.get("entry_cost") or 0 for p in known)
        percent = pnl / costs * 100 if costs else None
        if self.position_summary is not None:
            if self.locked:
                text = "发单已锁定，请先到 TWS 核对订单和成交"
            elif not positions and self.snapshot_data.get("position_sync_pending"):
                text = "已收到成交回报，正在核对持仓…"
            elif not positions and self.snapshot_data.get("position_open_unconfirmed"):
                text = "开仓成交已回报，持仓仍待确认，请点「刷新持仓 / 回报」"
            elif not positions:
                text = "当前无标准期权持仓"
            else:
                known_costs = [p["entry_cost"] for p in positions if p.get("entry_cost") is not None]
                cost_label = "开仓成本/基准" if len(known_costs) == len(positions) else "已知成本/基准"
                text = (f'{len(positions)} 个合约  ·  已估值浮动盈亏 ${number(pnl, signed=True) if known else "—"}'
                        f'  ({number(percent, signed=True)}%)  ·  {cost_label} ${number(sum(known_costs)) if known_costs else "—"}')
                if len(known) != len(positions):
                    text += f'  ·  {len(positions) - len(known)} 个合约待估值'
            if positions and not self.locked and getattr(self, "bean_mode", False):
                text = f"{len(positions)} 个合约 · 浮动盈亏见各持仓百分比"
            if positions and not self.locked and self.snapshot_data.get("position_open_unconfirmed"):
                text += "  ·  另有开仓成交待核对持仓"
            self.position_summary.set(text)
        if hasattr(self, "position_summary_label"):
            self.position_summary_label.configure(foreground=pnl_color(pnl if known else None, getattr(self, "green_profit", False)))
        self.position_selected()

    def position_selected(self, *_):
        position = self.selected_position()
        if getattr(self, "protection_conid", None) is not None and position.get("conid") != self.protection_conid:
            self.close_panel.grid_remove()
            self.protection_conid = None
        if hasattr(self, "position_style"):
            self.position_style.map("Positions.Treeview", foreground=[("selected", pnl_color(position.get("unrealized_pnl"), getattr(self, "green_profit", False)))], background=[("selected", "#e8eff7")])
        if self.position_detail is not None:
            if not position:
                self.position_detail.set("请选择要平仓的持仓。" if self.positions_by_id else "等待券商持仓回报。")
            else:
                details = (f'{option_label(position)}  ·  原始代码 {position.get("local_symbol", "")}'
                           f'\n每张成本 ${number(position.get("avg_cost"))}  ·  整笔成本/基准 ${number(position.get("entry_cost"))}'
                           f'  ·  {position.get("price_source") or "估值来源待确认"}  ·  {position.get("quote_status") or "等待行情"}')
                if position.get("close_status"):
                    details += "\n" + position["close_status"]
                details += ("\n" + position.get("take_profit_summary", "未发现限价平仓单")
                            + "  |  " + position.get("stop_loss_summary", "未发现止损单"))
                if getattr(self, "bean_mode", False):
                    details = option_label(position)
                    if position.get("close_pending"):
                        details += "  ·  " + position.get("close_status", "平仓委托待核对")
                self.position_detail.set(details)
        self.update_enabled()

    def prepare_position_menu(self):
        # Changing a posted native Windows menu restarts its display animation.
        # Read the toolbar state at posting time. Actions recheck the live position.
        state = str(self.position_add_btn.cget("state"))
        if self.position_menu.entrycget(0, "state") != state:
            self.position_menu.entryconfigure(0, state=state)

    def position_context_menu(self, event):
        row = self.pos_tree.identify_row(event.y)
        if row not in self.positions_by_id:
            return
        if self.pos_tree.selection() != (row,):
            self.pos_tree.selection_set(row)
        self.position_selected()
        try:
            self.position_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.position_menu.grab_release()

    def position_double_click(self, event):
        row = self.pos_tree.identify_row(event.y)
        if row in self.positions_by_id:
            self.pos_tree.selection_set(row)
            self.open_position_close("LMT")

    def show_position_protection(self, kind=None):
        from app import CLOSE_TYPES
        position = self.selected_position()
        if not position:
            self.status.set("请先选择持仓。")
            return
        if position.get("quantity", 0) < 0:
            self.status.set("空头可右键选择市价、限价或中间价买回；止损/移动卖单面板仅用于多头。")
            return
        if self.protection_conid != position["conid"]:
            for name in ("limit_price", "stop_price", "trail_value", "limit_offset"):
                self.close_spec_values[name].set("")
            self.protection_conid = position["conid"]
        self.close_panel.grid()
        if kind:
            self.close_type.set(next(label for label, value in CLOSE_TYPES.items() if value == kind))
        self.update_close_order_fields()
        feedback = (position.get("close_status") if position.get("close_pending") else
                    f'{option_label(position)} · 填写触发价或限价后，点击「提交所选卖单」。')
        self.protection_feedback.set(feedback)

    @staticmethod
    def can_set_cost_stop(position):
        qty = position.get("quantity", 0)
        if qty <= 0 or not position.get("close_pending"):
            return qty > 0
        exits = position.get("active_exit_orders", [])
        if len(exits) != 1:
            return False
        order = exits[0]
        return (order.get("owned") and order.get("working") and order.get("order_type") == "STP"
                and order.get("tif") == "DAY" and order.get("remaining") == qty
                and not order.get("filled") and not order.get("parent_id") and not order.get("oca_group"))

    def cost_stop_form(self):
        position = self.selected_position()
        return (position.get("conid"), position.get("quantity"), position.get("avg_cost"),
                self.close_type.get(), self.close_tif.get(), self.close_percent.get(),
                self.close_spec_values["stop_price"].get(), self.stop_multiple.get())

    def request_loss_stop(self, percent):
        self.request_cost_stop(loss_percent=percent)

    def request_breakeven_stop(self):
        position = self.selected_position()
        if position.get("quantity", 0) <= 0 or (position.get("close_pending") and not position.get("protection_id")):
            self.status.set(position.get("close_status") or "请先选择可设置止损的买方持仓。")
            return
        self.mutate("set_breakeven_stop", conid=position["conid"])

    def request_cost_stop(self, loss_percent=None):
        position = self.selected_position()
        from exit_orders import cost_multiple
        try:
            multiple = float(cost_multiple(self.stop_multiple.get())) if loss_percent is None else (100-loss_percent)/100
        except ValueError as exc:
            self.status.set(str(exc))
            return
        if multiple == 1 and loss_percent is None:
            self.request_breakeven_stop()
            return
        if not self.can_set_cost_stop(position):
            self.status.set(position.get("close_status") or "请先选择可设置止损的买方持仓。")
            return
        self.show_position_protection("STP")
        self.close_percent.set(100)
        self.close_tif.set("DAY")
        self.close_spec_values["stop_price"].set("")
        self.protection_feedback.set("正在核对持仓成本、报价步长与当前买一…")
        extra = {"loss_preset": loss_percent} if loss_percent is not None else {}
        self.submit("prepare_cost_stop", conid=position["conid"], multiple=multiple, **extra)

    def show_position_orders(self):
        position = self.selected_position()
        if not position:
            return
        self.notebook.select(self.order_tab)
        if hasattr(self, "order_filter"):
            self.order_filter.set("挂单")
            self.render_orders(self.snapshot_data.get("orders", []))
        for key, order in self.orders_by_id.items():
            if order.get("conid") == position["conid"] and order.get("remaining", 0) > 0:
                self.order_tree.selection_set(key)
                self.order_tree.see(key)
                self.order_selected()
                return
        self.status.set("当前可见挂单中没有该合约。其他客户端委托也可在原客户端核对。")

    def open_position_close(self, mode="LMT"):
        from app import ORDER_REQUESTS, CONTROL_REQUESTS
        position = self.selected_position()
        if not self.connected or self.locked or self.has_pending(ORDER_REQUESTS | CONTROL_REQUESTS) or not position:
            self.status.set("请连接账户并选择可平仓持仓。")
            return
        if position.get("close_pending") and not position.get("protection_id"):
            self.status.set(position.get("close_status") or "该持仓已有平仓请求，正在同步回报；请查看订单或刷新。")
            return
        self.dismiss_position_close()
        self.dismiss_position_add()
        dialog = ttk.Toplevel(self.root)
        placement = PointerPopup(dialog)
        dialog.title("平仓预览 · " + option_label(position))
        dialog.transient(self.root)
        dialog.resizable(False, False)
        frame = ttk.Frame(dialog, padding=14)
        frame.pack(fill="both", expand=True)
        state = dict(window=dialog, id=uuid.uuid4().hex, conid=position["conid"],
                     position_quantity=position["quantity"], mode=self.var({"MKT": "市价", "LMT": "限价", "MID": "中间价", "AUTO_MID": "自动中间价"}[mode]),
                     percent=self.var(100), limit=self.var(""), text=self.var("正在获取平仓预览…"), preview=None, suppress=False)
        self.position_close_dialog = state
        action = "买回空头" if position["quantity"] < 0 else "卖出平多头"
        ttk.Label(frame, text=f'{option_label(position)}\n{action} · 当前 {abs(position["quantity"]):g} 张', font=("Microsoft YaHei UI", 12, "bold")).grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 10))
        ttk.Label(frame, text="方式").grid(row=1, column=0, sticky="w")
        mode_box = ttk.Combobox(frame, textvariable=state["mode"], values=("市价", "限价", "中间价", "自动中间价"), state="readonly", width=14)
        mode_box.grid(row=1, column=1, padx=5)
        ttk.Label(frame, text="平仓比例 %").grid(row=1, column=2)
        ttk.Entry(frame, textvariable=state["percent"], width=8).grid(row=1, column=3, padx=5)
        ttk.Label(frame, text="限价 $/份").grid(row=2, column=0, sticky="w", pady=7)
        state["limit_entry"] = ttk.Entry(frame, textvariable=state["limit"], width=16)
        state["limit_entry"].grid(row=2, column=1, padx=5)
        ttk.Label(frame, text="留空时先填中间价", foreground="#687680").grid(row=2, column=2, columnspan=2)
        ttk.Label(frame, textvariable=state["text"], wraplength=520, justify="left").grid(row=3, column=0, columnspan=4, sticky="w", pady=8)
        buttons = ttk.Frame(frame)
        buttons.grid(row=4, column=0, columnspan=4, sticky="ew")
        ttk.Button(buttons, text="刷新预览", command=self.request_position_close).pack(side="left")
        state["submit_btn"] = ttk.ClassicButton(frame, text="等待平仓预览", command=self.submit_position_close,
                                        font=("Microsoft YaHei UI", 13, "bold"), bg="#9b283b", fg="white",
                                        activebackground="#702031", activeforeground="white",
                                        disabledforeground="#e7d7da", padx=18, pady=8, state="disabled")
        state["submit_btn"].grid(row=5, column=0, columnspan=4, sticky="ew", pady=(10, 0))
        ttk.Button(buttons, text="关闭", command=self.dismiss_position_close).pack(side="right", padx=6)
        for field in ("mode", "percent", "limit"):
            state[field].trace_add("write", self.position_close_changed)
        dialog.protocol("WM_DELETE_WINDOW", self.dismiss_position_close)
        dialog.bind("<Return>", lambda _event: self.request_position_close())
        self.position_close_changed()
        self.request_position_close()

        placement.show()

    def dismiss_position_close(self):
        state = self.position_close_dialog
        self.position_close_dialog = None
        if state and state.get("window"):
            state["window"].destroy()

    def position_close_changed(self, *_):
        state = self.position_close_dialog
        if not state or state["suppress"]:
            return
        state["preview"] = None
        state["text"].set("参数已改变，请刷新预览后提交。")
        state["limit_entry"].configure(state="normal" if state["mode"].get() == "限价" else "disabled")
        state["submit_btn"].configure(state="disabled")

    def close_form_fingerprint(self):
        state = self.position_close_dialog
        if not state:
            return None
        config = tuple(getattr(self, attr).get() if hasattr(self, attr) else str(default) for attr, default in
                       (("mid_interval", 3), ("mid_duration", 300), ("mid_drift", 10))) if state["mode"].get() == "自动中间价" else None
        return json.dumps((state["id"], state["conid"], state["mode"].get(), state["percent"].get(), state["limit"].get(), config))

    def request_position_close(self):
        from app import ORDER_REQUESTS, CONTROL_REQUESTS
        state = self.position_close_dialog
        if not state or not self.connected or self.locked or self.has_pending(ORDER_REQUESTS | CONTROL_REQUESTS):
            return
        state["preview"] = None
        state["submit_btn"].configure(state="disabled")
        try:
            percent = float(state["percent"].get())
            if not math.isfinite(percent) or not 0 < percent <= 100:
                raise ValueError("平仓比例须大于 0 且不超过 100%。")
            mode = {"市价": "MKT", "限价": "LMT", "中间价": "MID", "自动中间价": "MID"}[state["mode"].get()]
            if state["mode"].get() == "自动中间价":
                if state["position_quantity"] < 0:
                    raise ValueError("空头买回暂用固定中间价；自动追价仅支持卖出平多头。")
                self.automatic_mid_policy()
            limit = None
            if mode == "LMT" and state["limit"].get().strip():
                limit = float(state["limit"].get())
                if not math.isfinite(limit) or limit <= 0:
                    raise ValueError("限价须为有效正数。")
        except (ValueError, KeyError) as exc:
            state["text"].set(str(exc))
            return
        state["text"].set("正在核对持仓、盘口、交易时段与挂单…")
        position = self.positions_by_id.get(str(state["conid"]), {})
        extra = {"protection_id": position["protection_id"]} if position.get("protection_id") else {}
        self.submit("preview_close", conid=state["conid"], percent=percent, order_mode=mode, limit_price=limit, **extra)

    def render_position_close(self, preview):
        state = self.position_close_dialog
        if not state:
            return
        if preview.get("limit_seeded") and state["mode"].get() == "限价" and not state["limit"].get().strip():
            state["suppress"] = True
            state["limit"].set(self.price_text(preview["limit_price"]))
            state["suppress"] = False
        state["preview"] = preview
        state["fingerprint"] = self.close_form_fingerprint()
        state["position_quantity"] = preview.get("position_quantity", state["position_quantity"])
        action = "卖出平仓" if preview["action"] == "SELL" else "买入回补"
        price = "市价（成交价随市场变化）" if preview["order_type"] == "MKT" else f'限价 {self.price_text(preview["limit_price"])} 美元/份'
        text = (f'{action} {preview["quantity"]:g} 张 · {price}\n'
                f'买一 {self.price_text(preview["bid"])} / 卖一 {self.price_text(preview["ask"])}\n'
                f'{preview.get("session_label", "")} · {preview.get("tif", "DAY")}\n')
        if preview.get("estimated_proceeds") is not None:
            text += (f'预计净回收 ${number(preview["estimated_proceeds"])}\n' if preview["action"] == "SELL"
                     else f'预计总支出 ${number(abs(preview["estimated_proceeds"]))}\n')
        if preview.get("estimated_pnl") is not None:
            text += f'预计本次盈亏 ${number(preview["estimated_pnl"], signed=True)}（已扣下列平仓费用预留）\n'
        if preview.get("fee_reserve") is not None:
            text += f'平仓费用预留 ${number(preview["fee_reserve"])}\n'
        text += ('预览 10 秒有效。' if preview['order_type'] == 'MKT' else '预览 10 秒有效；限价可能等待成交，不自动追价。')
        if state["mode"].get() == "自动中间价":
            config = self.automatic_mid_policy()
            text = text.replace('限价可能等待成交，不自动追价。', f'自动追价每 {config["interval"]:g} 秒，最长 {config["duration"]:g} 秒；卖价下限 {preview["limit_price"] * (1 - config["drift_pct"] / 100):g}。停止后原单保留。')
        from privacy_view import mask_money
        state["text"].set(mask_money(text) if getattr(self, "bean_mode", False) else text)
        if preview.get("protection_id"):
            state["text"].set(state["text"].get() + f'\n将撤销保护止损 #{preview["protection_id"]}，撤单确认后提交平仓。部分平仓后请为余仓重新设置止损。')
        verb = "确认撤止损并" if preview.get("protection_id") else "确认"
        state["submit_btn"].configure(text=f'{verb}{action} {preview["quantity"]:g} 张', state="normal")

    def submit_position_close(self):
        from app import ORDER_REQUESTS, CONTROL_REQUESTS
        state = self.position_close_dialog
        preview = state.get("preview") if state else None
        if not preview:
            return
        position = self.positions_by_id.get(str(state["conid"]))
        if (not self.connected or self.locked or self.has_pending(ORDER_REQUESTS | CONTROL_REQUESTS)
                or self.close_form_fingerprint() != state.get("fingerprint")
                or time.time() - preview["created_at"] > 10
                or not position or position.get("quantity") != state["position_quantity"]):
            state["preview"] = None
            state["text"].set("持仓或预览已改变，请刷新预览后确认。")
            state["submit_btn"].configure(state="disabled")
            return
        token = preview["token"]
        args = dict(token=token)
        if state["mode"].get() == "自动中间价":
            args["auto_mid"] = self.automatic_mid_policy()
        state["preview"] = None
        state["submit_btn"].configure(text="正在核对平仓委托…", state="disabled")
        state["text"].set("已确认。正在核对持仓、报价与券商回报。")
        self.mutate("close_confirm", **args)

    def update_position_controls(self, ready, order_pending):
        if hasattr(self, "position_limit_btn"):
            position = self.selected_position()
            enabled = ready and not order_pending and not self.locked and position and not position.get("close_pending")
            if hasattr(self, "position_add_btn"):
                can_add = (ready and not order_pending and not self.locked and self.financial_ready and position
                           and (not position.get("close_pending") or position.get("can_add_with_stop")))
                self.position_add_btn.configure(state="normal" if can_add else "disabled")
            can_close = enabled or (ready and not order_pending and not self.locked and position and position.get("protection_id"))
            self.position_limit_btn.configure(state="normal" if can_close else "disabled")
            self.position_protect_btn.configure(state="normal" if enabled and position.get("quantity", 0) > 0 else "disabled")
            cost_ready = ready and not order_pending and not self.locked and self.can_set_cost_stop(position)
            try:
                at_cost = float(self.stop_multiple.get()) == 1
            except (AttributeError, TypeError, ValueError):
                at_cost = False
            breakeven_ready = can_close and position.get("quantity", 0) > 0
            available = breakeven_ready if at_cost else cost_ready
            self.cost_stop_btn.configure(text="一键保本损" if at_cost else "设置成本止损…",
                                         state="normal" if available else "disabled")
            if hasattr(self, "loss_stop_btn"):
                self.loss_stop_btn.configure(state="normal" if cost_ready else "disabled")
            if hasattr(self, "stop_multiple_box"):
                self.stop_multiple_box.configure(state="normal" if cost_ready or breakeven_ready else "disabled")
            self.position_refresh_btn.configure(state="normal" if ready and not order_pending and not self.has_pending({"refresh_positions"}) else "disabled")
            labels = ("市价平所选 100%", "平 25%", "平 50%")
            for button, label, pct in zip(self.close_buttons, labels, (100, 25, 50)):
                if position and position.get("protection_id"):
                    label = f"撤止损并平 {pct}%"
                button.configure(text=label, state="normal" if can_close else "disabled")
        add = getattr(self, "position_add_dialog", None)
        if add and add.get("preview"):
            usable = self.add_preview_usable(ready and not order_pending)
            add["submit_btn"].configure(state="normal" if usable else "disabled")
            if not usable:
                add["submit_btn"].configure(text="请重新预览加仓")
        state = self.position_close_dialog
        if state:
            preview = state.get("preview")
            usable = (ready and not order_pending and not self.locked and preview
                      and time.time() - preview["created_at"] <= 10)
            if preview and time.time() - preview["created_at"] > 10:
                state["submit_btn"].configure(text="预览已过期 · 请刷新", state="disabled")
            else:
                state["submit_btn"].configure(state="normal" if usable else "disabled")

    def clear_position_display(self):
        self.dismiss_position_add()
        self.dismiss_position_close()
        if self.position_summary is not None:
            self.position_summary.set("尚未连接")
        if self.position_detail is not None:
            self.position_detail.set("持仓信息已清空。")
