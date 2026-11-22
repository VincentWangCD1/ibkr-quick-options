"""Pure, SELL-only option exit specifications and IBKR Order construction.

No connection or submission is performed here. The caller must separately
verify position quantity, existing sell reservations, contract permissions,
session, and broker preflight. Prices/amount offsets are option quote prices,
not underlying index points or dollar cost per multiplied contract.

Official field references:
https://www.interactivebrokers.com/docs/general/order-types/stop
https://www.interactivebrokers.com/docs/general/order-types/stop-limit
https://www.interactivebrokers.com/docs/general/order-types/trailing-stop/tws-api-trailing-stop
https://www.interactivebrokers.com/docs/general/order-types/trailing-stop-limit

TRAIL starts tracking when submitted/activated, not after an assumed profit
threshold. TRAIL may omit an initial stop. TRAIL LIMIT requires an explicit
initial stop here, consistent with IB's TWS guide; it is never silently derived:
https://www.interactivebrokers.co.uk/en/software/tws.bak/usersguidebook/ordertypes/trailing_stop_limit.htm
"""
from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from fractions import Fraction
import math

from core import ValidationError, decimal_value
from order_policy import protected_limit


STOP_TYPES = ("STP", "STP LMT", "TRAIL", "TRAIL LIMIT")
EXIT_TYPES = ("LMT", *STOP_TYPES)
_KEYS = {"type", "limit_price", "stop_price", "trail_mode", "trail_value", "limit_offset", "tif"}
_LABELS = {"limit_price": "卖出限价", "stop_price": "止损触发价", "trail_value": "追踪幅度", "limit_offset": "限价偏移"}
_USED = {
    "LMT": {"limit_price"},
    "STP": {"stop_price"},
    "STP LMT": {"limit_price", "stop_price"},
    "TRAIL": {"stop_price", "trail_mode", "trail_value"},
    "TRAIL LIMIT": {"stop_price", "trail_mode", "trail_value", "limit_offset"},
}


def _empty(value):
    return value is None or (isinstance(value, str) and not value.strip())


def _number(value, label):
    number = decimal_value(value.strip() if isinstance(value, str) else value, label)
    # IB's double fields cannot retain arbitrarily precise user input. Reject,
    # rather than silently round a price or change a near-100 trailing percent.
    rendered = float(number)
    if not math.isfinite(rendered) or Decimal(str(rendered)) != number:
        raise ValidationError(f"{label}精度超出订单接口支持范围，请明确缩短小数位。")
    return number


def _normalized(spec):
    if not isinstance(spec, Mapping):
        raise ValidationError("平仓参数必须为字典。")
    if set(spec) - _KEYS:
        raise ValidationError("平仓参数包含不支持的字段。")
    order_type = " ".join(str(spec.get("type", "")).strip().upper().split())
    if order_type not in EXIT_TYPES:
        raise ValidationError("平仓类型须为 LMT、STP、STP LMT、TRAIL 或 TRAIL LIMIT。")
    tif = "DAY" if _empty(spec.get("tif")) else str(spec["tif"]).strip().upper()
    if tif not in ("DAY", "GTC"):
        raise ValidationError("平仓有效期仅支持 DAY 或 GTC。")
    values = {key: None for key in _KEYS}
    values.update(type=order_type, tif=tif)
    for key in _KEYS - {"type", "tif"}:
        raw = spec.get(key)
        if _empty(raw):
            continue
        if key not in _USED[order_type]:
            raise ValidationError(f"{order_type} 不使用 {key}，请清空无关字段。")
        if key == "trail_mode":
            mode = str(raw).strip().lower()
            if mode not in ("percent", "amount"):
                raise ValidationError("追踪方式须为 percent（百分比）或 amount（报价金额）。")
            values[key] = mode
        else:
            values[key] = _number(raw, _LABELS[key])

    required = _USED[order_type] - ({"stop_price"} if order_type == "TRAIL" else set())
    for key in required:
        if values[key] is None:
            if order_type == "TRAIL LIMIT" and key == "stop_price":
                raise ValidationError("TRAIL LIMIT 必须填写初始止损触发价，不会自动猜测。")
            raise ValidationError(f"{order_type} 必须填写 {_LABELS.get(key, key)}。")
    for key in ("limit_price", "stop_price", "trail_value"):
        if values[key] is not None and values[key] <= 0:
            raise ValidationError(f"{_LABELS[key]}必须大于 0。")
    if values["limit_offset"] is not None and values["limit_offset"] < 0:
        raise ValidationError("限价偏移不能小于 0。")
    if values["trail_mode"] == "percent" and not 0 < values["trail_value"] < 100:
        raise ValidationError("追踪百分比必须大于 0 且小于 100；填 5 表示 5%。")
    if order_type == "STP LMT" and values["limit_price"] > values["stop_price"]:
        raise ValidationError("卖出止损限价必须小于或等于止损触发价。")
    if order_type == "TRAIL LIMIT" and values["limit_offset"] >= values["stop_price"]:
        raise ValidationError("限价偏移必须小于初始止损价，确保初始卖出限价大于 0。")
    return values


