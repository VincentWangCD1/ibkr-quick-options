"""Pure MKT/LMT/fixed-midpoint entry and amendment pricing; never submits orders."""
from collections.abc import Mapping
from decimal import Decimal

from core import ValidationError, decimal_value
from exit_orders import _check_price, _number, _price_tiers
from order_policy import protected_limit


ENTRY_MODES = ("MKT", "LMT", "MID")


def limit_contract_changed(settings, right, conid):
    """A bound price must not silently migrate to a different matched option."""
    bindings = settings.get("limit_conids")
    bound = bindings.get(right) if isinstance(bindings, Mapping) else None
    return (settings.get("order_mode", "MKT") == "LMT" and conid is not None
            and bound is not None and str(bound) != str(conid))


def resolve_entry_price(settings, right, conid=None):
    """Resolve per-side prices without allowing an absent opposite-side map value
    to accidentally reuse the first side's limit. Blank LMT requests one seed.
    """
    if right not in ("C", "P"):
        raise ValidationError("方向必须为 C 或 P。")
    mode = settings.get("order_mode", "MKT")
    if mode not in ENTRY_MODES:
        raise ValidationError("下单方式须为 MKT、LMT 或 MID。")
    prices = settings.get("limit_prices")
    price = prices.get(right) if isinstance(prices, Mapping) else settings.get("limit_price")
    if limit_contract_changed(settings, right, conid):
        return mode, None, True
    empty = price is None or (isinstance(price, str) and not price.strip())
    return mode, (None if empty else price), mode == "LMT" and empty


def build_entry_plan(action, order_mode, quote, increments, session_kind="regular",
                     limit_price=None, slippage_pct=5, auto_limit=False):
    """Build a displayable order plan using the routed contract's tick schedule.

    MID is one fixed bid/ask midpoint limit, BUY rounded down and SELL up.
    Explicit LMT is validated exactly, never silently changed. Extended-session
    MKT retains the app's protected LMT behavior. Quote freshness and session
    permission checks belong to the broker; this helper checks finite prices.
    """
    if action not in ("BUY", "SELL"):
        raise ValidationError("订单方向必须为 BUY 或 SELL。")
    if order_mode not in ENTRY_MODES:
        raise ValidationError("下单方式须为 MKT、LMT 或 MID。")
    if session_kind not in ("regular", "extended"):
        raise ValidationError("当前不是可下单交易时段。")
    if not isinstance(quote, Mapping):
        raise ValidationError("盘口报价格式无效。")
    slip = decimal_value(slippage_pct, "滑点预留")
    if not 0 <= slip <= 100:
        raise ValidationError("滑点预留必须在 0 到 100% 之间。")
    outside = session_kind == "extended"
    result = dict(order_type="MKT", limit_price=None, price_mode=order_mode,
                  outside_rth=outside, sizing_price=None, reserve_slippage_pct=float(slip))
    if order_mode == "LMT" and auto_limit and limit_price is None:
        seeded = build_entry_plan(action, "MID", quote, increments, session_kind,
                                  slippage_pct=slippage_pct)
        seeded.update(price_mode="LMT", limit_seeded=True)
        return seeded
    if order_mode == "LMT":
        value = _number(limit_price, "限价")
        if value <= 0:
            raise ValidationError("限价必须大于 0。")
        _check_price(value, _price_tiers(increments), "限价")
        result.update(order_type="LMT", limit_price=float(value))
    elif order_mode == "MID":
        bid, ask = _number(quote.get("bid"), "买一价"), _number(quote.get("ask"), "卖一价")
        if bid < 0 or ask <= 0 or bid > ask:
            raise ValidationError("MID 需要有效且未倒挂的买卖盘口。")
        tiers = _price_tiers(increments)
        midpoint = (bid + ask) / Decimal(2)
        # The opposite protected-limit direction produces a conservative grid:
        # BUY may pay no more than midpoint; SELL receives no less than midpoint.
        rounded = protected_limit("SELL" if action == "BUY" else "BUY",
                                  midpoint, 0, list(tiers.items()))
        result.update(order_type="LMT", limit_price=rounded, midpoint=float(midpoint))
    else:
        price = _number(quote.get("ask" if action == "BUY" else "bid"), "盘口价")
        if price <= 0:
            raise ValidationError("市价单需要有效的正数盘口价。")
        if outside:
            tiers = _price_tiers(increments)
            result.update(order_type="LMT", limit_price=protected_limit(action, price, slip, list(tiers.items())))
        else:
            result["sizing_price"] = float(price)
    if result["order_type"] == "LMT":
        result.update(sizing_price=result["limit_price"], reserve_slippage_pct=0.0)
    return result
