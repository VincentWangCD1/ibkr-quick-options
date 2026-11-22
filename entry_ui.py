"""Dual-direction entry controls; all broker work stays on App's Worker."""
import json
import math
import time
from datetime import datetime
from zoneinfo import ZoneInfo
import tkinter as tk
import localized_widgets as ttk
from popup_position import messagebox

BUDGET_SELECTION = "按预算找虚值档"

OPEN_SIDES = {"买入期权（买方）": "BUY", "卖出期权（卖方）": "SELL"}


def positive_number(value, label):
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(label + "请输入有效正数。") from None
    if not math.isfinite(number) or number <= 0:
        raise ValueError(label + "请输入有效正数。")
    return number


def parse_chain_limit(value):
    if str(value).strip() in ("所有档数", "ALL", "0"):
        return 0
    try:
        count = int(value)
    except (TypeError, ValueError):
        raise ValueError("每页档数请输入正整数，或选择“所有档数”。") from None
    if count < 1 or count > 10000:
        raise ValueError("每页档数支持 1–10000，更多请选择“所有档数”。")
    return count


def direction_label(action, right):
    bullish = (action == "BUY" and right == "C") or (action == "SELL" and right == "P")
    return ("买" if action == "BUY" else "卖") + ("Call" if right == "C" else "Put") + ("（做多）" if bullish else "（做空）")