def _price_tiers(increments):
    if increments is None or isinstance(increments, (str, bytes, Mapping)):
        raise ValidationError("缺少有效交易所价格阶梯。")
    tiers = {}
    try:
        entries = list(increments)
    except TypeError:
        raise ValidationError("交易所价格阶梯格式无效。") from None
    for item in entries:
        try:
            if isinstance(item, Mapping):
                edge, step = item["lowEdge"], item["increment"]
            elif isinstance(item, (tuple, list)) and len(item) == 2:
                edge, step = item
            else:
                edge, step = item.lowEdge, item.increment
        except (KeyError, AttributeError, TypeError):
            raise ValidationError("交易所价格阶梯须含 lowEdge 和 increment。") from None
        edge, step = _number(edge, "价格阶梯起点"), _number(step, "最小报价单位")
        if edge < 0 or step <= 0 or (edge in tiers and tiers[edge] != step):
            raise ValidationError("交易所价格阶梯无效或冲突。")
        tiers[edge] = step
    if not tiers or min(tiers) != 0:
        raise ValidationError("缺少从零价格起的有效报价单位。")
    return tiers


def _check_price(value, tiers, label):
    exact = Fraction(value)
    edge = max(edge for edge in tiers if Fraction(edge) <= exact)
    step = tiers[edge]
    ticks = (exact - Fraction(edge)) / Fraction(step)
    if ticks.denominator != 1:
        example = protected_limit("BUY", Decimal(exact.numerator) / Decimal(exact.denominator), 0, tiers.items())
        raise ValidationError(f"{label} {value} 不符合该价格档的报价单位 {step}。有效价示例 {example:g}，请修改输入，不会自动改价。")


def is_cost_protection(order_ref, stop, avg_cost, multiplier):
    """Older tools had no marker. Recognize their full-position break-even/profit stops by price."""
    import re
    if str(order_ref).startswith("QOPT1:PS:"):
        return True
    if not re.fullmatch(r"QOPT1:[0-9a-fA-F]{16}", str(order_ref)):
        return False
    try:
        return decimal_value(stop) * decimal_value(multiplier) >= decimal_value(avg_cost) > 0 and decimal_value(multiplier) > 0
    except (ValueError, TypeError, ArithmeticError):
        return False


def cost_multiple(value):
    factor = _number(value, "成本倍数")
    if factor <= 0:
        raise ValidationError("成本倍数必须大于 0，例如 1、1.5 或 2。")
    return factor


def cost_stop_quote(avg_cost, multiplier, quantity, bid, increments, multiple=1):
    factor = cost_multiple(multiple)
    cost = decimal_value(avg_cost, "每张成本") / decimal_value(multiplier, "合约乘数")
    stop = protected_limit("BUY", cost * factor, 0, _price_tiers(increments).items())
    if factor < 1 and decimal_value(stop) >= cost:
        raise ValidationError(f"所选亏损幅度不足一个有效报价档。按报价单位取整后触发价 {stop:g} 已达到成本 {cost:g}，请另选比例。")
    if quantity <= 0 or not bid or bid <= stop:
        raise ValidationError(f"每份成本 {cost:g}，成本止损触发价 {stop:g}，当前买一 {bid:g}。买一尚未高于触发价，未提交止损单。")
    return dict(avg_cost=float(avg_cost), unit_cost=float(cost), multiple=float(factor),
                quantity=float(quantity), bid=float(bid), stop_price=stop)


