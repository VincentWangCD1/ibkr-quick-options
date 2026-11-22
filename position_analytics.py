"""Display-only position valuation and working-order descriptions.

No value produced here authorizes a trade. Quote references, pending cancels and
stop triggers are deliberately distinguished from executable/live evidence.
"""
import math


def number(value):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return value if math.isfinite(value) and abs(value) < 1e100 else None


def _fresh(age, ttl):
    age = number(age)
    return age is not None and 0 <= age <= ttl


def position_mark(quote=None, portfolio=None, ttl=10):
    quote, portfolio = quote or {}, portfolio or {}
    bid, ask, last = (number(quote.get(key)) for key in ("bid", "ask", "last"))
    synthetic = quote.get("synthetic") is True or "合成" in str(quote.get("quote_type", ""))
    live = quote.get("market_data_type") == 1 and not quote.get("halted")
    book = bid is not None and ask is not None and 0 <= bid <= ask and ask > 0
    if (live or synthetic) and book and _fresh(quote.get("bid_age"), ttl) and _fresh(quote.get("ask_age"), ttl):
        return (bid + ask) / 2, ("离线合成中间价" if synthetic else "IBKR 实时买卖中间价"), ("合成演示" if synthetic else "实时盘口"), True
    if (live or synthetic) and last is not None and last > 0 and _fresh(quote.get("last_age"), ttl):
        return last, ("离线合成最新价" if synthetic else "IBKR 实时最新成交价"), ("合成演示" if synthetic else "实时成交价，非保证成交价"), True
    portfolio_price = number(portfolio.get("market_price"))
    if portfolio_price is not None and portfolio_price >= 0:
        return portfolio_price, "IBKR 投资组合估值", "组合估值参考，非实时盘口", False
    reference = (bid + ask) / 2 if book else last if last is not None and last > 0 else None
    if reference is not None:
        kind = {2: "冻结", 3: "延迟", 4: "延迟冻结"}.get(quote.get("market_data_type"), "陈旧/未确认")
        if quote.get("halted"):
            kind = "停牌"
        return reference, str(quote.get("source") or quote.get("quote_type") or "行情参考"), kind + "行情参考，非实时盘口", False
    return None, "无有效报价", "等待报价；盈亏暂不可计算", False


def _order_status(status):
    base = str(status or "UNKNOWN").split(" (")[0]
    if base in ("Submitted", "PreSubmitted"):
        return "working", "已挂单" if base == "Submitted" else "已预提交/待触发"
    if base == "PendingCancel":
        return "pending_cancel", "撤单中，尚未确认撤销"
    if base in ("PendingSubmit", "ApiPending"):
        return "pending_submit", "待确认，尚未确认生效"
    if base == "Inactive":
        return "inactive", "未生效"
    return "unconfirmed", "状态未确认"


def position_exit_orders(position, orders=()):
    quantity = number(position.get("quantity")) or 0
    multiplier = number(position.get("multiplier")) or 100
    cost = number(position.get("avg_cost"))
    action = "SELL" if quantity > 0 else "BUY"
    found = []
    for raw in orders:
        status = str(raw.get("status", "")).split(" (")[0]
        if (str(raw.get("conid")) != str(position.get("conid")) or raw.get("action") != action
                or raw.get("opens_short") or status in ("Filled", "Cancelled", "ApiCancelled", "Inactive")):
            continue
        remaining = number(raw.get("remaining"))
        if remaining is None or remaining <= 0:
            continue
        kind = str(raw.get("order_type") or "")
        stop, limit = number(raw.get("stop_price")), number(raw.get("limit_price"))
        is_stop = kind in ("STP", "STP LMT", "TRAIL", "TRAIL LIMIT")
        target = stop if is_stop else limit if kind == "LMT" else None
        signed = min(remaining, abs(quantity)) * (1 if quantity > 0 else -1)
        projected = ((target * multiplier - abs(cost)) * signed
                     if target is not None and target > 0 and cost is not None else None)
        category = "stop_loss" if is_stop else "take_profit" if projected is not None and projected > 0 else "close"
        state, label = _order_status(status)
        record = dict(raw, remaining=remaining, category=category, working=state == "working", state=state,
                      status_label=label, target_price=target, projected_pnl=projected,
                      covered_quantity=min(remaining, abs(quantity)),
                      exceeds_position=remaining > abs(quantity),
                      projection_note="按触发价估算，实际成交及平仓费用另计" if is_stop else "按限价估算，平仓费用另计")
        found.append(record)
    return found


def _exit_summary(orders, category):
    relevant = [order for order in orders if order["category"] == category
                or (category == "take_profit" and order["category"] == "close" and order.get("order_type") == "LMT")]
    if not relevant:
        return "未发现止损单" if category == "stop_loss" else "未发现限价平仓单"
    parts = []
    for order in relevant:
        price, pnl = order["target_price"], order["projected_pnl"]
        kind = order.get("order_type", "")
        prefix = "限价平仓（非止盈）" if order["category"] == "close" else kind
        text = f'{prefix} {order["remaining"]:g}张'
        if price is not None and price > 0:
            text += f' · 当前触发 {price:g}' if kind.startswith("TRAIL") else f' @{price:g}'
        if kind in ("TRAIL", "TRAIL LIMIT"):
            trail = number(order.get("trail_value"))
            if trail is not None and trail > 0:
                text += f' · 移动 {trail:g}%' if order.get("trail_mode") == "percent" else f' · 移动价差 ${trail:g}/份'
            else:
                text += " · 移动幅度待回报"
            if price is None or price <= 0:
                text += " · 当前触发价待回报"
        if kind == "TRAIL LIMIT":
            offset = number(order.get("limit_offset"))
            text += f' · 限价偏移 ${offset:g}/份' if offset is not None and offset >= 0 else " · 限价偏移待回报"
        elif kind == "STP LMT":
            limit = number(order.get("limit_price"))
            if limit is not None and limit > 0:
                text += f' · 限价 {limit:g}'
        if pnl is not None:
            text += f' · 预计盈亏 ${pnl:+,.2f}'
        text += " · " + order["status_label"]
        if order.get("oca_group"):
            text += " · OCA " + str(order["oca_group"])
        parts.append(text)
    return "；".join(parts)


def enrich_position(position, quote=None, portfolio=None, orders=()):
    result = dict(position)
    quote = quote or {}
    quantity, multiplier, average = (number(position.get(key)) for key in ("quantity", "multiplier", "avg_cost"))
    multiplier = multiplier if multiplier is not None and multiplier > 0 else None
    average = abs(average) if average is not None else None
    mark, source, status, fresh = position_mark(quote, portfolio)
    entry_cost = abs(quantity) * average if quantity is not None and average is not None else None
    pnl = (quantity * (mark * multiplier - average)
           if None not in (quantity, mark, multiplier, average) else None)
    exits = position_exit_orders(position, orders)
    result.update(avg_option_price=average / multiplier if average is not None and multiplier else None,
                  entry_cost=entry_cost, current_price=mark,
                  market_value=quantity * mark * multiplier if None not in (quantity, mark, multiplier) else None,
                  unrealized_pnl=pnl, unrealized_pnl_pct=100 * pnl / entry_cost if pnl is not None and entry_cost else None,
                  bid=number(quote.get("bid")), ask=number(quote.get("ask")), last=number(quote.get("last")),
                  price_source=source, quote_status=status, price_fresh=fresh,
                  active_exit_orders=exits,
                  stop_loss_summary=_exit_summary(exits, "stop_loss"),
                  take_profit_summary=_exit_summary(exits, "take_profit"))
    return result
