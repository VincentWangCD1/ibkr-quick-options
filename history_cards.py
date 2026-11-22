"""Virtualized daily execution cards: amount, commission and P&L stay visible."""
from bisect import bisect_right
from collections import defaultdict
import tkinter as tk
import localized_widgets as ttk
from contract_display import pnl_color
from history_groups import order_rows, trade_recap


def amount(value, currency="USD", signed=False):
    if value is None:
        return "—"
    prefix = "$" if currency == "USD" else currency + " "
    return ("-" if value < 0 else "+" if signed and value > 0 else "") + prefix + f"{abs(value):,.2f}"


def contract_description(row, expiry_label="到期"):
    expiry = row.get("expiry", "")
    expiry = f"{expiry[:4]}-{expiry[4:6]}-{expiry[6:8]}" if len(expiry) == 8 else expiry
    right = {"C": "Call", "P": "Put"}.get(row.get("right"), row.get("right", ""))
    if right:
        expiry = f"{expiry_label} {expiry}" if expiry else ""
        strike = row.get("strike")
        return (f'{expiry}  {strike:g} {right}' if strike is not None else f'{expiry} {right}').strip()
    return row.get("local_symbol") or row.get("sec_type", "")


def quote_amount(value, currency="USD"):
    if value is None:
        return "—"
    digits = f"{abs(value):,.8f}".rstrip("0")
    if digits.endswith("."):
        digits += "00"
    elif len(digits.rsplit(".", 1)[-1]) == 1:
        digits += "0"
    return ("-" if value < 0 else "") + ("$" if currency == "USD" else currency + " ") + digits


def average_quote(value, currency="USD"):
    return quote_amount(round(value, 4), currency) if value is not None else "—"


def grouped_cards(rows):
    days = defaultdict(list)
    for row in rows:
        days[row.get("date", "未知日期")].append(row)
    records = []
    for day, entries in sorted(days.items(), reverse=True):
        totals = defaultdict(float)
        missing = 0
        for row in entries:
            if row.get("realized_pnl") is None:
                missing += 1
            else:
                totals[row.get("pnl_currency") or row.get("currency") or "?"] += row["realized_pnl"]
        records.append(("day", dict(date=day, count=len(entries), totals=totals, missing=missing)))
        records.extend(("trade", row) for row in sorted(entries, key=lambda value: value.get("timestamp", ""), reverse=True))
    return records


