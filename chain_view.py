"""Virtualized option-chain display with fixed Call / strike / Put zones.

The small Treeview-compatible surface lets existing contract selection and
subscription logic keep their stable identities. This widget never trades.
"""
import math
import tkinter as tk
import localized_widgets as ttk
from tkinter import font as tkfont

from ui_state import quote_age_text


def chain_columns(width):
    width = max(1, width)
    center = min(116, width * .16)
    side = (width - center) / 2
    return side, side + center, (side / 3, side / 2, side * 5 / 6)


def price_label(value):
    try:
        numeric = float(value)
        if not math.isfinite(numeric) or numeric < 0 or numeric >= 1e100:
            return "—"
        return f"{numeric:.2f}" if abs(numeric * 100 - round(numeric * 100)) < 1e-7 else f"{numeric:.3f}"
    except (ValueError, TypeError):
        return "—"


def short_status(quote, zone):
    kind = str(quote.get("quote_type") or "等待报价")
    if "延迟" in kind:
        state = "延迟"
    elif "冻结" in kind:
        state = "冻结"
    elif "无权限" in kind:
        state = "待报价/权限"
    elif "久未" in kind:
        state = "盘口较旧"
    elif "实时" in kind:
        state = "实时"
    elif "演示" in kind:
        state = "演示"
    else:
        state = "待报价"
    ages = []
    for key in ("bid_age", "ask_age"):
        try:
            value = float(quote.get(key))
            ages.append(f"{value:.0f}s" if math.isfinite(value) and value >= 0 else "—")
        except (ValueError, TypeError):
            ages.append("—")
    return f"{zone} · {state} · 买 {ages[0]} / 卖 {ages[1]}"


