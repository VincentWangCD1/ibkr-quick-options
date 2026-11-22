"""Build a position-bound add ticket and resolve its sizing inputs."""
from decimal import Decimal

from core import ValidationError, decimal_value


def positive(value, label):
    result = decimal_value(value, label)
    if not 0 < result < Decimal("1e30"):
        raise ValidationError(f"{label}须为有效正数。")
    return result


def add_settings(request, held, suggested_principal=None):
    mode = request["method"]
    if mode not in ("lots", "amount", "principal"):
        raise ValidationError("请选择加仓计算方式。")
    value = positive(request["value"], {"lots": "加仓张数", "amount": "美元预算", "principal": "本金比例"}[mode])
    cap = positive(request.get("max_contracts", 10000), "每笔张数上限")
    if cap != int(cap) or cap > 1000000:
        raise ValidationError("每笔张数上限须为 1 到 1,000,000 的整数。")
    settings = {k: request[k] for k in ("commission_per_contract", "min_commission", "slippage_pct", "order_mode", "limit_price") if k in request}
    settings.setdefault("order_mode", "MID")
    if settings["order_mode"] not in ("MID", "LMT"):
        raise ValidationError("加仓请选择固定中间价或手动限价。")
    if settings["order_mode"] == "LMT":
        positive(settings.get("limit_price"), "委托限价")
    settings.update(open_action="BUY" if held > 0 else "SELL", percent=100,
                    max_contracts=int(cap), rounding_mode="floor", budget_mode="percent")
    info = dict(add_method=mode, add_value=float(value), position_quantity=held)
    if mode == "lots":
        if value != int(value) or value > cap:
            raise ValidationError("加仓张数须为正整数且不超过每笔张数上限。")
        settings.update(max_contracts=int(value), seller_quantity=int(value))
    else:
        budget = value
        if mode == "principal":
            manual = str(request.get("principal") or "").strip()
            if not manual and suggested_principal is None:
                raise ValidationError("请填写原始本金 USD。" + ("卖方请填写首次开仓投入的保证金。" if held < 0 else "现有成交记录不足以还原首笔开仓成本。"))
            base = positive(manual or suggested_principal, "原始本金")
            budget = base * value / 100
            info.update(principal=float(base), principal_source="手动填写" if manual else "首笔买单成交记录（含佣金）")
        settings.update(budget_mode="amount", cash_amount=float(budget))
    return settings, info


def short_budget_lots(settings, one):
    # The full-size IB margin check still decides whether this estimate fits.
    budget = decimal_value(one["budget"])
    unit = positive(one["buffered_cost"], "每张新增保证金及手续费")
    return min(settings["max_contracts"], int(budget // unit))


def check_add_size(size, info):
    if not size["quantity"]:
        raise ValidationError(f'本次可用预算 ${size["budget"]:,.2f}，不足以加仓 1 张（含费用预留）。')
    if info["add_method"] == "lots" and size["quantity"] != info["add_value"]:
        raise ValidationError("可用资金不足以加仓指定张数，请调整后重新预览。")


def first_buy_cost(rows, held):
    """Match the current long cycle to the first opening order, including split fills."""
    net = Decimal(0)
    opening = None
    cost = None
    for row in rows:
        qty = decimal_value(row["quantity"])
        if qty == 0:
            continue
        buy = row["action"] == "BUY"
        after = net + (qty if buy else -qty)
        if buy:
            source = row["source"]
            if source == "DEMO":
                key = row["exec_id"].rsplit(":", 1)[0]
            else:
                key = row.get("perm_id" if source == "API" else "order_id")
            identity = (source, str(key)) if key and str(key) != "0" else None
            if net <= 0 < after:
                opening = identity
                cost = Decimal(0) if net == 0 and opening else None
            if cost is not None:
                if not identity or source != opening[0]:
                    cost = None  # Independent sources may identify parts of one order differently.
                elif identity == opening:
                    if (row.get("commission") is None or row.get("commission_currency") != "USD"
                            or row.get("currency") != "USD" or not row.get("multiplier")):
                        cost = None
                    else:
                        premium = qty * decimal_value(row["price"]) * decimal_value(row["multiplier"])
                        cost += premium + decimal_value(row["commission"])
        if after <= 0:
            cost = None
        net = after
    return cost if net == decimal_value(held) and cost is not None and cost > 0 else None
