"""Readable labels only; broker identities and order inputs are untouched."""
from datetime import date, datetime
from zoneinfo import ZoneInfo
import math
import re


def option_parts(contract):
    raw = str(contract.get("local_symbol") or "").strip()
    parsed = re.fullmatch(r"(.*?)\s*(\d{6})([CP])(\d{8})", raw)
    fallback = {}
    if parsed:
        root, day, right, strike = parsed.groups()
        fallback = dict(symbol=root.strip(), expiry="20" + day, right=right, strike=int(strike) / 1000)
    symbol = str(contract.get("trading_class") or fallback.get("symbol") or contract.get("symbol") or "").strip()
    expiry = str(contract.get("expiry") or fallback.get("expiry") or "")
    try:
        expiry = datetime.strptime(expiry[:8], "%Y%m%d").strftime("%Y-%m-%d")
    except ValueError:
        pass
    right = str(contract.get("right") or fallback.get("right") or "").upper()
    right = {"C": "Call", "CALL": "Call", "P": "Put", "PUT": "Put"}.get(right, "")
    strike = contract.get("strike")
    if strike is None:
        strike = fallback.get("strike")
    try:
        strike = f"{float(strike):g}" if math.isfinite(float(strike)) and float(strike) > 0 else ""
    except (TypeError, ValueError):
        strike = ""
    return symbol, expiry, strike, right


def option_label(contract, multiline=False):
    symbol, expiry, strike, right = option_parts(contract)
    if not (expiry and strike and right):
        return str(contract.get("local_symbol") or contract.get("symbol") or "—")
    first = " · ".join(value for value in (symbol, expiry) if value)
    return first + ("\n" if multiline else " · ") + f"{strike} {right}"


def pnl_color(value, green_profit=False, *, red="#be3045", green="#008653", neutral="#334c5d"):
    try:
        value = float(value)
        if math.isfinite(value):
            if value != 0:
                return green if (value > 0) == green_profit else red
    except (ValueError, TypeError):
        pass
    return neutral


def compact_exit(position, category):
    orders = [o for o in position.get("active_exit_orders", []) if o.get("category") == category
              or (category == "take_profit" and o.get("category") == "close" and o.get("order_type") == "LMT")]
    if not orders:
        full = position.get("stop_loss_summary" if category == "stop_loss" else "take_profit_summary", "")
        return "未发现挂单" if not full or full.startswith(("未发现", "未设")) else full
    order = orders[0]
    kind, price = order.get("order_type", ""), order.get("target_price")
    line = kind + (f" @{price:g}" if price is not None and price > 0 else "")
    if kind.startswith("TRAIL") and order.get("trail_value"):
        line = "移动 " + f'{order["trail_value"]:g}' + ("%" if order.get("trail_mode") == "percent" else "价差")
    second = f'{order.get("remaining", 0):g}张 · {order.get("status_label", "待确认")}'
    if kind in ("STP LMT", "TRAIL LIMIT") and order.get("limit_price"):
        second = f'限 {order["limit_price"]:g} · {order.get("status_label", "待确认")}'
    if len(orders) > 1:
        second = f"共 {len(orders)} 笔 · 见下方详情"
    return line + "\n" + second


def order_display(order, today=None):
    _, expiry, _, _ = option_parts(order)
    today = today or datetime.now(ZoneInfo("America/New_York")).date()
    try:
        days = (date.fromisoformat(expiry) - today).days
        dte = f"{days} DTE" if days >= 0 else f"已到期 {abs(days)} 天"
    except ValueError:
        dte = "—"
    stamp = order.get("placed_at") or order.get("observed_at")
    clock = datetime.fromtimestamp(stamp, ZoneInfo("Asia/Shanghai")).strftime("%m-%d %H:%M:%S") if stamp else "—"
    if stamp and not order.get("placed_at"):
        clock = "首报 " + clock
    raw = order.get("status", "").split(" (", 1)[0]
    status = {"PendingSubmit": "待提交", "ApiPending": "待提交", "PreSubmitted": "已预提交",
              "Submitted": "已挂单", "Filled": "已成交", "PendingCancel": "撤单中",
              "Cancelled": "已撤单", "ApiCancelled": "已撤单", "Inactive": "未激活"}.get(raw, raw)
    active = raw not in ("Filled", "Cancelled", "ApiCancelled", "Inactive")
    if active and order.get("filled", 0) > 0 and order.get("remaining", 0) > 0:
        status = "部分成交 · " + status
    quote = order.get("quote") or {}
    def price(side):
        value = quote.get(side)
        if value is None or not (0 < value < 1e100):
            return "—"
        age = quote.get(side + "_age")
        return f"{value:g}" + ("*" if age is None or age > 10 else "")
    book = price("bid") + " / " + price("ask")
    mid = "—"
    if (active and not quote.get("halted") and quote.get("quote_type") in ("实时订阅", "合成演示")
            and all(price(side) != "—" and not price(side).endswith("*") for side in ("bid", "ask"))
            and quote["bid"] <= quote["ask"]):
        from decimal import Decimal
        mid = format((Decimal(str(quote["bid"])) + Decimal(str(quote["ask"]))) / 2, "f")
    source = "停牌" if quote.get("halted") else quote.get("quote_type", "待行情")
    return dict(contract=option_label(order), dte=dte, time=clock, status=status, active=active,
                side={"BUY": "买入", "SELL": "卖出"}.get(order.get("action"), "—"),
                book=book, midpoint=mid, last=price("last"), source=source if active else "—")