class EntryUiMixin:
    def init_entry_state(self):
        self.budget_search_id = 0
        self.normal_selection_mode = "按档位"
        self.side_previews = {}
        self.side_fingerprints = {}
        self.entry_confirmation_open = False
        self.selected_conids = {}
        self.limit_conids = {}
        self.chain_rows_by_id = {}
        self.committed_chain_limit = 40
        self.chain_view_signature = None
        self.last_expiry_refresh = 0.0
        self.expiry_day = datetime.now(ZoneInfo("America/New_York")).date()

    def build_extra_selection(self, left):
        self.budget_mode, self.cash_amount = self.var("比例 %"), self.var("")
        self.percent_input = self.var(50)
        self.percent_input.trace_add("write", self.changed)
        self.cash_amount.trace_add("write", self.changed)
        exact = ttk.Frame(left)
        exact.grid(row=10, column=0, columnspan=4, sticky="ew", pady=(5, 0))
        self.selection_mode, self.exact_strike = self.var("按档位"), self.var("")
        ttk.Label(exact, text="合约选择").pack(side="left")
        self.selection_mode_box = ttk.Combobox(exact, textvariable=self.selection_mode, values=("按档位", "指定行权价", BUDGET_SELECTION), width=10, state="readonly")
        self.selection_mode_box.pack(side="left", padx=5)
        self.exact_strike_entry = ttk.Entry(exact, textvariable=self.exact_strike, width=12)
        self.exact_strike_entry.pack(side="left")
        ttk.Label(exact, text="也可双击期权链的 Call / Put 报价", foreground="#687680").pack(side="left", padx=6)
        self.selection_mode.trace_add("write", self.selection_mode_changed)
        self.exact_strike.trace_add("write", self.exact_strike_changed)
        seller = ttk.Frame(left)
        seller.grid(row=11, column=0, columnspan=4, sticky="ew", pady=(5, 0))
        self.seller_quantity = self.var(1)
        ttk.Label(seller, text="卖方开仓张数").pack(side="left")
        self.seller_quantity_entry = ttk.Entry(seller, textvariable=self.seller_quantity, width=8)
        self.seller_quantity_entry.pack(side="left", padx=5)
        ttk.Label(seller, text="按保证金核验；卖出开仓前需确认", foreground="#945900").pack(side="left")
        self.seller_quantity.trace_add("write", self.changed)

    def build_entry_panel(self, parent):
        top = ttk.Frame(parent)
        top.pack(fill="x", pady=(0, 5))
        self.entry_mode = self.var("市价")
        ttk.Label(top, text="委托方式").pack(side="left")
        self.entry_mode_box = ttk.Combobox(top, textvariable=self.entry_mode, values=("市价", "限价", "中间价（固定限价）", "中间价（自动追价）"), state="readonly", width=19)
        self.entry_mode_box.pack(side="left", padx=5)
        self.preview_btn = ttk.Button(top, text="匹配 / 刷新", command=self.refresh_entry_match)
        self.preview_btn.pack(side="left", padx=3)
        self.auto = tk.BooleanVar(value=True)
        ttk.Checkbutton(top, text="自动（5秒）", variable=self.auto).pack(side="left", padx=3)
        budget = ttk.Frame(parent)
        budget.pack(fill="x", pady=(0, 4))
        ttk.Label(budget, text="找档预算").pack(side="left")
        self.budget_mode_box = ttk.Combobox(budget, textvariable=self.budget_mode, values=("比例 %", "金额 USD"), width=8, state="readonly")
        self.budget_mode_box.pack(side="left", padx=5)
        self.budget_input = ttk.Entry(budget, textvariable=self.percent_input, width=8)
        self.budget_input.pack(side="left")
        self.budget_input.bind("<Return>", self.commit_budget)
        self.budget_input.bind("<FocusOut>", self.commit_budget)
        self.budget_mode_box.bind("<<ComboboxSelected>>", self.budget_mode_changed)
        self.budget_match_button = ttk.Button(budget, text="按预算找档", command=self.match_budget)
        self.budget_match_button.pack(side="left", padx=5)
        self.budget_summary = self.var("")
        ttk.Label(parent, textvariable=self.budget_summary, foreground="#24506a").pack(anchor="w", pady=(0, 3))
        follow = ttk.Frame(parent)
        self.entry_follow_frame = follow
        follow.pack(fill="x", pady=(0, 4))
        self.mid_interval, self.mid_duration, self.mid_drift = self.var(3), self.var(300), self.var(10)
        for label, variable in (("追价间隔(秒)", self.mid_interval), ("最长(秒)", self.mid_duration), ("幅度(%)", self.mid_drift)):
            ttk.Label(follow, text=label).pack(side="left")
            ttk.Entry(follow, textvariable=variable, width=5).pack(side="left", padx=3)
            variable.trace_add("write", self.changed)
        self.match_text = self.var("连接后选择标的、到期日和交易类别。")
        self.entry_match_label = ttk.Label(parent, textvariable=self.match_text, foreground="#526775", wraplength=560)
        self.entry_match_label.pack(anchor="w", fill="x", pady=(0, 2))
        follow.pack_forget()
        self.entry_limits = {side: self.var("") for side in ("C", "P")}
        self.entry_limit = self.entry_limits["C"]  # Existing controller API alias.
        cards = ttk.Frame(parent)
        cards.pack(fill="both", expand=True)
        ttk.Style(self.root).configure("CompactBuy.TButton", font=("Microsoft YaHei UI", 11, "bold"), padding=4)
        self.entry_detail_widgets = []
        self.entry_details_expanded = False
        self.entry_price_frames = []
        self.side_frames, self.side_vars, self.side_buttons, self.entry_limit_entries = {}, {}, {}, {}
        for col, side in enumerate(("C", "P")):
            cards.columnconfigure(col, weight=1, uniform="entry")
            card = ttk.LabelFrame(cards, text=direction_label("BUY", side), padding=4)
            card.grid(row=0, column=col, sticky="nsew", padx=(0, 5) if col == 0 else (5, 0))
            self.side_frames[side] = card
            values = {name: self.var(value) for name, value in (("contract", "等待匹配"), ("quotes", "买一 —   卖一 —"),
                        ("amount", "预计支出 —"), ("quantity", "— 张"), ("detail", ""), ("status", ""))}
            self.side_vars[side] = values
            ttk.Label(card, textvariable=values["contract"], font=("Microsoft YaHei UI", 10, "bold"), wraplength=260).pack(anchor="w")
            ttk.Label(card, textvariable=values["quotes"], font=("Microsoft YaHei UI", 11, "bold"), foreground="#205974").pack(anchor="w", pady=(2, 0))
            ttk.Label(card, textvariable=self.display_text(values["amount"], "金额已隐藏"), font=("Microsoft YaHei UI", 15, "bold"), foreground="#123e53").pack(anchor="w", pady=(2, 0))
            price = ttk.Frame(card)
            self.entry_price_frames.append(price)
            ttk.Label(price, text="限价 $/份").pack(side="left")
            entry = ttk.Entry(price, textvariable=self.entry_limits[side], width=10)
            entry.pack(side="left", padx=5)
            self.entry_limit_entries[side] = entry
            self.side_buttons[side] = ttk.Button(card, text=direction_label("BUY", side), style="CompactBuy.TButton", command=lambda r=side: self.execute_side(r))
            self.side_buttons[side].pack(fill="x", pady=2)
            ttk.Label(card, textvariable=self.display_text(values["status"]), foreground="#945900", wraplength=260).pack(anchor="w", fill="x")
            detail = ttk.Label(card, textvariable=self.display_text(values["detail"]), foreground="#687680", wraplength=260)
            self.entry_detail_widgets.append(detail)
            card.bind("<Configure>", lambda event, frame=card: self.wrap_entry_card(frame, event.width - 16))
            self.entry_limits[side].trace_add("write", self.changed)
        self.buy_btn = self.side_buttons["C"]
        self.entry_limit_entry = self.entry_limit_entries["C"]
        self.entry_mode.trace_add("write", self.entry_mode_changed)
        self.entry_details_button = ttk.Button(top, text="追价/费用 ▾", style="Compact.TButton", command=self.toggle_entry_details)
        self.entry_details_button.pack(side="right")
        self.entry_explanation = ttk.Label(parent, text="自动中间价：仅改未成交部分，幅度相对初始限价；停止后原单保留，不转市价。支持买入开仓、卖出平多头。", foreground="#687680", wraplength=560)

    def toggle_entry_details(self, expanded=None):
        self.entry_details_expanded = not self.entry_details_expanded if expanded is None else expanded
        for widget in self.entry_detail_widgets + [self.entry_explanation]:
            widget.pack(anchor="w", fill="x") if self.entry_details_expanded else widget.pack_forget()
        self.entry_details_button.configure(text="收起明细 ▴" if self.entry_details_expanded else "追价/费用 ▾")
        self.update_entry_visibility()

    def update_entry_visibility(self):
        for side, price in zip(("C", "P"), self.entry_price_frames):
            if self.entry_mode.get() == "限价":
                price.pack(before=self.side_buttons[side], fill="x", pady=2)
            else:
                price.pack_forget()
        if self.entry_details_expanded and self.entry_mode.get() == "中间价（自动追价）":
            self.entry_follow_frame.pack(before=self.entry_match_label, fill="x", pady=(0, 3))
        else:
            self.entry_follow_frame.pack_forget()

    def automatic_mid_policy(self):
        from auto_mid import policy
        return policy({key: getattr(self, attr).get() if hasattr(self, attr) else default for key, attr, default in
                       (("interval", "mid_interval", 3), ("duration", "mid_duration", 300), ("drift_pct", "mid_drift", 10))})

    def start_auto_mid_selected(self):
        from app import ORDER_REQUESTS, CONTROL_REQUESTS
        order = self.selected_order()
        if not order or not self.connected or self.locked or self.has_pending(ORDER_REQUESTS | CONTROL_REQUESTS):
            return
        try:
            config = self.automatic_mid_policy()
            if order.get("order_type") != "LMT" or not order.get("modifiable"):
                raise ValueError("请选择本工具可改单的普通限价买入/平多头订单。")
            oid = order["order_id"]
            from auto_mid import AutoMidManager
            expected = dict(identity=AutoMidManager.identity(order), limit_price=order["limit_price"])
            session = self.session_epoch
            bound = order["limit_price"] * (1 + config["drift_pct"] / 100 if order["action"] == "BUY" else 1 - config["drift_pct"] / 100)
            accepted = messagebox.askyesno("开启自动中间价", f'{order["local_symbol"]} · {order["action"]}\n每 {config["interval"]:g} 秒检查，最长 {config["duration"]:g} 秒。\n买入上限/卖出下限 {bound:g} 美元/份。\n只改未成交部分；停止后原限价单继续保留。', parent=self.root)
            if accepted and session == self.session_epoch and (self.selected_order() or {}).get("order_id") == oid:
                self.mutate("auto_mid_start", order_id=oid, config=config, expected=expected)
        except ValueError as exc:
            self.status.set(str(exc))

    def stop_auto_mid_selected(self):
        order = self.selected_order()
        if order and self.connected:
            self.submit("auto_mid_stop", order_id=order["order_id"])

    @staticmethod
    def wrap_entry_card(frame, width):
        for child in frame.winfo_children():
            if isinstance(child, ttk.Label) and child.cget("wraplength"):
                child.configure(wraplength=max(140, width))

    def clear_entry_cards(self, reason):
        self.side_previews, self.side_fingerprints = {}, {}
        if not hasattr(self, "side_vars"):
            return
        action = OPEN_SIDES[self.open_side.get()]
        for side, values in self.side_vars.items():
            values["amount"].set("预计收入 —" if action == "SELL" else "预计支出 —")
            values["quantity"].set("— 张")
            values["quotes"].set("买一 —   卖一 —")
            values["contract"].set("等待匹配")
            values["status"].set("")
            values["detail"].set("")
            self.side_buttons[side].configure(text=direction_label(action, side), state="disabled")

    def reset_entry_prices(self):
        if not hasattr(self, "entry_limits"):
            return
        old = self.suppress_change
        self.suppress_change = True
        for variable in self.entry_limits.values():
            variable.set("")
        self.limit_conids = {}
        self.suppress_change = old

    def contract_input_changed(self, *_):
        if not self.suppress_change:
            self.reset_entry_prices()
            self.changed()

    def selection_mode_changed(self, *_):
        mode = self.selection_mode.get()
        if mode == BUDGET_SELECTION:
            self.budget_search_id += 1
        else:
            self.normal_selection_mode = mode
        self.contract_input_changed()

    def exact_strike_changed(self, *_):
        if not self.suppress_change:
            self.selected_conids = {}
            self.contract_input_changed()

    def entry_mode_changed(self, *_):
        if self.suppress_change:
            return
        if hasattr(self, "entry_follow_frame"):
            self.update_entry_visibility()
        # Worker seeds blank limits from a fresh routed midpoint. No UI-side
        # calculation from old or unqualified quotes can authorize an order.
        self.changed()
        if self.entry_mode.get() == "限价":
            self.match_text.set("正在获取所选合约的中间价；已有手填限价会保留。")

    def open_side_changed(self, *_):
        if self.suppress_change:
            return
        self.reset_entry_prices()
        action = OPEN_SIDES[self.open_side.get()]
        if action == "SELL" and self.selection_mode.get() == BUDGET_SELECTION:
            self.selection_mode.set(self.normal_selection_mode)
        if action == "SELL" and hasattr(self, "entry_options_button"):
            self.toggle_entry_options(True)
            self.toggle_entry_details(True)
        for side, frame in self.side_frames.items():
            frame.configure(text=direction_label(action, side))
            frame.grid_configure(column=0 if (side == "C") == (action == "BUY") else 1)
        self.changed()

    def budget_mode_changed(self, *_):
        amount_mode = self.budget_mode.get() == "金额 USD"
        variable = self.cash_amount if amount_mode else self.percent_input
        self.budget_input.configure(textvariable=variable, show="•" if amount_mode and getattr(self, "bean_mode", False) else "")
        if hasattr(self, "percent_scale"):
            self.percent_scale.configure(state="disabled" if amount_mode else "normal")
        self.percent_text.set("使用固定美元预算" if amount_mode else f"使用现金 {self.percent_input.get()}%")
        self.changed()

    def budget_summary_text(self):
        amount_mode = self.budget_mode.get() == "金额 USD"
        try:
            value = positive_number(self.cash_amount.get() if amount_mode else self.percent_input.get(), "预算")
        except ValueError:
            return "请输入有效美元金额" if amount_mode else "请输入有效比例"
        if not amount_mode and value > 100:
            return "比例须在 0% 至 100% 之间"
        basis = "固定金额" if amount_mode else f"可开仓资金 × {value:g}%"
        if not self.financial_ready or self.locked:
            return basis + " · 等待资金核验"
        cash = (self.snapshot_data.get("financial") or {}).get("cash")
        if cash is None or not math.isfinite(cash) or cash < 0:
            return basis + " · 等待资金核验"
        if self.bean_mode:
            return basis + " · 金额已隐藏"
        cap = min(value, cash) if amount_mode else cash * value / 100
        return f"{basis} · 找档上限 ${cap:,.2f}（含费）"

    def commit_budget(self, *_):
        if self.budget_mode.get() == "金额 USD":
            self.changed()
            return
        try:
            amount = positive_number(self.percent_input.get(), "现金比例")
            if amount > 100:
                raise ValueError("现金比例不能超过 100%。")
        except ValueError as exc:
            self.status.set(str(exc))
            return
        self.percent.set(amount)
        self.percent_text.set(f"使用现金 {amount:g}%")
        self.changed()

    def exit_budget_match(self):
        if self.selection_mode.get() == BUDGET_SELECTION:
            self.selection_mode.set(self.normal_selection_mode)
            self.request_preview()

    def refresh_entry_match(self):
        if self.selection_mode.get() == BUDGET_SELECTION:
            self.budget_search_id += 1
            self.changed()
        self.request_preview()

    def match_budget(self):
        if self.selection_mode.get() == BUDGET_SELECTION:
            self.exit_budget_match()
            return
        if OPEN_SIDES[self.open_side.get()] != "BUY":
            self.status.set("按预算找档适用于买入 Call 或 Put。")
            return
        self.selection_mode.set(BUDGET_SELECTION)
        self.request_preview()

    def extra_entry_settings(self):
        percent = positive_number(self.percent_input.get(), "现金比例") if self.budget_mode.get() == "比例 %" else 100.0
        if percent > 100:
            raise ValueError("现金比例不能超过 100%。")
        amount = positive_number(self.cash_amount.get(), "预算金额") if self.budget_mode.get() == "金额 USD" else None
        action = OPEN_SIDES[self.open_side.get()]
        automatic = self.entry_mode.get() == "中间价（自动追价）"
        if automatic and action != "BUY":
            raise ValueError("卖出开仓暂用固定中间价；自动中间价目前支持买入开仓与平多头。")
        quantity = int(self.seller_quantity.get()) if action == "SELL" else 1
        if quantity <= 0:
            raise ValueError("卖方开仓张数必须为正整数。")
        limits = {}
        for side, variable in self.entry_limits.items():
            text = variable.get().strip()
            limits[side] = positive_number(text, side + " 限价") if self.entry_mode.get() == "限价" and text else None
        selection = "strike" if self.selection_mode.get() == "指定行权价" else "depth"
        if self.selection_mode.get() == BUDGET_SELECTION:
            selection = "budget"
        strike = positive_number(self.exact_strike.get(), "行权价") if selection == "strike" else None
        result = dict(open_action=action, seller_quantity=quantity, percent=percent,
                    budget_mode="amount" if amount is not None else "percent", cash_amount=amount,
                    selection_mode=selection, strike=strike, conids=dict(self.selected_conids) if selection == "strike" else {},
                    limit_prices=limits, limit_conids=dict(self.limit_conids))
        if selection == "budget":
            result["budget_search_id"] = self.budget_search_id
        if automatic:
            result["auto_mid_config"] = self.automatic_mid_policy()
        return result

    def render_pair(self, results, context):
        valid = {side: value for side, value in results.items() if side in ("C", "P") and value and not value.get("error")}
        # Seed each blank field only if this is the unchanged form that requested
        # it. Its exact frozen LMT is the one held by the backend token.
        self.suppress_change = True
        try:
            for side, preview in valid.items():
                if preview.get("limit_seeded") and (not self.entry_limits[side].get().strip() or preview.get("limit_reseeded")):
                    self.entry_limits[side].set(self.price_text(preview["limit_price"]))
                if preview.get("price_mode") == "LMT" and preview["contract"].get("conid"):
                    self.limit_conids[side] = preview["contract"]["conid"]
        finally:
            self.suppress_change = False
        form = self.fingerprint()
        self.side_previews = valid
        self.side_fingerprints = {side: form for side in valid}
        self.preview_data = valid.get(self.right.get()) or next(iter(valid.values()), None)
        self.preview_fingerprint = form if valid else None
        self.match_text.set(f'{self.active_symbol} · 到期 {self.expiry.get()} · {self.trading_class.get()}')
        action = OPEN_SIDES[self.open_side.get()]
        for side in ("C", "P"):
            values, preview = self.side_vars[side], valid.get(side)
            if not preview:
                error = str((results.get(side) or {}).get("error", "等待该方向报价"))
                values["contract"].set("Call" if side == "C" else "Put")
                values["quotes"].set("买一 —   卖一 —")
                values["amount"].set("暂不可开仓")
                values["quantity"].set("— 张")
                values["status"].set(error)
                values["detail"].set("")
                self.side_buttons[side].configure(text=direction_label(action, side))
                continue
            contract = preview["contract"]
            quantity = preview["quantity"]
            values["contract"].set(f'{"Call" if side == "C" else "Put"} · 行权价 {contract["strike"]:g}')
            if preview.get("budget_rank"):
                values["contract"].set(values["contract"].get() + f' · 虚值第 {preview["budget_rank"]} 档')
            values["quotes"].set(f'买一 {self.price_text(preview["bid"])}   卖一 {self.price_text(preview["ask"])}')
            short = preview.get("opens_short", False)
            if short:
                credit = preview.get("estimated_credit", preview.get("estimated_premium", 0))
                values["amount"].set(f'预计收入 ${credit:,.2f}')
                margin = preview.get("margin_required", preview.get("initial_margin_change", 0))
                cost_detail = f'保证金预估 ${margin:,.2f} + 费用预留 ${preview["fee_reserve"]:,.2f}'
                if preview.get("margin_checked_at"):
                    cost_detail += f'\n保证金核验 {time.strftime("%H:%M:%S", time.localtime(preview["margin_checked_at"]))}；发单前再核验。'
                else:
                    cost_detail += '\n卖方收入不等于可承受亏损。'
            else:
                values["amount"].set(f'预计支出 ${preview["buffered_cost"]:,.2f}')
                cost_detail = f'权利金 ${preview["estimated_premium"]:,.2f} + 费用预留 ${preview["fee_reserve"]:,.2f}'
            values["quantity"].set(f'{quantity} 张' + (" · 卖出开仓" if short else " · 买入开仓"))
            execution = "市价" if preview["order_type"] == "MKT" else f'限价 {self.price_text(preview["limit_price"])}'
            if preview.get("price_mode") == "MID":
                execution = "中间价固定" + execution
            if self.entry_mode.get() == "中间价（自动追价）":
                config = self.automatic_mid_policy()
                execution = f'自动中间价 {self.price_text(preview["limit_price"])} · 每 {config["interval"]:g} 秒\n买入上限 {self.price_text(preview["limit_price"] * (1 + config["drift_pct"] / 100))}'
            status = f'{execution} · {preview.get("session_label", "")} · DAY'
            if quantity == 0:
                status += "\n当前预算不足 1 张（含费用）；请调整预算或合约。"
            values["status"].set(status)
            values["detail"].set(cost_detail + f'\n{time.strftime("%H:%M:%S", time.localtime(preview["created_at"]))} · 预览有效 10 秒')
            self.side_buttons[side].configure(text=direction_label(action, side) + f' {quantity} 张')
        if any(v.get("scan_pending") for v in results.values() if isinstance(v, dict)):
            self.preview_dirty = True
            self.status.set("正在按预算逐档查询，已取得的候选显示在对应一侧。")
            return
        self.status.set("双方向预览已更新；下单前重新核验行情、资金及订单。" if valid else "两侧暂不可开仓，详见对应合约提示。")

    def execute_side(self, side):
        from app import ORDER_REQUESTS, CONTROL_REQUESTS
        preview = self.side_previews.get(side)
        if (not preview or self.entry_confirmation_open or not self.connected or self.locked
                or not self.financial_ready or self.pending_symbol
                or self.has_pending(ORDER_REQUESTS | CONTROL_REQUESTS)):
            return
        try:
            valid = self.fingerprint() == self.side_fingerprints.get(side) and time.time() - preview["created_at"] <= 10 and preview["quantity"] > 0
        except (ValueError, tk.TclError):
            valid = False
        if not valid:
            self.clear_preview("该方向预览已失效，正在重新获取。")
            self.preview_dirty = True
            return
        epoch = self.session_epoch
        if preview.get("opens_short"):
            self.entry_confirmation_open = True
            self.update_enabled()
            try:
                text = (f'账户：{preview.get("account", "")}\n卖出开仓：{preview["contract"]["local_symbol"]} · {preview["quantity"]} 张\n'
                        f'预计收入 ${preview.get("estimated_credit", 0):,.2f}\n'
                        f'初始保证金变动 ${preview.get("initial_margin_change", 0):,.2f}\n'
                        '这会建立空头期权，不是平已有多头。卖 Call 潜在亏损无上限；卖 Put 潜在亏损可能远高于收入。\n确认按预览委托开仓？')
                if not messagebox.askyesno("确认卖出期权开仓", text, parent=self.root):
                    return
            finally:
                self.entry_confirmation_open = False
                self.update_enabled()
            try:
                form_unchanged = self.fingerprint() == self.side_fingerprints.get(side)
            except (ValueError, tk.TclError):
                form_unchanged = False
            if (epoch != self.session_epoch or not self.connected or self.locked
                    or not self.financial_ready or self.pending_symbol
                    or self.has_pending(ORDER_REQUESTS | CONTROL_REQUESTS)
                    or time.time() - preview["created_at"] > 10
                    or not form_unchanged):
                self.status.set("卖方预览已失效，请重新预览并确认。")
                return
        args = dict(token=preview["token"])
        if self.entry_mode.get() == "中间价（自动追价）":
            config = self.automatic_mid_policy()
            cap = preview.get("cash") if self.budget_mode.get() == "比例 %" and self.rounding_mode.get() == "四舍五入" else preview.get("budget")
            config["budget_cap"] = preview.get("budget") if preview.get("budget_rank") else cap
            args["auto_mid"] = config
        self.clear_preview("开仓请求正在处理，请查看订单状态。")
        self.submit("buy", **args)

    def select_chain_contract(self, event):
        row_id = self.chain_tree.identify_row(event.y)
        column = self.chain_tree.identify_column(event.x)
        row = self.chain_rows_by_id.get(row_id)
        if not row or column not in ("#1", "#2", "#3", "#4", "#6", "#7", "#8", "#9"):
            return
        side = "C" if int(column[1:]) < 5 else "P"
        self.suppress_change = True
        try:
            self.selection_mode.set("指定行权价")
            self.exact_strike.set(self.price_text(row["strike"]))
            self.right.set(side)
            self.selected_conids = {r: (row.get(key) or {}).get("conid") for r, key in (("C", "call"), ("P", "put")) if (row.get(key) or {}).get("conid")}
        finally:
            self.suppress_change = False
        self.reset_entry_prices()
        self.changed()
        self.status.set(f'已选行权价 {row["strike"]:g} · {"Call" if side == "C" else "Put"}，仅选择合约，尚未下单。')
        if getattr(self, "panel_focus_active", False):
            self.restore_panel_focus()
        self.request_preview()

    def visible_chain_strikes(self):
        if not self.chain_rows_by_id or self.chain_center_pending:
            return None
        ids = self.chain_tree.get_children()
        top = int(self.chain_tree.yview()[0] * len(ids))
        visible = self.chain_tree.visible_rows() if hasattr(self.chain_tree, "visible_rows") else max(1, (self.chain_tree.winfo_height() - 30) // self.chain_row_height)
        return [self.chain_rows_by_id[iid]["strike"] for iid in ids[max(0, top - 3):top + visible + 3] if iid in self.chain_rows_by_id]

