"""Pure display rules shared by the desktop UI and headless tests.

No broker calls or GUI imports belong here. In particular, a disconnected
snapshot must never keep a previous session's balances on screen.
"""
from __future__ import annotations

from collections.abc import Mapping
import math


UNKNOWN = "—"


def default_depth(moneyness: str) -> int:
    """Default *on a category change*, preserving later manual depth edits."""
    if moneyness == "OTM":
        return 4
    if moneyness in ("ITM", "ATM"):
        return 1
    raise ValueError("虚实值必须为 OTM、ITM 或 ATM。")


def _finite_number(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def money_text(value, currency: str = "USD") -> str:
    """Format an observed amount; unknown/non-finite values are never $0."""
    amount = _finite_number(value)
    if amount is None:
        return UNKNOWN
    code = str(currency or "").strip().upper()
    if not code or code == "BASE":
        # BASE is an aggregate label, not a concrete monetary denomination.
        return f"{amount:,.2f}（币种待确认）"
    prefix = "$" if code == "USD" else f"{code} "
    return f"{prefix}{amount:,.2f}"


def last_session_label(report):
    title = "最近交易日已实现盈亏"
    day = report.get("date")
    if not day:
        return title + "　—"
    amounts = []
    for currency, value in sorted(report.get("by_currency", {}).items()):
        amount = _finite_number(value)
        amounts.append(f"{amount:+,.2f} {currency}" if amount is not None else f"— {currency}")
    subtotal = " / ".join(amounts) if amounts else "—"
    if report.get("missing_pnl_count"):
        subtotal += "（已知小计）"
    if not report.get("fills"):
        subtotal += "（暂无成交记录）"
    return f"{title}　{day}　{subtotal}"


def account_day_label(financial):
    pnl = _finite_number(financial.get("daily_pnl"))
    currency = financial.get("base_currency")
    if pnl is None or not currency or abs(pnl) > 1e100:
        return "账户当日盈亏　—"
    equity = _finite_number(financial.get("net_liquidation"))
    result = f"账户当日盈亏　{pnl:+,.2f} {currency}"
    if (equity is not None and financial.get("net_liquidation_currency") == currency
            and 0 < equity - pnl < 1e100):
        result += f"　{pnl / (equity - pnl):+.2%}（按期初净值估算）"
    return result


def build_fund_cards(snapshot: Mapping) -> dict[str, str]:
    """Return the six strings used by the always-visible account header.

    Only the current snapshot's ``financial`` object supplies amounts. There is
    intentionally no fallback to old top-level ``cash`` or a previous preview.
    """
    mode = str(snapshot.get("mode", "")).upper()
    labels = {"DEMO": "离线演示 · 演示虚构", "PAPER": "IBKR 模拟盘", "LIVE": "IBKR 实盘"}
    mode_label = labels.get(mode, "账户模式待确认")
    if not snapshot.get("connected", False):
        return {
            "equity": UNKNOWN,
            "usd_cash": UNKNOWN,
            "settled_cash": UNKNOWN,
            "order_cash": UNKNOWN,
            "detail": "未连接账户，资金和行情显示已清空。",
            "connection": f"{mode_label} · 未连接",
        }

    data = snapshot.get("financial")
    data = data if isinstance(data, Mapping) else {}
    account = str(snapshot.get("account") or "待确认")
    cash = _finite_number(data.get("cash"))
    ready = (data.get("ready") is True and mode in labels
             and cash is not None and cash >= 0)
    demo = mode == "DEMO"
    demo_suffix = "（演示虚构）" if demo else ""

    def card(value, currency="USD"):
        result = money_text(value, currency)
        return result + demo_suffix if result != UNKNOWN else result

    equity_currency = data.get("net_liquidation_currency") or data.get("base_currency") or ""
    result = {
        "equity": card(data.get("net_liquidation"), equity_currency),
        "usd_cash": card(data.get("usd_cash")),
        "settled_cash": card(data.get("settled_cash")),
        "order_cash": card(cash) if ready else "暂停开仓",
        "connection": f"{mode_label} · 已连接 · 账户 {account}",
        "detail": "",
    }
    if ready:
        result["detail"] = "按美元现金、已结算资金与可用资金的较小值计算，再扣除本工具预留。"
    else:
        result["detail"] = str(data.get("error") or "资金尚未完整核验；可查看已取得的数据，暂不能开仓。")
    if ready and not demo and data.get("cash_before_local_hold") is not None:
        raw = data["cash_before_local_hold"]
        limits = [("美元现金", data.get("usd_cash")), ("可用资金", data.get("available_funds")),
                  ("结算上限", data.get("settlement_cash_cap")), ("结算账本", data.get("ledger_settled_cash"))]
        limiting = "、".join(name for name, value in limits if value is not None and abs(raw - value) < .01)
        result["detail"] = f"本工具开仓上限 {money_text(raw)}（{limiting or '取各项较低值'}）"
        usd_cash = _finite_number(data.get("usd_cash"))
        if usd_cash is not None and usd_cash - raw >= .01:
            result["detail"] += f"  ·  美元现金高出该上限 {money_text(usd_cash - raw)}"
        if snapshot.get("cash_reserve"):
            result["detail"] += f"  ·  买单扣款待核对，本地实际占用 {money_text(max(0, raw - cash))}"
        else:
            result["detail"] += "  ·  本地买单预留 $0.00"
    if not demo:
        type_label = data.get("account_type_label")
        if type_label:
            result["detail"] += "  |  " + str(type_label)
        settled_date = data.get("settled_as_of")
        if settled_date:
            result["detail"] += "  |  结算明细日期 " + str(settled_date)
    equity = _finite_number(data.get("net_liquidation"))
    total_cash = _finite_number(data.get("total_cash_value"))
    if equity is not None and total_cash is not None and equity_currency and equity_currency != "BASE":
        holdings = money_text(equity - total_cash, equity_currency)
        composition = f"总资产含现金 {money_text(total_cash, equity_currency)}、持仓净值 {holdings}"
        result["detail"] += "  |  " + composition
    if demo:
        result["detail"] = "演示虚构资金，仅用于本地体验。" + result["detail"]
    return result


def empty_chain_message(connected: bool, symbol: str = "", expiry: str = "") -> str:
    if not connected:
        return "未连接，期权链已清空。"
    if not str(symbol).strip():
        return "选择或输入标的代码后加载期权链。"
    if not str(expiry).strip():
        return "选择到期日后加载期权链。"
    return "等待期权链数据；无行情的价格将显示为 —。"


def chain_rows_with_reference(rows, reference):
    """Place a visible reference divider among ascending, real strikes.

    No reference means no invented center or moneyness. The divider precedes
    the first strike >= reference; a strike exactly at reference is ATM on
    both sides. Each contract keeps a stable strike identity across updates.
    """
    price = _finite_number(reference)
    if price is None or price <= 0:
        return []
    valid = {}
    for row in rows:
        strike = _finite_number(row.get("strike"))
        if strike is not None and strike > 0:
            valid[strike] = row
    if not valid:
        return []
    divider = {"iid": "reference-price", "kind": "reference", "strike": price}
    display = []
    placed = False
    for strike in sorted(valid):
        if not placed and strike >= price:
            display.append(divider)
            placed = True
        call = "实值" if strike < price else "虚值" if strike > price else "平值"
        put = "虚值" if strike < price else "实值" if strike > price else "平值"
        display.append({"iid": f"strike-{strike:.12g}", "kind": "contract", "strike": strike,
                        "call_zone": call, "put_zone": put, "row": valid[strike]})
    if not placed:
        display.append(divider)
    return display


def quote_age_text(quote):
    """Subscription state and independent bid/ask update ages, not expiry."""
    def age(value):
        number = _finite_number(value)
        return f"{number:.0f}s" if number is not None and number >= 0 else UNKNOWN
    return f'{quote.get("quote_type") or "无报价"} · 买 {age(quote.get("bid_age"))} / 卖 {age(quote.get("ask_age"))}'


def centered_chain_fraction(reference_index, row_count, visible_rows):
    """Treeview yview fraction that centers the divider when there is room."""
    if row_count <= 0:
        return 0.0
    visible_rows = max(1, int(visible_rows))
    first = max(0, min(int(reference_index) - visible_rows // 2, row_count - visible_rows))
    return first / row_count


CLOSE_ORDER_FIELDS = {
    "LMT": ("limit_price",),
    "STP": ("stop_price",),
    "STP LMT": ("stop_price", "limit_price"),
    "TRAIL": ("trail_mode", "trail_value"),
    "TRAIL LIMIT": ("stop_price", "trail_mode", "trail_value", "limit_offset"),
}


def close_order_fields(order_type):
    if order_type not in CLOSE_ORDER_FIELDS:
        raise ValueError("请选择支持的卖单类型。")
    return CLOSE_ORDER_FIELDS[order_type]


def build_close_order_spec(order_type, values):
    """Validate only visible fields; stale values from another type cannot leak."""
    fields = close_order_fields(order_type)
    tif = str(values.get("tif", "DAY")).strip().upper()
    if tif not in ("DAY", "GTC"):
        raise ValueError("有效期必须为 DAY 或 GTC。")
    spec = {"type": order_type, "tif": tif}
    labels = {"limit_price": "限价", "stop_price": "止损触发价", "trail_value": "移动幅度", "limit_offset": "移动限价偏移"}
    for field in fields:
        if field == "trail_mode":
            mode = values.get(field)
            if mode not in ("percent", "amount"):
                raise ValueError("移动单位必须为百分比或价差。")
            spec[field] = mode
        else:
            value = _finite_number(values.get(field))
            if value is None or value <= 0 or value >= 1e12:
                raise ValueError(labels[field] + "必须为有效正数。")
            spec[field] = value
    if spec.get("trail_mode") == "percent" and spec["trail_value"] >= 100:
        raise ValueError("移动百分比必须大于 0 且小于 100。")
    if order_type == "STP LMT" and spec["limit_price"] > spec["stop_price"]:
        raise ValueError("卖出止损限价单的限价不能高于止损触发价。")
    return spec


def order_number_text(value):
    number = _finite_number(value)
    if number is None or number < 0 or number >= 1e12:
        return UNKNOWN
    return f"{number:.6f}".rstrip("0").rstrip(".")


def build_entry_fields(order_mode, limit_price=None, rounding_mode="nearest"):
    """Hidden limit input must never affect market/midpoint orders."""
    if order_mode not in ("MKT", "LMT", "MID"):
        raise ValueError("请选择市价、限价或中间价。")
    if rounding_mode not in ("nearest", "floor"):
        raise ValueError("请选择有效的张数取整方式。")
    price = None
    if order_mode == "LMT":
        price = _finite_number(limit_price)
        if price is None or price <= 0 or price >= 1e12:
            raise ValueError("限价必须为有效正数，单位为美元/份。")
    return dict(order_mode=order_mode, limit_price=price, rounding_mode=rounding_mode)


def modify_order_confirmation(preview):
    new_type = preview["order_type"]
    new_price = "市价（成交价不保证）" if new_type == "MKT" else order_number_text(preview.get("limit_price"))
    old_price = order_number_text(preview.get("old_limit_price"))
    lines = [f'订单：{preview["order_id"]} · {preview["local_symbol"]} · {preview["action"]}',
             f'原委托：{preview["old_order_type"]} {old_price}',
             f'新委托：{new_type} {new_price}',
             f'已成交：{preview.get("filled", 0):g} 张；当前剩余：{preview["remaining"]:g} 张']
    if preview.get("price_mode") == "MID":
        lines.append("中间价已按最小价格单位取整为上述固定限价，不自动追价。")
    if preview.get("outside_rth"):
        lines.append("已请求合约支持的延长时段；以 IBKR 接受为准。")
    if preview.get("action") == "BUY":
        fee = order_number_text(preview.get("commission_per_contract", 1))
        minimum = order_number_text(preview.get("min_commission", 1))
        lines.append(f"手续费预留：${fee}/张，最低 ${minimum}；实际预检费用更高时按较高值核验。")
        if new_type == "MKT":
            lines.append("市价滑点预留：" + order_number_text(preview.get("slippage_pct", 5)) + "%（不是成交价格上限）。")
        if preview.get("reserve_settings_source") == "defaults":
            lines.append("未保留原单预留参数，按以上默认参数核验。")
    if preview.get("method") == "replace":
        lines.append("切换类型：先撤原单，收到撤单确认后，按届时剩余数量提交新单。")
        lines.append("撤单期间仍可能成交；新单若被拒绝，原单不会恢复。")
    else:
        lines.append("修改原订单限价，保留原订单号及总委托数量。")
    lines.append("预览 10 秒内有效；提交前再次检查订单状态和资金/持仓。")
    return "\n".join(lines)


def order_parameters_text(order):
    """Use readable prices/percentages, suppressing IB's unset sentinels."""
    parts = []
    stop = order_number_text(order.get("stop_price"))
    if stop != UNKNOWN:
        parts.append(("初始止损 " if order.get("type") == "TRAIL LIMIT" else "触发 ") + stop)
    mode, value = order.get("trail_mode"), order.get("trail_value")
    if value is None and order.get("trailing_percent") is not None:
        mode, value = "percent", order["trailing_percent"]
    if value is None and order.get("trail_amount") is not None:
        mode, value = "amount", order["trail_amount"]
    trail = order_number_text(value)
    if trail != UNKNOWN:
        parts.append("移动 " + trail + ("%" if mode == "percent" else " 美元价差"))
    offset = order_number_text(order.get("limit_offset"))
    if offset != UNKNOWN:
        parts.append("限价偏移 " + offset)
    if order.get("outside_rth"):
        parts.append("已请求延长时段")
    if order.get("execution_warning"):
        parts.append(str(order["execution_warning"]))
    return "；".join(parts) or UNKNOWN


def close_order_confirmation(position, quantity, percent, spec, *, amend_stop=False):
    kind = spec["type"]
    lines = [f'合约：{position["local_symbol"]}',
             f'卖出平仓：{percent:g}% · 按当前持仓预计 {quantity} 张',
             f'类型：{kind}    有效期：{spec["tif"]}']
    if "limit_price" in spec:
        lines.append("限价：" + order_number_text(spec["limit_price"]))
    parameters = order_parameters_text(spec)
    if parameters != UNKNOWN:
        lines.append(parameters)
    lines.append("价格单位：期权报价（美元/份，标准合约 ×100）。")
    if kind in ("TRAIL", "TRAIL LIMIT"):
        lines.append("移动从提交后开始跟随，不是到盈利价才启动。")
    if kind in ("STP", "TRAIL"):
        lines.append("仅常规时段触发，触发后为市价单；成交价不保证。")
    elif kind in ("STP LMT", "TRAIL LIMIT"):
        lines.append("允许合约支持的常规时段外触发/成交，须 IBKR 支持；限价单可能不成交。")
    else:
        lines.append("允许合约支持的常规时段外成交，须 IBKR 支持；按指定限价挂单，可能不会立即成交，不自动追价。")
    if not amend_stop:
        lines.append("提交前将按最新持仓核验；已有挂单须先撤销并收到取消回报。")
    return "\n".join(lines)