class OptionChainView(ttk.Frame):
    row_height = 22
    header_height = 42

    def __init__(self, parent):
        super().__init__(parent)
        self.price_font = tkfont.Font(family="Microsoft YaHei UI", size=10)
        self.small_font = tkfont.Font(family="Microsoft YaHei UI", size=8)
        self.title_font = tkfont.Font(family="Microsoft YaHei UI", size=10, weight="bold")
        self.row_height = max(22, self.price_font.metrics("linespace") + 2)
        self.header_height = max(42, self.title_font.metrics("linespace") + self.small_font.metrics("linespace") + 6)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)
        self.header = tk.Canvas(self, height=self.header_height, background="#f6f8fb", highlightthickness=0)
        self.header.grid(row=0, column=0, sticky="ew")
        self.canvas = tk.Canvas(self, height=self.row_height * 14, background="white", highlightthickness=0, yscrollincrement=self.row_height, takefocus=True)
        self.canvas.grid(row=1, column=0, sticky="nsew")
        self.scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.yview)
        self.scrollbar.grid(row=0, column=1, rowspan=2, sticky="ns")
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.detail_var = tk.StringVar(value="双击报价选合约（不下单） · 浅底色=实值 · ! 表示延迟/冻结/陈旧等状态；悬停查看详情。")
        ttk.Label(self, textvariable=self.detail_var, foreground="#667887", font=("Microsoft YaHei UI", 8)).grid(row=2, column=0, columnspan=2, sticky="ew", pady=(3, 0))
        self._ids, self._rows, self._indices = [], {}, {}
        self.contract_rows, self.reference_meta = {}, {}
        self._selected, self._side = "", ""
        self._pending_draw = None
        self.canvas.bind("<Configure>", lambda _: self._schedule())
        self.canvas.bind("<MouseWheel>", self._wheel)
        self.canvas.bind("<Button-1>", self._click)
        self.canvas.bind("<Motion>", self._hover)
        self.canvas.bind("<Leave>", lambda _: self._update_detail())
        for key, direction in (("<Prior>", -1), ("<Next>", 1)):
            self.canvas.bind(key, lambda _, d=direction: self.yview("scroll", d, "pages"))
        self.canvas.bind("<Home>", lambda _: self.yview_moveto(0))
        self.canvas.bind("<End>", lambda _: self.yview_moveto(1))

    def bind(self, sequence=None, func=None, add=None):
        if hasattr(self, "canvas") and sequence in ("<Double-1>", "<Button-3>"):
            return self.canvas.bind(sequence, func, add)
        return super().bind(sequence, func, add)

    def destroy(self):
        if self._pending_draw is not None:
            self.after_cancel(self._pending_draw)
            self._pending_draw = None
        super().destroy()

    def set_context(self, rows, metadata=None):
        self.contract_rows = rows
        self.reference_meta = metadata if isinstance(metadata, dict) else {}
        self._update_detail()
        self._schedule()

    def get_children(self):
        return tuple(self._ids)

    def delete(self, *ids):
        removed = set(ids)
        self._ids = [key for key in self._ids if key not in removed]
        for key in removed:
            self._rows.pop(key, None)
        self._reindex()
        if self._selected in removed:
            self._selected, self._side = "", ""
            self._update_detail()
        self._schedule()

    def insert(self, parent, index, iid, **options):
        if iid in self._rows:
            raise ValueError("Duplicate option-chain row")
        index = len(self._ids) if index == "end" else int(index)
        self._rows[iid] = options
        self._ids.insert(index, iid)
        if index == len(self._ids) - 1:
            self._indices[iid] = index
        else:
            self._reindex()
        self._schedule()
        return iid

    def item(self, iid, **options):
        if options:
            self._rows[iid].update(options)
            self._schedule()
        return self._rows[iid]

    def move(self, iid, parent, index):
        index = len(self._ids) - 1 if index == "end" else int(index)
        if self._indices.get(iid) != index:
            self._ids.remove(iid)
            self._ids.insert(index, iid)
            self._reindex()
            self._schedule()

    def _reindex(self):
        self._indices = {key: index for index, key in enumerate(self._ids)}

    def _region(self):
        self.canvas.configure(scrollregion=(0, 0, 1, max(1, len(self._ids) * self.row_height)))

    def yview(self, *args):
        if not args:
            return self.canvas.yview()
        self._region()
        self.canvas.yview(*args)
        self._schedule()

    def yview_moveto(self, fraction):
        self.yview("moveto", fraction)

    def visible_rows(self):
        return max(1, self.canvas.winfo_height() // self.row_height)

    def identify_row(self, y):
        if y < 0 or y >= self.canvas.winfo_height():
            return ""
        index = int(self.canvas.canvasy(y) // self.row_height)
        return self._ids[index] if 0 <= index < len(self._ids) else ""

    def identify_column(self, x):
        width = self.canvas.winfo_width()
        left, right, _ = chain_columns(width)
        if not 0 <= x < width:
            return ""
        if x < left:
            return "#" + str(min(3, int(x / (left / 3)) + 1))
        if x < right:
            return "#5"
        return "#" + str(min(8, int((x - right) / ((width - right) / 3)) + 6))

    def _wheel(self, event):
        if event.delta:
            self.yview("scroll", -int(math.copysign(max(1, abs(event.delta) // 120), event.delta)), "units")
        return "break"

    def _click(self, event):
        self.canvas.focus_set()
        key = self.identify_row(event.y)
        if key not in self.contract_rows:
            return
        col = self.identify_column(event.x)
        self._selected = key
        self._side = "C" if col in ("#1", "#2", "#3") else "P" if col in ("#6", "#7", "#8") else ""
        self._update_detail()
        self._schedule()

    def _update_detail(self):
        self._show_detail(self._selected)

    def _hover(self, event):
        self._show_detail(self.identify_row(event.y))

    def _show_detail(self, key):
        row = self.contract_rows.get(key)
        if row:
            self.detail_var.set(f'行权价 {row["strike"]:g}  |  Call：{quote_age_text(row.get("call") or {})}  |  Put：{quote_age_text(row.get("put") or {})}')
        else:
            self.detail_var.set("双击报价选合约（不下单） · 浅底色=实值 · ! 表示延迟/冻结/陈旧等状态；悬停查看详情。")

    def _schedule(self):
        if self._pending_draw is None:
            self._pending_draw = self.after_idle(self.draw)

    def _text(self, canvas, x, y, text, font, color, width):
        text = ttk.display(self, str(text))
        if font.measure(text) > width:
            while text and font.measure(text + "…") > width:
                text = text[:-1]
            text += "…"
        canvas.create_text(x, y, text=text, font=font, fill=color, anchor="center")

    def draw(self):
        self._pending_draw = None
        self._region()
        c, h = self.canvas, self.header
        width = max(1, c.winfo_width())
        if width < 100:
            return
        left, right, _ = chain_columns(width)
        side_width, col_width = left, left / 3
        h.delete("all")
        for x0, x1, title, color, bg in ((0, left, "看涨期权  CALL", "#245b86", "#edf4fb"), (right, width, "看跌期权  PUT", "#655184", "#f3eff8")):
            h.create_rectangle(x0, 0, x1, self.header_height, fill=bg, outline="")
            self._text(h, (x0 + x1) / 2, self.header_height * .27, title, self.title_font, color, side_width - 12)
            for index, label in enumerate(("买价 / 买一", "卖价 / 卖一", "最新成交")):
                self._text(h, x0 + (index + .5) * col_width, self.header_height * .76, label, self.small_font, "#536675", col_width - 8)
        h.create_rectangle(left, 0, right, self.header_height, fill="#dfe6ec", outline="")
        self._text(h, (left + right) / 2, self.header_height / 2, "行权价", self.title_font, "#2f4556", right - left - 8)
        c.delete("all")
        top = c.canvasy(0)
        first = max(0, int(top // self.row_height))
        last = min(len(self._ids), first + self.visible_rows() + 2)
        if not self._ids:
            self._text(c, width / 2, 45, "等待所选到期日的期权链；缺少报价显示 —", self.small_font, "#6f7d86", width - 24)
        for index in range(first, last):
            key = self._ids[index]
            record = self._rows[key]
            values = record.get("values", ("—",) * 9)
            tags = record.get("tags", ())
            y = index * self.row_height
            middle = y + self.row_height / 2
            if "reference" in tags:
                c.create_rectangle(0, y, width, y + self.row_height, fill="#2f526c", outline="")
                label = "估算参考" if self.reference_meta.get("estimated") else "标的参考"
                self._text(c, left / 2, middle, label + "（仅用于选档）", self.small_font, "#e6eef6", side_width - 12)
                self._text(c, (left + right) / 2, middle, values[4], self.title_font, "white", right - left - 8)
                self._text(c, right + side_width / 2, middle, "浅底色为实值区", self.small_font, "#e6eef6", side_width - 12)
                continue
            row = self.contract_rows.get(key, {})
            for side, x0, x1, data_index, status_index, quote_key in (("C", 0, left, 0, 3, "call"), ("P", right, width, 5, 8, "put")):
                zone = str(values[status_index]).split(" · ")[0]
                bg = ("#edf5fc" if side == "C" else "#f2eef8") if zone == "实值" else ("#ffffff" if index % 2 else "#fafbfc")
                c.create_rectangle(x0, y, x1, y + self.row_height, fill=bg, outline="")
                for col in range(3):
                    text = price_label(values[data_index + col])
                    self._text(c, x0 + (col + .5) * col_width, middle, text, self.price_font, "#263e51" if col < 2 else "#6c7b87", col_width - 20)
                quote = row.get(quote_key) or {}
                caution = any(word in str(quote.get("quote_type", "")) for word in ("延迟", "冻结", "等待", "无权限", "久未"))
                if caution:
                    self._text(c, x0 + 10, middle, "!", self.title_font, "#9c6b27", 15)
                if key == self._selected and side == self._side:
                    c.create_rectangle(x0 + 1, y + 1, x1 - 1, y + self.row_height - 1, outline="#3c82b5" if side == "C" else "#8d6bae", width=2)
            c.create_rectangle(left, y, right, y + self.row_height, fill="#e6ebef", outline="")
            self._text(c, (left + right) / 2, middle, values[4], self.title_font, "#233c50", right - left - 8)
            if "matched" in tags:
                c.create_line(left + 8, y + self.row_height - 2, right - 8, y + self.row_height - 2, fill="#3c82b5", width=2)
            c.create_line(0, y + self.row_height, width, y + self.row_height, fill="#dce4ea")
