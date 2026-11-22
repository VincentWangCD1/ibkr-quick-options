"""Pure limit-price construction, with exchange price-tier rounding."""
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

from core import ValidationError, decimal_value


def protected_limit(action, price, slippage_pct, increments):
    """Buy at or above ask, sell at or below bid, on the actual price grid.

    increments is [(lowEdge, increment), ...] from the routed market rule.
    Rounding may move into another tier; validate the final tick in that tier.
    """
    if action not in ("BUY", "SELL"):
        raise ValidationError("限价方向无效")
    price = decimal_value(price, "盘口价")
    slip = decimal_value(slippage_pct, "限价保护百分比")
    if price <= 0 or not 0 <= slip <= 100:
        raise ValidationError("盘口价或限价保护百分比无效")
    tiers = {}
    for edge, step in increments:
        edge, step = decimal_value(edge), decimal_value(step)
        if edge < 0 or step <= 0 or (edge in tiers and tiers[edge] != step):
            raise ValidationError("合约最小报价单位无效或冲突")
        tiers[edge] = step
    if not tiers or min(tiers) != 0:
        raise ValidationError("缺少从零价格起的有效报价单位")
    target = price * (1 + slip / 100 if action == "BUY" else 1 - slip / 100)
    rounding = ROUND_CEILING if action == "BUY" else ROUND_FLOOR
    # A limit of zero is invalid; do not silently change the user's protection.
    if target <= 0:
        raise ValidationError("卖出价格保护不能产生零价格，请降低滑点百分比")
    for _ in range(len(tiers) + 2):
        edge = max(e for e in tiers if e <= target)
        step = tiers[edge]
        value = edge + ((target - edge) / step).to_integral_value(rounding=rounding) * step
        final_edge = max(e for e in tiers if e <= value)
        if final_edge == edge:
            if value <= 0:
                raise ValidationError("卖出限价低于该合约有效最小价格")
            return float(value)
        target = value
    raise ValidationError("无法确认最终限价的报价单位")
