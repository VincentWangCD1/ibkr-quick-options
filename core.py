"""Pure validation, strike selection and cash sizing. No broker or GUI imports."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_FLOOR, ROUND_HALF_UP
import re


SUPPORTED_SYMBOLS = ("SPX", "SPY", "XSP", "QQQ")
PREVIEW_TTL_SECONDS = 10


class ValidationError(ValueError):
    """A user input or broker value is unsuitable for an order."""


def normalize_symbol(value):
    symbol = str(value).strip().upper()
    if not re.fullmatch(r"[A-Z][A-Z0-9 .-]{0,14}", symbol):
        raise ValidationError("请输入有效标的代码，例如 SPX、XSP、SPY、QQQ 或 AAPL。")
    return symbol


def decimal_value(value, name: str = "数值") -> Decimal:
    """Accept finite numeric inputs, never silently treating NaN as zero."""
    if isinstance(value, bool):
        raise ValidationError(f"{name}不能是布尔值。")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise ValidationError(f"{name}不是有效数字。") from None
    if not result.is_finite():
        raise ValidationError(f"{name}必须为有限数字。")
    if abs(result) > Decimal("1e15"):
        raise ValidationError(f"{name}超出支持范围。")
    return result


def _whole_positive(value, name: str) -> int:
    number = decimal_value(value, name)
    if number <= 0 or number != number.to_integral_value():
        raise ValidationError(f"{name}必须为正整数。")
    return int(number)


def _percent(value, name: str = "比例") -> Decimal:
    number = decimal_value(value, name)
    if not Decimal(0) < number <= Decimal(100):
        raise ValidationError(f"{name}必须大于 0 且不超过 100。")
    return number


def select_strike(strikes, spot, right, moneyness, depth=1) -> float:
    """Choose an actually available strike from a validated expiry/right chain.

    ATM: nearest strike; equal distances resolve to the lower strike.
    OTM/ITM: strict comparison to spot, with depth=1 meaning nearest eligible.
    A strike exactly at spot belongs to neither OTM nor ITM.
    """
    reference = decimal_value(spot, "标的价格")
    if reference <= 0:
        raise ValidationError("标的价格必须大于 0。")
    if right not in ("C", "P"):
        raise ValidationError("方向必须为 C 或 P。")
    if moneyness not in ("ATM", "OTM", "ITM"):
        raise ValidationError("档位类型必须为 ATM、OTM 或 ITM。")
    step = _whole_positive(depth, "档数")
    available = sorted({decimal_value(strike, "行权价") for strike in strikes})
    if not available or any(strike <= 0 for strike in available):
        raise ValidationError("缺少有效的正数行权价。")
    if moneyness == "ATM":
        return float(min(available, key=lambda strike: (abs(strike - reference), strike)))
    higher = (right == "C") == (moneyness == "OTM")
    eligible = [strike for strike in available
                if (strike > reference if higher else strike < reference)]
    eligible.sort(reverse=not higher)
    if len(eligible) < step:
        raise ValidationError(f"当前到期日没有足够行权价，无法选择 {moneyness} 第 {step} 档。")
    return float(eligible[step - 1])


def budget_strikes(strikes, spot, right, settings):
    if settings.get("open_action", "BUY") != "BUY":
        raise ValidationError("按预算找档仅适用于买入开仓。")
    start = int(settings.get("depth", 1)) if settings.get("moneyness") == "OTM" else 1
    eligible = sorted({s for s in strikes if (s > spot if right == "C" else s < spot)}, reverse=right == "P")
    return list(enumerate(eligible, 1))[max(0, start - 1):]


def affordable_size(cash, ask, multiplier, settings, limit=None):
    price = max(decimal_value(ask), decimal_value(limit or 0))
    return size_order(cash, settings.get("percent", 50), price, multiplier,
                      settings.get("slippage_pct", 5), settings.get("commission_per_contract", 1),
                      settings.get("min_commission", 1), settings.get("max_contracts", 10000),
                      "floor", settings.get("budget_mode", "percent"), settings.get("cash_amount"))


def size_order(cash, percent, ask, multiplier, slippage_pct=5,
               commission_per_contract=1, min_commission=1,
               max_contracts=10000, rounding_mode="nearest",
               budget_mode="percent", cash_amount=None) -> dict:
    """Round the desired premium quantity, then enforce total available cash.

    nearest uses decimal ROUND_HALF_UP on budget/(price*multiplier). Its premium
    and buffered cost may exceed the selected percentage, but never total cash.
    floor retains the previous largest size fitting the selected buffered budget.
    Estimates do not cap a market order's actual execution price or actual fees.
    """
    available = decimal_value(cash, "可用现金")
    if budget_mode not in ("percent", "amount"):
        raise ValidationError("预算方式必须为 percent 或 amount。")
    ratio = _percent(percent, "现金比例") if budget_mode == "percent" else None
    price = decimal_value(ask, "卖一价")
    contract_multiplier = _whole_positive(multiplier, "合约乘数")
    slippage = decimal_value(slippage_pct, "滑点预留")
    per_fee = decimal_value(commission_per_contract, "每张手续费预留")
    minimum_fee = decimal_value(min_commission, "最低手续费预留")
    cap = _whole_positive(max_contracts, "每笔张数上限")
    if available < 0:
        raise ValidationError("可用现金不能为负数。")
    if price <= 0:
        raise ValidationError("卖一价必须大于 0。")
    if not Decimal(0) <= slippage <= Decimal(100):
        raise ValidationError("滑点预留必须在 0 到 100% 之间。")
    if per_fee < 0 or minimum_fee < 0:
        raise ValidationError("手续费预留不能为负数。")
    if cap > 1_000_000:
        raise ValidationError("每笔张数上限不能超过 1,000,000。")
    if rounding_mode not in ("nearest", "floor"):
        raise ValidationError("张数取整方式必须为 nearest（四舍五入）或 floor（预算内向下取整）。")
    if budget_mode == "amount":
        requested = decimal_value(cash_amount, "指定美元金额")
        if requested <= 0:
            raise ValidationError("指定美元金额必须大于 0。")
        budget = min(available, requested)
    else:
        requested = available * ratio / Decimal(100)
        budget = requested
    premium_unit = price * contract_multiplier
    buffered_unit = premium_unit * (Decimal(1) + slippage / Decimal(100))
    raw_quantity = budget / premium_unit
    desired = int(raw_quantity.to_integral_value(rounding=ROUND_HALF_UP))
    ceiling = available if rounding_mode == "nearest" and budget_mode == "percent" else budget
    # Both inequalities must hold because reserved fees use max(minimum, per*n).
    fee_bound = (ceiling / (buffered_unit + per_fee)).to_integral_value(rounding=ROUND_FLOOR)
    minimum_bound = ((ceiling - minimum_fee) / buffered_unit).to_integral_value(rounding=ROUND_FLOOR)
    quantity = max(0, min(cap, int(fee_bound), int(minimum_bound),
                          desired if rounding_mode == "nearest" else cap))
    premium = premium_unit * quantity
    fees = max(minimum_fee, per_fee * quantity) if quantity else Decimal(0)
    cost = buffered_unit * quantity + fees
    return {
        "quantity": quantity,
        "budget": float(budget),
        "budget_mode": budget_mode,
        "requested_budget": float(requested),
        "estimated_premium": float(premium),
        "fee_reserve": float(fees),
        "buffered_cost": float(cost),
        "unit_cost": float(buffered_unit + per_fee),
        "raw_quantity": float(raw_quantity),
        "rounding_mode": rounding_mode,
        "actual_percent": float(premium / available * 100) if available else 0.0,
        "budget_difference": float(premium - budget),
        "buffered_percent": float(cost / available * 100) if available else 0.0,
        "buffered_budget_difference": float(cost - budget),
    }


def select_settings_strike(strikes, spot, right, settings):
    """Explicit strike selects only an actually offered contract; no nearest fallback."""
    if right not in ("C", "P"):
        raise ValidationError("方向必须为 C 或 P。")
    mode = settings.get("selection_mode", "depth")
    if mode == "strike":
        strike = decimal_value(settings.get("strike"), "指定行权价")
        if strike <= 0 or strike not in {decimal_value(value, "行权价") for value in strikes}:
            raise ValidationError("指定行权价不属于当前标的、到期日及交易类别，不能自动替换成其他合约。")
        return float(strike)
    if mode != "depth":
        raise ValidationError("合约选择方式必须为 depth 或 strike。")
    return select_strike(strikes, spot, right, settings.get("moneyness", "OTM"), settings.get("depth", 4))


def opening_quantity(settings):
    """Seller size is explicit; premium received never becomes a size budget."""
    quantity = _whole_positive(settings.get("seller_quantity", 1), "卖方开仓张数")
    cap = _whole_positive(settings.get("max_contracts", 10000), "每笔张数上限")
    if quantity > cap or quantity > 1_000_000:
        raise ValidationError("卖方开仓张数超过每笔张数上限。")
    return quantity


def close_quantity(position, percent) -> int:
    """Floor a long position's requested proportion; a positive proportion is >=1."""
    quantity = _whole_positive(position, "持仓张数")
    ratio = _percent(percent, "平仓比例")
    result = int((Decimal(quantity) * ratio / Decimal(100)).to_integral_value(rounding=ROUND_FLOOR))
    return min(quantity, max(1, result))