class HistoryCards(ttk.Frame):
    def __init__(self, parent, on_share=None):
        super().__init__(parent)
        self.green_profit = False
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self.canvas = ttk.TextCanvas(self, background="white", highlightthickness=0, yscrollincrement=20)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(self, orient="vertical", command=self.scroll)
        scroll.grid(row=0, column=1, sticky="ns")
        self.canvas.configure(yscrollcommand=scroll.set)
        self.canvas.bind("<Configure>", lambda _: self.draw())
        self.canvas.bind("<MouseWheel>", self.wheel)
        self.canvas.bind("<Button-1>", self.click)
        self.on_share = on_share
        self.expanded = set()
        self.rows, self.groups, self.returns = [], {}, {}
        self.records, self.tops = [], []

    def set_data(self, rows, returns=None):
        self.rows = rows
        self.groups = order_rows(rows)
        self.expanded.intersection_update(self.groups)
        self.returns = {}
        self.fill_returns = {}
        by_day = defaultdict(list)
        for key, order in self.groups.items():
            result = trade_recap(order["fills"], returns or {})
            if result:
                self.returns[key] = result
            if len(order["fills"]) > 1:
                for fill in order["fills"]:
                    detail = trade_recap([fill], returns or {})
                    if detail:
                        self.fill_returns[fill["exec_id"]] = detail
            by_day[order["date"]].append(order)
        self.base_records = []
        for kind, day in grouped_cards(rows):
            if kind == "day" and day["date"] in by_day:
                self.base_records.append((kind, day))
                orders = sorted(by_day[day["date"]], key=lambda r: (r["timestamp"], r["exec_id"]), reverse=True)
                self.base_records.extend(("order", order) for order in orders)
        self.layout()

    def toggle(self, key):
        self.expanded.symmetric_difference_update({key})
        self.layout()

    def click(self, event):
        index = bisect_right(self.tops, self.canvas.canvasy(event.y)) - 1
        if not (0 <= index < len(self.records)):
            return
        kind, row = self.records[index]
        if kind == "day":
            return
        offset = self.canvas.canvasy(event.y) - self.tops[index]
        if offset >= 106:
            return
        recap = self.recap_for(kind, row)
        if recap and 77 <= offset <= 103 and event.x >= self.canvas.winfo_width() - 150 and self.on_share:
            self.on_share(row, recap)
        elif kind == "order" and len(row["fills"]) > 1:
            self.toggle(row["key"])

    def recap_for(self, kind, row):
        if kind == "fill":
            return self.fill_returns.get(row["exec_id"])
        return self.returns.get(row["key"])

    def layout(self):
        self.records = []
        for kind, row in self.base_records:
            self.records.append((kind, row))
            if kind == "order" and row["key"] in self.expanded:
                self.records.extend(("fill", item) for item in row["fills"])
        self.tops = []
        top = 0
        for kind, _ in self.records:
            self.tops.append(top)
            top += 36 if kind == "day" else 106
        self.canvas.configure(scrollregion=(0, 0, 1, max(1, top)))
        self.draw()

    def scroll(self, *args):
        self.canvas.yview(*args)
        self.draw()

    def wheel(self, event):
        self.scroll("scroll", -int(event.delta / 120), "units")
        return "break"

    def draw(self):
        canvas = self.canvas
        canvas.delete("row")
        width, height = max(600, canvas.winfo_width()), max(150, canvas.winfo_height())
        top = canvas.canvasy(0)
        first = max(0, bisect_right(self.tops, top) - 1)
        if not self.records:
            canvas.create_text(20, 35, anchor="w", text="所选范围尚无已取得的成交记录。可刷新近期成交，或同步 IBKR 历史报表。", fill="#63727e", tags="row")
        for index in range(first, len(self.records)):
            y = self.tops[index]
            if y > top + height:
                break
            kind, row = self.records[index]
            if kind == "day":
                canvas.create_rectangle(0, y, width, y + 36, fill="#edf4f8", outline="", tags="row")
                canvas.create_text(16, y + 18, anchor="w", text=row["date"] + f'  ·  {row["count"]} 笔', font=("Microsoft YaHei UI", 11, "bold"), fill="#334d5d", tags="row")
                if getattr(self, "hide_amounts", False):
                    continue
                total = "  /  ".join(amount(value, currency, True) for currency, value in sorted(row["totals"].items())) or "—"
                label = "已知盈亏 " if row["missing"] else "当日盈亏 "
                canvas.create_text(width - 18, y + 18, anchor="e", text=label + total, font=("Microsoft YaHei UI", 11, "bold"), fill="#235b60", tags="row")
                continue
            middle, right = width * .64, width - 20
            symbol = row.get("symbol") or row.get("local_symbol") or "—"
            multiple = kind == "order" and len(row["fills"]) > 1
            left = 42 if kind == "fill" else 18
            if kind == "fill":
                canvas.create_rectangle(0, y, width, y + 105, fill="#f6f9fb", outline="", tags="row")
            if multiple:
                arrow = "▾" if row["key"] in self.expanded else "▸"
                symbol = f'{arrow}  {symbol}  ·  {len(row["fills"])} 笔成交'
            canvas.create_text(left, y + 22, anchor="w", text=symbol, font=("Microsoft YaHei UI", 14, "bold"), fill="#202a32", tags="row")
            canvas.create_text(left, y + 46, anchor="w", text=contract_description(row), font=("Microsoft YaHei UI", 10), fill="#525d67", tags="row")
            action = "买入" if row.get("action") in ("BUY", "BOT") else "已卖出"
            canvas.create_text(left, y + 70, anchor="w", text=f'{action} {row.get("quantity", 0):g}', font=("Microsoft YaHei UI", 12), fill="#226e9d" if action == "买入" else "#b53548", tags="row")
            detail = " · ".join(str(row.get(key)) for key in ("order_type", "tif", "exchange") if row.get(key))
            canvas.create_text(left, y + 92, anchor="w", text=detail or row.get("sec_type", ""), font=("Microsoft YaHei UI", 9), fill="#77838d", tags="row")
            price = quote_amount(row.get("price"), row.get("currency", "USD"))
            if multiple:
                price = "均价 " + average_quote(row["price"], row["currency"])
            has_share = self.recap_for(kind, row)
            if has_share:
                canvas.create_text(right, y + 91, anchor="e", text="分享收益图 ↗", font=("Microsoft YaHei UI", 10, "bold"), fill="#674378", tags="row")
            canvas.create_text(middle, y + 25, anchor="e", text=price, font=("Microsoft YaHei UI", 15, "bold"), fill="#25323b", tags="row")
            stamp = f'{row.get("date", "")} {row.get("time", "")}'.strip()
            canvas.create_text(right, y + 25, anchor="e", text="成交时间 " + stamp, fill="#687780", tags="row")
            if getattr(self, "hide_amounts", False):
                canvas.create_line(16, y + 105, width - 16, y + 105, fill="#e6e9ec", tags="row")
                continue
            canvas.create_text(middle, y + 51, anchor="e", text="成交额 " + amount(row.get("gross"), row.get("currency", "USD")), fill="#525d67", tags="row")
            canvas.create_text(middle, y + 76, anchor="e", text="佣金 " + amount(row.get("commission"), row.get("commission_currency") or row.get("currency", "USD")), fill="#88939b", tags="row")
            pnl = row.get("realized_pnl")
            canvas.create_text(right, y + 47, anchor="e", text=amount(pnl, row.get("pnl_currency") or row.get("currency", "USD"), True), font=("Microsoft YaHei UI", 14, "bold"), fill=pnl_color(pnl, self.green_profit, neutral="#647583"), tags="row")
            canvas.create_text(right, y + 68, anchor="e", text="盈亏待回报" if pnl is None else "已实现盈亏", fill="#88939b", tags="row")
            canvas.create_line(16, y + 105, width - 16, y + 105, fill="#e6e9ec", tags="row")