def check_cost_stop(expected, actual, spec, quantity):
    if (spec.get("type") != "STP" or spec.get("stop_price") != actual["stop_price"]
            or quantity != actual["quantity"] or expected.get("multiple", 1) != actual["multiple"]
            or any(expected.get(k) != actual[k] for k in ("avg_cost", "quantity", "stop_price"))):
        raise ValidationError("持仓成本、数量或触发价已改变，请重新预览成本止损。")


def _check_offset(value, minimum, label):
    if (Fraction(value) / Fraction(minimum)).denominator != 1:
        raise ValidationError(f"{label}必须为最小报价单位 {minimum} 的整数倍，不会自动改价。")


def validate_exit_spec(spec, increments):
    """Return normalized, displayable input without changing user prices.

    increments accepts IB PriceIncrement objects, {lowEdge, increment} mappings,
    or (lowEdge, increment) pairs. Fixed prices use their price tier. Absolute
    trailing/limit offsets use the minimum tick because they are differences,
    not option prices; the broker must still validate dynamic future prices.
    Percent offsets are percentages, not quote-currency tick amounts.
    """
    values = _normalized(spec)
    tiers = _price_tiers(increments)
    minimum = min(tiers.values())
    for key in ("limit_price", "stop_price"):
        if values[key] is not None:
            _check_price(values[key], tiers, _LABELS[key])
    if values["trail_mode"] == "amount":
        _check_offset(values["trail_value"], minimum, _LABELS["trail_value"])
    if values["limit_offset"] is not None:
        _check_offset(values["limit_offset"], minimum, _LABELS["limit_offset"])
    if values["type"] == "TRAIL LIMIT":
        initial_limit = Fraction(values["stop_price"]) - Fraction(values["limit_offset"])
        _check_price(initial_limit, tiers, "初始止损价减去限价偏移后的卖出限价")
    return {key: float(value) if isinstance(value, Decimal) else value for key, value in values.items()}


def build_exit_order(spec, quantity, account, order_ref, outside_rth=False):
    """Construct only; never connect, submit, inspect positions, or monitor.

    The caller must first validate_exit_spec with the routed contract's current
    price increments. Basic constraints are checked again here. Unused IB price
    fields retain UNSET_DOUBLE; in particular TRAIL LIMIT never sets lmtPrice
    together with lmtPriceOffset. Trigger method 0 leaves election to IBKR's
    standard trigger logic; no local price trigger is installed. STP/TRAIL turn
    into market orders, so this version restricts those two to RTH. LMT and the
    two limit-producing conditional types may request outsideRth, subject to
    broker preflight and actual contract/route support. In particular Cboe's
    native stop-limit restrictions do not prove IB-simulated stops are enabled.
    """
    values = _normalized(spec)
    count = decimal_value(quantity, "平仓张数")
    if count <= 0 or count != count.to_integral_value():
        raise ValidationError("平仓张数必须为正整数。")
    if not isinstance(outside_rth, bool):
        raise ValidationError("outside_rth 必须为布尔值。")
    if outside_rth and values["type"] in ("STP", "TRAIL"):
        raise ValidationError("STP/TRAIL 触发后为市价单，只允许常规交易时段；盘外请选支持的限价类型。")
    for value, label in ((account, "账户"), (order_ref, "订单标识")):
        if not isinstance(value, str) or not value.strip() or any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValidationError(f"{label}不能为空或包含控制字符。")

    from ib_async import Order

    order = Order(action="SELL", totalQuantity=int(count), orderType=values["type"],
                  tif=values["tif"], account=account.strip(), orderRef=order_ref.strip(),
                  outsideRth=outside_rth, triggerMethod=0, transmit=True)
    if values["type"] in ("LMT", "STP LMT"):
        order.lmtPrice = float(values["limit_price"])
    if values["type"] in ("STP", "STP LMT"):
        order.auxPrice = float(values["stop_price"])
    if values["type"] in ("TRAIL", "TRAIL LIMIT"):
        if values["stop_price"] is not None:
            order.trailStopPrice = float(values["stop_price"])
        if values["trail_mode"] == "percent":
            order.trailingPercent = float(values["trail_value"])
        else:
            order.auxPrice = float(values["trail_value"])
        if values["type"] == "TRAIL LIMIT":
            order.lmtPriceOffset = float(values["limit_offset"])
    return order
