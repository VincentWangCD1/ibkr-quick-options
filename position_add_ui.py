"""Position add ticket. All Tk controls stay on the UI thread."""
import time
import tkinter as tk
import localized_widgets as ttk
from uuid import uuid4

from contract_display import option_label
from popup_position import PointerPopup


METHODS = {"按张数": ("lots", "张", (1, 2, 5)), "按美元": ("amount", "USD", (100, 250, 500)),
           "按原始本金比例": ("principal", "%", (25, 50, 100))}


class PositionAddUiMixin:
    def open_position_add(self):
        from app import CONTROL_REQUESTS, ORDER_REQUESTS
        p = self.selected_position()
        if (not p or not self.connected or not self.financial_ready or self.locked or
                (p.get("close_pending") and not p.get("can_add_with_stop")) or self.has_pending(CONTROL_REQUESTS | ORDER_REQUESTS)):
            self.status.set("请连接可开仓账户，选中持仓并核对未完成委托。")
            return
        self.dismiss_position_close()
        self.dismiss_position_add()
        dialog = ttk.Toplevel(self.root)
        placement = PointerPopup(dialog, center_on_parent=True)
        dialog.title("加仓 · " + option_label(p))
        dialog.transient(self.root)
        dialog.resizable(False, False)
        panel = ttk.Frame(dialog, padding=16)
        panel.pack(fill="both", expand=True)
        side = "买入加仓" if p["quantity"] > 0 else "卖出加仓（扩大空头）"
        state = dict(window=dialog, id=uuid4().hex, conid=p["conid"], held=p["quantity"], side=side,
                     method=self.var("按张数"), value=self.var("1"), principal=self.var(""),
                     pricing=self.var("固定中间价"), limit=self.var(""), unit=self.var("张"),
                     text=self.var(""), preview=None, fingerprint=None)
        self.position_add_dialog = state
        ttk.Label(panel, text=option_label(p), font=("Microsoft YaHei UI", 13, "bold")).grid(
            row=0, column=0, columnspan=4, sticky="w")
        account = self.snapshot_data.get("account", "")
        ttk.Label(panel, text=f'{self.session_mode} · {account} · {side} · 当前 {abs(p["quantity"]):g} 张').grid(
            row=1, column=0, columnspan=4, sticky="w", pady=(0, 10))
        ttk.Label(panel, text="加仓方式").grid(row=2, column=0, sticky="w")
        ttk.Combobox(panel, textvariable=state["method"], values=list(METHODS), state="readonly",
                     width=16).grid(row=2, column=1, sticky="w", padx=5)
        ttk.Entry(panel, textvariable=state["value"], width=10).grid(row=2, column=2)
        ttk.Label(panel, textvariable=state["unit"], width=5).grid(row=2, column=3, sticky="w", padx=4)
        presets = ttk.Frame(panel)
        presets.grid(row=3, column=1, columnspan=3, sticky="w", pady=5)
        state["presets"] = [ttk.Button(presets, width=7) for _ in range(3)]
        for btn in state["presets"]:
            btn.pack(side="left", padx=(0, 4))
        basis = ttk.Frame(panel)
        basis.grid(row=4, column=0, columnspan=4, sticky="w", pady=4)
        state["basis_row"] = basis
        ttk.Label(basis, text="原始本金 USD").pack(side="left")
        ttk.Entry(basis, textvariable=state["principal"], width=12).pack(side="left", padx=5)
        ttk.Label(basis, text="留空按首笔买单成本" if p["quantity"] > 0 else "填写首次开仓投入的保证金").pack(side="left")
        ttk.Label(panel, text="委托方式").grid(row=5, column=0, sticky="w")
        ttk.Combobox(panel, textvariable=state["pricing"], values=("固定中间价", "手动限价"),
                     state="readonly", width=16).grid(row=5, column=1, sticky="w", padx=5)
        state["limit_entry"] = ttk.Entry(panel, textvariable=state["limit"], width=10)
        state["limit_entry"].grid(row=5, column=2)
        ttk.Label(panel, text="$/份").grid(row=5, column=3, sticky="w", padx=4)
        ttk.Separator(panel).grid(row=6, column=0, columnspan=4, sticky="ew", pady=(12, 6))
        ttk.Label(panel, textvariable=state["text"], wraplength=410, justify="left",
                  width=49).grid(row=7, column=0, columnspan=4, sticky="w")
        actions = ttk.Frame(panel)
        actions.grid(row=8, column=0, columnspan=4, sticky="ew", pady=(10, 0))
        ttk.Button(actions, text="刷新预览", command=self.request_position_add).pack(side="left")
        ttk.Button(actions, text="关闭", command=self.dismiss_position_add).pack(side="right")
        state["submit_btn"] = ttk.ClassicButton(panel, text="等待加仓预览", command=self.submit_position_add,
                                        font=("Microsoft YaHei UI", 12, "bold"), bg="#195b74",
                                        fg="white", padx=12, pady=6, state="disabled")
        state["submit_btn"].grid(row=9, column=0, columnspan=4, sticky="ew", pady=(8, 0))
        for key in ("value", "principal", "pricing", "limit"):
            state[key].trace_add("write", self.position_add_changed)
        state["method"].trace_add("write", self.position_add_method_changed)
        dialog.protocol("WM_DELETE_WINDOW", self.dismiss_position_add)
        dialog.bind("<Escape>", lambda _: self.dismiss_position_add())
        dialog.bind("<Return>", lambda _: self.request_position_add())
        self.position_add_method_changed()
        self.request_position_add()
        placement.show()

    def dismiss_position_add(self):
        s = getattr(self, "position_add_dialog", None)
        self.position_add_dialog = None
        if s:
            s["window"].destroy()

    def position_add_method_changed(self, *_):
        s = self.position_add_dialog
        mode, unit, presets = METHODS[s["method"].get()]
        s["unit"].set(unit)
        for btn, preset in zip(s["presets"], presets):
            btn.configure(text=f"{preset}{unit}" if mode != "amount" else f"${preset}",
                          command=lambda v=preset: s["value"].set(str(v)))
        if mode == "principal":
            s["basis_row"].grid()
        else:
            s["basis_row"].grid_remove()
        s["value"].set(str(presets[0]))

    def position_add_changed(self, *_):
        s = self.position_add_dialog
        s["preview"] = None
        s["submit_btn"].configure(text="等待加仓预览", state="disabled")
        s["text"].set("请刷新预览，核对本次张数与预算。")
        s["limit_entry"].configure(state="normal" if s["pricing"].get() == "手动限价" else "disabled")

    def add_form_fingerprint(self):
        s = self.position_add_dialog
        if s:
            return (s["id"], *(s[key].get() for key in ("method", "value", "principal", "pricing", "limit")),
                    *(v.get() for v in (self.fee, self.min_fee, self.cap)))

    def request_position_add(self):
        from app import ORDER_REQUESTS, CONTROL_REQUESTS
        s = self.position_add_dialog
        if not s or self.locked or not self.financial_ready or self.has_pending(ORDER_REQUESTS | CONTROL_REQUESTS):
            return
        self.position_add_changed()
        if self.positions_by_id.get(str(s["conid"]), {}).get("quantity") != s["held"]:
            s["text"].set("持仓数量已变化，请关闭窗口并重新选择持仓。")
            return
        try:
            req = dict(method=METHODS[s["method"].get()][0], value=s["value"].get().strip(),
                       principal=s["principal"].get().strip(), limit_price=s["limit"].get().strip(),
                       order_mode="MID" if s["pricing"].get() == "固定中间价" else "LMT",
                       commission_per_contract=float(self.fee.get()), min_commission=float(self.min_fee.get()),
                       max_contracts=int(self.cap.get()))
        except ValueError:
            s["text"].set("请核对开仓参数中的手续费与每笔张数上限。")
            return
        s["text"].set("正在核对选中合约、持仓、盘口和资金…")
        self.submit("preview_add", conid=s["conid"], expected=s["held"], request=req)

    def render_position_add(self, result):
        s = self.position_add_dialog
        if not s:
            return
        size = result["quantity"]
        held = abs(result["position_quantity"])
        rows = [f'{s["side"]} {size} 张 · 全部成交后 {held:g} → {held + size:g} 张',
                f'买一 {result["bid"]:g} / 卖一 {result["ask"]:g} · 委托限价 {result["limit_price"]:g} USD/份']
        if result.get("principal") is not None:
            rows.append(f'原始本金 ${result["principal"]:,.2f}（{result["principal_source"]}）× {result["add_value"]:g}%')
        if result["add_method"] != "lots":
            rows.append(f'指定预算 ${result["requested_budget"]:,.2f} · 可用预算 ${result["budget"]:,.2f}')
        cost = "预计新增保证金及费用" if result["opens_short"] else "预计支出"
        rows.extend([f'{cost} ${result["buffered_cost"]:,.2f}（含预留费用 ${result["fee_reserve"]:,.2f}）',
                     f'{result["session_label"]} · DAY · 预览 10 秒有效'])
        s.update(preview=result, fingerprint=self.add_form_fingerprint())
        s["text"].set("\n".join([*rows[:2], rows[-1]] if getattr(self, "bean_mode", False) else rows))
        s["submit_btn"].configure(text=f'确认{s["side"]} {size} 张')
        self.update_enabled()

    def add_preview_usable(self, ready):
        s = self.position_add_dialog
        if not s or not s.get("preview"):
            return False
        p = self.positions_by_id.get(str(s["conid"]), {})
        return bool(ready and not self.locked and self.financial_ready
                    and (not p.get("close_pending") or p.get("can_add_with_stop"))
                    and p.get("quantity") == s["held"] and s["fingerprint"] == self.add_form_fingerprint()
                    and time.time() - s["preview"]["created_at"] <= 10)

    def submit_position_add(self):
        from app import ORDER_REQUESTS, CONTROL_REQUESTS
        if not self.add_preview_usable(self.connected and not self.has_pending(ORDER_REQUESTS | CONTROL_REQUESTS)):
            return
        s = self.position_add_dialog
        token = s["preview"]["token"]
        s["preview"] = None
        s["submit_btn"].configure(text="正在提交加仓委托…", state="disabled")
        self.mutate("add_confirm", token=token)
