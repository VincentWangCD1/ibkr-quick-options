"""Explicit short openings and buy-to-cover, using broker margin WhatIf checks.

Premium received never funds sizing. The seller quantity is always explicit.
This module does not connect to IB; only the host's explicit send methods place
orders. A distinct durable order reference prevents long-only amendment rules
from being applied to short openings or covers after reconnecting.
"""
from __future__ import annotations

import math
import copy
import time
import uuid

from core import decimal_value, opening_quantity


def failure(message):
    from ib_broker import BrokerError
    return BrokerError(message)


class ShortOrdersMixin:
    @staticmethod
    def _short_order_role(order):
        reference = str(getattr(order, "orderRef", ""))
        if reference.startswith("QOPT1:SO:"):
            return "OPEN_SHORT"
        if reference.startswith("QOPT1:SC:"):
            return "CLOSE_SHORT"
        return None

    @staticmethod
    def _margin_number(state, name):
        try:
            value = float(getattr(state, name))
        except (TypeError, ValueError, AttributeError):
            raise failure(f"IBKR 未返回有效的保证金预检字段 {name}，未发单。") from None
        if not math.isfinite(value) or abs(value) >= 1e100:
            raise failure(f"IBKR 保证金预检字段 {name} 无效，未发单。")
        return value

    def _short_order(self, action, quantity, plan, role):
        from ib_async import LimitOrder, MarketOrder
        common = dict(account=self.account, tif="DAY", transmit=True,
                      outsideRth=plan["outside_rth"],
                      orderRef=self.ORDER_PREFIX + role + ":" + uuid.uuid4().hex[:16])
        if plan["order_type"] == "LMT":
            if not self._finite(plan.get("limit_price"), positive=True):
                raise failure("限价必须为有效正数。")
            return LimitOrder(action, int(quantity), plan["limit_price"], **common)
        if plan["order_type"] != "MKT":
            raise failure("卖方开仓/快捷买回仅支持市价或限价。")
        return MarketOrder(action, int(quantity), **common)

    def _verify_short_plan(self, detail, plan):
        session = self._session(detail)
        if (session.kind != plan["session_kind"] or session.outside_rth != plan["outside_rth"]
                or detail.contract.exchange != plan["route"]):
            raise failure("预检期间交易时段或路由变化，请重新预览；未发单。")

    def _check_short_margin_hold(self):
        """Do not reuse margin before a fresh account download reflects a fill."""
        held = self._short_margin_hold
        if not held:
            return
        trade = held["trade"]
        executed = sum(float(fill.execution.shares) for fill in trade.fills)
        reported = float(trade.orderStatus.filled)
        terminal = trade.isDone()
        cancelled = (terminal and trade.orderStatus.status in ("Cancelled", "ApiCancelled")
                     and executed == reported == 0)
        complete = terminal and executed > 0 and abs(executed - reported) < 1e-9
        available = self._financial.get("available_funds")
        reflected = (self._finite(available) and
                     float(available) <= held["available_before"] - held["consumption"] + .01)
        if cancelled or (complete and reflected):
            self._short_margin_hold = None
            return
        if complete:
            positions = [p for p in self._position_records(fresh=True, include_short=True)
                         if p.contract.conId == trade.contract.conId]
            if len(positions) == 1 and positions[0].position < 0:
                held["position_seen"] = True
                # Partial fills and changing portfolio offsets make margin
                # nonlinear. A new read-only WhatIf supplies the CURRENT
                # portfolio's Before fields; never scale the old estimate.
                probe = copy.deepcopy(trade.order)
                probe.orderId = probe.permId = 0
                probe.totalQuantity = 1
                state = self.ib.whatIfOrder(trade.contract, probe)
                initial_before = self._margin_number(state, "initMarginBefore")
                equity_before = self._margin_number(state, "equityWithLoanBefore")
                confirmed_available = equity_before - initial_before
                if (initial_before >= 0 and confirmed_available >= 0
                        and self._finite(available) and float(available) <= confirmed_available + .01):
                    self._short_margin_hold = None
                    return
            elif not positions and held.get("position_seen"):
                # Exposure has since been closed. A tool-initiated cover has
                # its own separate USD debit hold, checked by every new entry.
                self._short_margin_hold = None
                return
        raise failure("上一笔卖方开仓的成交及保证金扣减尚未同步，请刷新资金后再开仓。")

    def _check_cover_cash_hold(self):
        held = self._cover_cash_hold
        if not held:
            return
        total_debit = 0.0
        for trade in held["trades"]:
            executed = sum(float(f.execution.shares) for f in trade.fills)
            reported = float(trade.orderStatus.filled)
            if (trade.isDone() and trade.orderStatus.status in ("Cancelled", "ApiCancelled")
                    and executed == reported == 0):
                continue
            reports = [f.commissionReport for f in trade.fills]
            if (not trade.isDone() or executed <= 0 or abs(executed - reported) > 1e-9
                    or not reports or not all(r.execId and r.currency == "USD"
                       and self._finite(r.commission) and float(r.commission) < 1e100 for r in reports)):
                raise failure("买回平仓的完整成交和资金扣减尚未同步，请刷新资金后再开仓。")
            total_debit += sum(float(f.execution.shares) * float(f.execution.price)
                               * float(trade.contract.multiplier) for f in trade.fills)
            total_debit += sum(float(r.commission) for r in reports)
        current = self._financial.get("usd_cash")
        if total_debit == 0 or (self._finite(current) and self._finite(held["cash_before"])
                and current <= held["cash_before"] - total_debit + .01):
            self._cover_cash_hold = None
            return
        raise failure("买回平仓的美元扣款尚未同步，请刷新资金后再开仓。")

    def _short_margin_estimate(self, detail, settings, plan, quote):
        quantity = opening_quantity(settings)
        self._verify_short_plan(detail, plan)
        self._sync_cash(force=True)
        self._check_short_margin_hold()
        self._check_cover_cash_hold()
        if self._opening_blocked(detail.contract.conId):
            raise failure("账户有未完成订单，请等待成交/撤单后再预览卖方开仓。")
        if plan.get("add_method"):
            self._add_position(detail.contract.conId, plan["position_quantity"], plan["position_avg_cost"])
        elif any(p.contract.conId == detail.contract.conId
                 for p in self._position_records(fresh=True, include_short=True)):
            raise failure("该合约已有持仓；卖方开仓仅创建新空头，请先管理现有持仓。")
        available = self._financial.get("available_funds")
        if not self._finite(available, positive=True) or self._financial.get("base_currency") != "USD":
            raise failure("卖方开仓需要已验证的 USD 可用资金及保证金预检。")
        available = float(available)
        # The same allocation control means cash budget for buyers and a hard
        # incremental-margin allocation for sellers; neither uses premium credit.
        mode = settings.get("budget_mode", "percent")
        if mode == "amount":
            requested = float(decimal_value(settings.get("cash_amount"), "指定美元金额"))
            if requested <= 0:
                raise failure("指定美元金额必须大于 0。")
        elif mode == "percent":
            percent = float(decimal_value(settings.get("percent", 50), "现金比例"))
            if not 0 < percent <= 100:
                raise failure("现金比例须大于 0 且不超过 100%。")
            requested = min(self._cash, available) * percent / 100
        else:
            raise failure("预算方式必须为 percent 或 amount。")
        budget = min(self._cash, available, requested)
        order = self._short_order("SELL", quantity, plan, "SO")
        state = self.ib.whatIfOrder(detail.contract, order)
        if state.warningText:
            raise failure(f"IBKR 保证金预检提示，未发单：{state.warningText}")
        initial = self._margin_number(state, "initMarginChange")
        maintenance = self._margin_number(state, "maintMarginChange")
        equity_change = self._margin_number(state, "equityWithLoanChange")
        initial_after = self._margin_number(state, "initMarginAfter")
        maintenance_after = self._margin_number(state, "maintMarginAfter")
        equity_after = self._margin_number(state, "equityWithLoanAfter")
        if initial <= 0 or maintenance < 0 or initial_after < 0 or maintenance_after < 0:
            raise failure("本版卖方仅支持具有明确正数新增保证金的独立开仓；预检结果不适用。")
        if equity_after < max(initial_after, maintenance_after):
            raise failure("IBKR 预检显示成交后权益不足以满足保证金，未发单。")
        per_fee = float(decimal_value(settings.get("commission_per_contract", 1), "每张手续费"))
        minimum = float(decimal_value(settings.get("min_commission", 1), "最低手续费"))
        if per_fee < 0 or minimum < 0:
            raise failure("手续费预留不能为负数。")
        fee = max(minimum, quantity * per_fee)
        if self._finite(state.commission) and float(state.commission) < 1e100:
            if state.commissionCurrency not in ("", "USD"):
                raise failure("预检手续费不是 USD，未发单。")
            fee = max(fee, float(state.commission))
        margin = max(initial, maintenance, initial - equity_change, maintenance - equity_change)
        cost = margin + fee
        # A new account download following WhatIf can lower, never raise this cap.
        self._sync_cash(force=True)
        current_available = self._financial.get("available_funds")
        if not self._finite(current_available):
            raise failure("保证金预检后可用资金失效，未发单。")
        budget = min(budget, self._cash, float(current_available))
        if cost > budget + 1e-9:
            raise failure(f"卖出 {quantity} 张需预留保证金及费用约 ${cost:,.2f}，超过预算 ${budget:,.2f}；请减少卖方张数或调整预算。")
        credit = (plan["limit_price"] if plan["order_type"] == "LMT" else quote["bid"]) * quantity * float(detail.contract.multiplier)
        return dict(quantity=quantity, raw_quantity=float(quantity), rounding_mode="manual",
                    margin_checked_at=time.time(),
                    budget=budget, budget_mode=mode, requested_budget=requested,
                    estimated_credit=credit, estimated_premium=credit, fee_reserve=fee,
                    margin_required=margin, buffered_cost=cost, unit_cost=cost / quantity,
                    initial_margin_change=initial, maintenance_margin_change=maintenance,
                    margin_available=min(available, float(current_available)),
                    margin_consumption=max(initial - equity_change, 0),
                    actual_percent=cost / available * 100, buffered_percent=cost / available * 100,
                    budget_difference=cost - budget, buffered_budget_difference=cost - budget)

    def _preview_short(self, detail, settings, plan, quote):
        return self._short_margin_estimate(detail, settings, plan, quote)

    def _send_short_open(self, detail, preview, settings, deadline):
        self._require()
        quote = self._quote(detail.contract)
        estimate = self._short_margin_estimate(detail, settings, preview, quote)
        if estimate["quantity"] != preview["quantity"] or estimate["buffered_cost"] > preview["buffered_cost"] + .01:
            raise failure("卖方保证金或数量已变化，请重新预览后确认；未发单。")
        if self._opening_blocked(detail.contract.conId):
            raise failure("预检期间出现挂单，请重新预览。")
        if preview.get("add_method"):
            self._add_position(detail.contract.conId, preview["position_quantity"], preview["position_avg_cost"])
        elif any(p.contract.conId == detail.contract.conId
                 for p in self._position_records(fresh=True, include_short=True)):
            raise failure("预检期间订单或持仓变化，卖方开仓未提交。")
        self._require()
        self._verify_short_plan(detail, preview)
        final_quote = self._quote(detail.contract, wait=0)
        if time.monotonic() > deadline:
            raise failure("卖方预检超过预览有效期，请刷新；未发单。")
        floor = preview["limit_price"] if preview["order_type"] == "LMT" else quote["bid"]
        if preview.get("price_mode", "MKT") == "MKT" and final_quote["bid"] < floor:
            raise failure("预检期间买一价下跌，请重新预览卖方订单；未发单。")
        order = self._short_order("SELL", estimate["quantity"], preview, "SO")
        trade = self._place_short_order(detail, order)
        self._short_margin_hold = dict(trade=trade, available_before=estimate["margin_available"],
                                       consumption=estimate["margin_consumption"])
        self.log(f"已提交卖方开仓 SELL {estimate['quantity']} 张 {detail.contract.localSymbol}；预计收取权利金 ${estimate['estimated_credit']:,.2f}，保证金预留 ${estimate['buffered_cost']:,.2f}。提交不代表成交。")
        return self._order_dict(trade)

    def _send_short_cover(self, detail, quantity, plan=None, deadline=None):
        self._require()
        contract = detail.contract
        # Capture USD ledger cash for an entry-only hold; cash readiness never
        # prevents a broker-approved risk-reducing cover.
        self._financial = self._account_data.refresh(self.account)
        cash_before = self._financial.get("usd_cash")
        side = "ask" if (plan or {}).get("price_mode", "MKT") == "MKT" else None
        quote = self._quote(contract, required_side=side)
        plan = plan or self._execution_plan(detail, "BUY", quote)
        order = self._short_order("BUY", quantity, plan, "SC")
        state = self.ib.whatIfOrder(contract, order)
        if state.warningText:
            raise failure(f"IBKR 买回预检提示，未发单：{state.warningText}")
        records = [p for p in self._position_records(fresh=True, include_short=True)
                   if p.contract.conId == contract.conId]
        if len(records) != 1 or records[0].position >= 0 or abs(records[0].position) < quantity:
            raise failure("预检期间空头持仓变化，买回数量可能超过持仓；未发单。")
        if plan.get("close_intent") and float(records[0].position) != plan["expected_position"]:
            raise failure("预检期间空头持仓数量变化，请重新预览买回平仓；未发单。")
        baseline = abs(float(records[0].position))
        if (self._short_margin_hold and
                self._short_margin_hold["trade"].contract.conId == contract.conId):
            self._short_margin_hold["position_seen"] = True
        self._check_close_reservation(contract.conId, baseline)
        if any(t.contract.conId == contract.conId for t in self._open_orders()):
            raise failure("预检期间该合约出现未完成订单，买回未提交。")
        self._verify_short_plan(detail, plan)
        self._require()
        wait = min(2., max(0., deadline - time.monotonic())) if deadline is not None else 2.
        final_quote = self._quote(contract, wait=wait, required_side=side)
        ceiling = plan["limit_price"] if plan["order_type"] == "LMT" else quote["ask"]
        if plan.get("price_mode", "MKT") == "MKT" and final_quote["ask"] > ceiling:
            raise failure("预检期间卖一价上涨，请重新买回平仓；未发单。")
        if deadline is not None and time.monotonic() > deadline:
            raise failure("买回平仓预览已过期，请重新预览；未发单。")
        # Covers reduce an existing short and do not require the long-entry
        # settled-cash budget. The WhatIf permission check plus re-read signed
        # holdings ensures the order cannot intentionally open a long position.
        trade = self._place_short_order(detail, order)
        if self._cover_cash_hold is None:
            self._cover_cash_hold = {"cash_before": cash_before, "trades": []}
        self._cover_cash_hold["trades"].append(trade)
        self._close_reservations[contract.conId] = (trade, baseline)
        self.log(f"已提交 BUY 买回平仓 {quantity} 张 {contract.localSymbol}；提交不代表成交。")
        return self._order_dict(trade)

    def _place_short_order(self, detail, order):
        self._tokens.clear()
        getattr(self, "_close_tokens", {}).clear()
        try:
            trade = self.ib.placeOrder(detail.contract, order)
        except Exception as exc:
            self._uncertain = True
            raise failure("发单结果不明确，已锁定。请到 TWS 核对；禁止直接重试。") from exc
        self._dirty_account = True
        return trade
