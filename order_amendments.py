"""Explicit, single-use previews for this client's own plain option orders.

IBKR recommends same-ID modification for price, and cancel/new for other fields:
https://www.interactivebrokers.com/docs/tws-api/doc/orders/modify-order
No network connection is opened here. The host broker owns all API access.
"""
from __future__ import annotations

import copy
import math
import time
import uuid

from entry_orders import build_entry_plan


def failure(message):
    from ib_broker import BrokerError
    return BrokerError(message)


class OrderAmendmentsMixin:
    CANCEL_WAIT_SECONDS = 8.0
    AMEND_WAIT_SECONDS = 5.0

    def _managed_order(self, order_id, refresh=True):
        self._require()
        if refresh:
            self._open_orders()
        matches = [t for t in self.ib.trades() if t.order.orderId == int(order_id)
                   and t.order.account == self.account and self._ours(t)]
        if len(matches) != 1:
            raise failure("只能操作当前账户、当前 Client ID、带本程序标记的唯一订单；不修改手工或其他客户端订单。")
        return matches[0]

    @staticmethod
    def _fill_quantity(trade):
        return sum(float(f.execution.shares) for f in trade.fills)

    def _plain_order(self, trade):
        o, s, c = trade.order, trade.orderStatus, trade.contract
        if self._short_order_role(o):
            raise failure("卖方开仓/买回订单暂不快捷改单；请先撤单并确认，再重新预检保证金及持仓。")
        if (s.status not in ("Submitted", "PreSubmitted") or trade.isDone()
                or o.orderId in self._amend_pending):
            raise failure("订单必须已确认且仍有效；成交、撤单中、改单待确认或状态不明确的订单不能修改。")
        if o.orderType not in ("MKT", "LMT"):
            raise failure("本版仅改单普通市价/限价单。止损、止损限价及移动止损单请先单独撤单，确认后再重新设置。")
        if (o.action not in ("BUY", "SELL") or c.secType != "OPT" or c.currency != "USD"
                or str(c.multiplier) != "100" or o.parentId or o.ocaGroup
                or o.conditions or o.algoStrategy or o.hedgeType):
            raise failure("该订单不是本版支持的独立标准期权单，不能快捷改单。")
        total, filled = float(o.totalQuantity), float(s.filled)
        if (not math.isfinite(total + filled) or total != int(total) or filled != int(filled)
                or not 0 <= filled < total or float(s.remaining) != total - filled):
            raise failure("订单总数、成交数和余量尚未同步，不能改单。")
        if abs(self._fill_quantity(trade) - filled) > 1e-9:
            raise failure("完整成交明细尚未同步，请稍后重新预览。")

    @staticmethod
    def _order_fingerprint(trade):
        return (repr(trade.order), repr(trade.contract), trade.orderStatus.status,
                float(trade.orderStatus.filled), float(trade.orderStatus.remaining),
                tuple((f.execution.execId, float(f.execution.shares), float(f.execution.price))
                      for f in trade.fills))

    @staticmethod
    def _replacement_identity(trade):
        o, c = trade.order, trade.contract
        return (c.conId, c.secType, c.symbol, c.currency, c.multiplier, c.exchange,
                o.account, o.clientId, o.orderId, o.action, float(o.totalQuantity),
                o.orderType, o.lmtPrice, o.tif, o.outsideRth, o.parentId, o.ocaGroup, o.orderRef)

    def _amend_detail(self, contract):
        requested = copy.copy(contract)  # Preserve the original exchange/route.
        matches = [d for d in self.ib.reqContractDetails(requested)
                   if d.contract and d.contract.conId == requested.conId]
        if len(matches) != 1:
            raise failure("无法唯一核对原订单合约，未改单。")
        detail = copy.copy(matches[0])
        detail.contract = requested
        self._session(detail)
        return detail

    def _amend_plan(self, trade, detail, order_mode, limit_price=None):
        session = self._session(detail)
        if session.kind == "extended" and order_mode == "MKT":
            raise failure("延长时段不能把挂单改为真正市价单；请选择明确限价或固定 MID。")
        if session.kind == "extended" and not trade.order.outsideRth:
            raise failure("原单未启用盘外交易；快捷改单保留原有效时段，请在常规时段操作或撤单后重新设置。")
        quote = self._quote(detail.contract)
        original_settings = self._order_sizing_settings.get(trade.order.orderId)
        reserves = {"slippage_pct": 5, "commission_per_contract": 1, "min_commission": 1}
        if original_settings is not None:
            reserves.update(original_settings)
        plan = build_entry_plan(trade.order.action, order_mode, quote, self._increments(detail),
                                session_kind=session.kind, limit_price=limit_price,
                                slippage_pct=reserves["slippage_pct"])
        plan.update(outside_rth=trade.order.outsideRth, session_kind=session.kind,
                    route=detail.contract.exchange, **reserves,
                    reserve_settings_source="original" if original_settings is not None else "defaults")
        return plan, quote

    def preview_modify(self, order_id, order_mode, limit_price=None):
        trade = self._managed_order(order_id)
        self._plain_order(trade)
        fingerprint = self._order_fingerprint(trade)
        detail = self._amend_detail(trade.contract)
        plan, quote = self._amend_plan(trade, detail, order_mode, limit_price)
        # No implicit re-pricing of MID after this preview.
        if self._order_fingerprint(trade) != fingerprint:
            raise failure("生成预览时原订单已变化，请重新预览。")
        method = "amend" if trade.order.orderType == plan["order_type"] == "LMT" else "replace"
        if trade.order.orderType == plan["order_type"] == "MKT":
            raise failure("原订单已是市价单；不能通过重复提交加速成交。")
        token = uuid.uuid4().hex
        old_limit = float(trade.order.lmtPrice) if trade.order.orderType == "LMT" else None
        result = dict(token=token, order_id=trade.order.orderId, action=trade.order.action,
                      local_symbol=trade.contract.localSymbol, quantity=float(trade.order.totalQuantity),
                      filled=float(trade.orderStatus.filled), remaining=float(trade.orderStatus.remaining),
                      old_order_type=trade.order.orderType, old_limit_price=old_limit,
                      order_type=plan["order_type"], limit_price=plan["limit_price"],
                      new_order_type=plan["order_type"], new_limit_price=plan["limit_price"],
                      price_mode=plan["price_mode"], outside_rth=plan["outside_rth"],
                      method=method, created_at=time.time(), mode=self.mode, account=self.account,
                      bid=quote["bid"], ask=quote["ask"], tif=trade.order.tif,
                      route=trade.contract.exchange,
                      slippage_pct=plan["slippage_pct"],
                      commission_per_contract=plan["commission_per_contract"],
                      min_commission=plan["min_commission"],
                      reserve_settings_source=plan["reserve_settings_source"])
        self._modify_tokens = {k: v for k, v in self._modify_tokens.items()
                               if time.monotonic() - v["created"] <= self.TOKEN_TTL}
        self._modify_tokens[token] = dict(preview=copy.deepcopy(result), plan=copy.deepcopy(plan),
                                          fingerprint=fingerprint, detail=detail, created=time.monotonic())
        return result

    def _verify_amend_context(self, saved, trade, *, check_state=True):
        self._require()
        guard = getattr(self, "_auto_mid_guard", None)
        if guard is not None and not guard():
            raise failure("自动追价已停止或到时，未提交修改。")
        if time.monotonic() - saved["created"] > self.TOKEN_TTL:
            raise failure("改单预览已超过有效期，未提交修改；请重新预览。")
        if saved["preview"]["account"] != self.account or not self._ours(trade):
            raise failure("账户或订单归属改变，未改单。")
        latest = self._session(saved["detail"])
        if latest.kind != saved["plan"]["session_kind"]:
            raise failure("预览后交易时段改变，未改单。")
        self._quote(saved["detail"].contract, wait=0)
        if check_state and self._order_fingerprint(trade) != saved["fingerprint"]:
            raise failure("原订单的状态、价格或成交数已变化，请重新预览；未改单。")

    def _amend_cost(self, trade, plan, quantity):
        quote = self._quote(trade.contract, wait=0)
        unit = (float(plan["limit_price"]) if plan["order_type"] == "LMT" else quote["ask"] * (1 + float(plan.get("slippage_pct", 5)) / 100)) * float(trade.contract.multiplier)
        fee = max(float(plan.get("min_commission", 1)), quantity * float(plan.get("commission_per_contract", 1)))
        return unit * quantity + fee, quote["ask"], fee

    def _filled_debit(self, trade):
        if not trade.fills:
            return 0.0
        if abs(self._fill_quantity(trade) - float(trade.orderStatus.filled)) > 1e-9:
            raise failure("原单成交明细未完整同步，未提交新单。")
        total = 0.0
        for fill in trade.fills:
            report = fill.commissionReport
            if (not report.execId or report.currency != "USD" or not math.isfinite(float(report.commission))
                    or abs(float(report.commission)) >= 1e100):
                raise failure("原单成交手续费尚未返回，暂不能安全重算买单现金。")
            total += float(fill.execution.shares) * float(fill.execution.price) * float(trade.contract.multiplier)
            total += max(0.0, float(report.commission))
        return total

    def _amend_capacity(self, trade, plan, quantity, commission=None):
        others = [t for t in self._open_orders() if t is not trade and
                  (t.order.clientId, t.order.orderId) != (trade.order.clientId, trade.order.orderId)]
        if trade.order.action == "BUY":
            if self._opening_blocked(trade.contract.conId, others):
                raise failure("账户还有其他未完成订单，无法独立核对改单现金。")
            if self._reserve_trade is not None and self._reserve_trade is not trade:
                raise failure("其他买单的现金扣款尚未核对，不能改单。")
            self._sync_cash(force=True)
            self._check_short_margin_hold()
            self._check_cover_cash_hold()
            # _cash includes this same order's local hold. Use the freshly read
            # broker cash once, then cover old filled debits not reflected yet.
            capacity = float(self._raw_cash)
            paid = self._filled_debit(trade)
            if self._reserve_trade is trade and self._reserve_cash_baseline is not None:
                # AvailableFunds can include this order's exchange/broker hold.
                # Restore at most this original LMT's remaining principal,
                # exactly once; never lift any USD/settled-cash restriction.
                financial = self._financial
                caps = [financial.get("usd_cash"), financial.get("settlement_cash_cap")]
                available = financial.get("available_funds")
                ledger = financial.get("ledger_settled_cash")
                if ledger is not None:
                    caps.append(ledger)
                if (trade.order.orderType == "LMT" and financial.get("ready")
                        and all(self._finite(v) for v in caps) and self._finite(available)
                        and self._finite(trade.order.lmtPrice, positive=True)):
                    unfilled = float(trade.order.totalQuantity) - float(trade.orderStatus.filled)
                    own_principal = unfilled * float(trade.order.lmtPrice) * float(trade.contract.multiplier)
                    own_allowance = min(own_principal, self._debit_reserve)
                    capacity = min(*caps, float(available) + own_allowance)
                capacity = min(capacity, max(0.0, self._reserve_cash_baseline - paid))
            cost, ask, fee = self._amend_cost(trade, plan, quantity)
            if commission is not None:
                cost += max(0.0, commission - fee)
            auto_cap = getattr(self, "_auto_mid_budget_cap", None)
            if auto_cap is not None and paid + cost > auto_cap + 1e-9:
                raise failure("最新委托连同已成交扣款及费用超过自动追价预算，未改单。")
            if cost > capacity + 1e-9:
                raise failure("修改后的未成交部分超出当前可核对现金（含手续费/市价缓冲），未提交修改。")
            return dict(cost=cost, paid=paid, capacity=capacity, checked_ask=ask,
                        sizing_settings={k: plan.get(k, default) for k, default in
                                         (("slippage_pct", 5), ("commission_per_contract", 1), ("min_commission", 1))})
        if any(t.contract.conId == trade.contract.conId for t in others):
            raise failure("该合约还有其他未完成订单，不能安全改单。")
        positions = [p for p in self._position_records(fresh=True) if p.contract.conId == trade.contract.conId]
        if len(positions) != 1 or float(positions[0].position) < quantity:
            raise failure("当前多头持仓不足以覆盖改单余量，未提交修改。")
        held = self._close_reservations.get(trade.contract.conId)
        if held is not None and held[0] is not trade:
            raise failure("该合约另有平仓扣减尚未确认，不能改单。")
        baseline = float(positions[0].position)
        # Own fills may arrive before the position update. Bound the observed
        # position by the original pre-close quantity minus reported fills.
        if held is not None:
            baseline = min(baseline, max(0.0, held[1] - float(trade.orderStatus.filled)))
        if baseline < quantity:
            raise failure("原平仓成交/持仓变化后，剩余可卖数量不足；未改单。")
        return dict(position=baseline)

    @staticmethod
    def _copy_changed_order(trade, plan, quantity, replace):
        from ib_async.util import UNSET_DOUBLE
        order = copy.deepcopy(trade.order)
        order.orderType = plan["order_type"]
        order.lmtPrice = plan["limit_price"] if plan["order_type"] == "LMT" else UNSET_DOUBLE
        # A same-ID amendment retains ORIGINAL total, including already filled.
        order.totalQuantity = quantity if replace else trade.order.totalQuantity
        order.transmit = True
        if replace:
            order.orderId = 0
            order.permId = 0
            order.orderRef = trade.order.orderRef.split(":replace:")[0] + ":replace:" + uuid.uuid4().hex[:8]
        return order

    def _whatif_amend(self, trade, order):
        probe = copy.deepcopy(order)
        probe.orderId = 0
        probe.permId = 0
        # WhatIf tests only the not-yet-filled part, not historical fills.
        probe.totalQuantity = int(float(trade.order.totalQuantity) - float(trade.orderStatus.filled))
        state = self.ib.whatIfOrder(trade.contract, probe)
        if state.warningText:
            raise failure(f"IBKR 改单预检提示，未修改：{state.warningText}")
        commission = None
        if self._finite(state.commission) and float(state.commission) < 1e100:
            if state.commissionCurrency not in ("", "USD"):
                raise failure("改单手续费币种不是 USD，未修改。")
            commission = float(state.commission)
        return commission

    def _wait_cancelled(self, trade, *, verify_protective_exit=False):
        expires = time.monotonic() + self.CANCEL_WAIT_SECONDS
        while time.monotonic() < expires:
            self._require()
            if trade.orderStatus.status in ("Cancelled", "ApiCancelled", "Filled"):
                # Refresh executions before deriving the replacement quantity.
                received = self.ib.reqExecutions()
                if verify_protective_exit:
                    if not isinstance(received, (list, tuple)):
                        raise failure("无法确认撤单期间的成交记录，平仓已停止。")
                    local = list(trade.fills)
                    if any(not self._matching_close_fill(trade, f) or not f.execution.execId for f in local):
                        raise failure("保护单成交明细与订单不符，请核对剩余持仓。")
                    matches = [f for f in [*local, *received] if self._matching_close_fill(trade, f)]
                    if any(not f.execution.execId for f in matches):
                        raise failure("无法确认撤单期间的成交记录，平仓已停止。")
                    executed = sum(float(f.execution.shares) for f in self._latest_close_fills(matches).values())
                else:
                    executed = self._fill_quantity(trade)
                reported = float(trade.orderStatus.filled)
                total = float(trade.order.totalQuantity)
                if (math.isfinite(reported) and reported == int(reported)
                        and 0 <= reported <= total and abs(executed - reported) < 1e-9):
                    if trade.orderStatus.status == "Filled" and reported != total:
                        self.ib.sleep(0.05)
                        continue
                    return int(total - reported)
            self.ib.sleep(0.05)
        raise failure("撤单或成交明细未在时限内确认；没有提交替代单。请先在 TWS 核对，勿重复点击。")

    def _record_amend_reserve(self, original, resulting, risk, replace):
        if original.order.action == "BUY":
            self._order_sizing_settings[resulting.order.orderId] = copy.deepcopy(risk["sizing_settings"])
            if self._reserve_trade is None:
                # Capture a ledger baseline only before any part of this buy
                # was filled. A later snapshot cannot stand in for that balance.
                self._reserve_usd_baseline = self._financial.get("usd_cash") if not risk["paid"] else None
            if replace and risk["paid"]:
                self._reserve_usd_baseline = None
            if replace:
                self._reserve_cash_baseline = risk["capacity"]
                self._debit_reserve = risk["cost"]
            else:
                if self._reserve_cash_baseline is None:
                    self._reserve_cash_baseline = risk["capacity"] + risk["paid"]
                self._debit_reserve = risk["cost"] + risk["paid"]
            self._reserve_trade = resulting
            self._cash = max(0.0, min(self._raw_cash, self._reserve_cash_baseline - self._debit_reserve))
        else:
            baseline = risk["position"] if replace else risk["position"] + float(original.orderStatus.filled)
            self._close_reservations[original.contract.conId] = (resulting, baseline)
        self._dirty_account = True

    def modify_order(self, token):
        self._require()
        saved = self._modify_tokens.pop(token, None)
        if saved is None:
            raise failure("改单预览已失效或已经使用，请重新预览。")
        trade = self._managed_order(saved["preview"]["order_id"])
        self._plain_order(trade)
        self._verify_amend_context(saved, trade)
        replace = saved["preview"]["method"] == "replace"
        remaining = int(float(trade.order.totalQuantity) - float(trade.orderStatus.filled))
        order = self._copy_changed_order(trade, saved["plan"], remaining, replace)
        commission = self._whatif_amend(trade, order)
        self._amend_capacity(trade, saved["plan"], remaining, commission)
        self._verify_amend_context(saved, trade)
        if replace:
            identity = self._replacement_identity(trade)
            try:
                self.ib.cancelOrder(trade.order)
                remaining = self._wait_cancelled(trade)
                if self._replacement_identity(trade) != identity:
                    raise failure("撤单期间原单数量、方向或其他参数变化，没有提交替代单。")
                if remaining > saved["preview"]["remaining"]:
                    raise failure("撤单后余量超过预览数量，没有提交替代单。")
            except Exception as exc:
                self._amend_pending.add(trade.order.orderId)
                raise failure(f"替换已停止，没有发送新单。{exc}") from exc
            if remaining == 0:
                self.log(f"原订单 {trade.order.orderId} 在撤单期间已全部成交，没有发送替代单。")
                return {**self._order_dict(trade), "modification": "filled_before_replace"}
            # Cancellation may take longer than the preview TTL. The original
            # reviewed price remains immutable, but require same session/book.
            self._require()
            if self._session(saved["detail"]).kind != saved["plan"]["session_kind"]:
                raise failure("原单已撤销，但交易时段变化，没有发送替代单。")
            self._quote(trade.contract, wait=0)
            order = self._copy_changed_order(trade, saved["plan"], remaining, True)
            try:
                commission = self._whatif_amend(trade, order)
                risk = self._amend_capacity(trade, saved["plan"], remaining, commission)
                self._require()
                if trade.orderStatus.status not in ("Cancelled", "ApiCancelled"):
                    raise failure("原订单未保持已撤状态，未发替代单。")
                if self._fill_quantity(trade) != float(trade.orderStatus.filled):
                    raise failure("撤单后成交明细变化，未发替代单。")
                latest_remaining = int(float(trade.order.totalQuantity) - float(trade.orderStatus.filled))
                if latest_remaining != remaining:
                    raise failure("撤单后余量再次变化，未发替代单；请刷新。")
                if self._replacement_identity(trade) != identity:
                    raise failure("撤单后原订单参数变化，未发替代单。")
                latest_quote = self._quote(trade.contract, wait=0)
                if (trade.order.action == "BUY" and saved["plan"]["order_type"] == "MKT"
                        and latest_quote["ask"] > risk["checked_ask"]):
                    raise failure("买入市价报价在资金检查后上涨，未发替代单；请刷新重新设置。")
                if self._session(saved["detail"]).kind != saved["plan"]["session_kind"]:
                    raise failure("交易时段变化，未发替代单。")
            except Exception as exc:
                raise failure(f"原单已撤销，但替代单未发送：{exc}") from exc
            try:
                new_trade = self.ib.placeOrder(copy.copy(trade.contract), order)
            except Exception as exc:
                self._uncertain = True
                raise failure("替代单结果不明确，已锁定；请在 TWS 核对，禁止自动重试。") from exc
            self._record_amend_reserve(trade, new_trade, risk, True)
            self._tokens.clear()
            self.log(f"原单 {trade.order.orderId} 已确认撤销，仅替换剩余 {remaining} 张；新单 {new_trade.order.orderId}，提交不代表成交。")
            return {**self._order_dict(new_trade), "modification": "replacement_submitted",
                    "replaced_order_id": trade.order.orderId}
        risk = self._amend_capacity(trade, saved["plan"], remaining, commission)
        self._verify_amend_context(saved, trade)
        return self._amend_price(trade, order, "lmtPrice", risk)

    def _amend_price(self, trade, order, field, risk=None):
        original_order = copy.deepcopy(trade.order)
        acknowledged = []
        def opened(updated):
            if (updated.order.orderId == original_order.orderId and updated.order.account == self.account
                    and updated.order.clientId == self.client_id
                    and updated.order.orderType == order.orderType
                    and abs(float(getattr(updated.order, field)) - float(getattr(order, field))) < 1e-9):
                acknowledged.append(True)
        self.ib.openOrderEvent += opened
        self._amend_pending.add(original_order.orderId)
        try:
            changed = self.ib.placeOrder(copy.copy(trade.contract), order)
            if risk is not None:
                self._record_amend_reserve(trade, changed, risk, False)
            end = time.monotonic() + self.AMEND_WAIT_SECONDS
            while not acknowledged and not changed.isDone() and time.monotonic() < end:
                self._require()
                self.ib.sleep(0.05)
            if acknowledged or changed.isDone():
                self._amend_pending.discard(original_order.orderId)
            else:
                self.log("改单请求已发出，但尚未收到对应价格确认；已暂停该单再次修改，请在 TWS 核对。")
            self._tokens.clear()
            return {**self._order_dict(changed), "modification": "amend_acknowledged" if acknowledged else "amend_requested"}
        except Exception as exc:
            self._uncertain = True
            raise failure("改单请求结果不明确，已锁定；请到 TWS 核对，禁止自动重试。") from exc
        finally:
            self.ib.openOrderEvent -= opened

    def cancel_order(self, order_id):
        trade = self._managed_order(order_id)
        if trade.isDone() or trade.orderStatus.status in ("PendingCancel", "ApiPending"):
            raise failure("订单已结束或正在等待撤单/确认，请刷新状态。")
        self._modify_tokens.clear()
        self._tokens.clear()
        try:
            self.ib.cancelOrder(trade.order)
        except Exception as exc:
            self._uncertain = True
            raise failure("撤单请求结果不明确，请在 TWS 核对；未自动重试。") from exc
        self.log(f"已请求撤销本程序订单 {trade.order.orderId}，请等待 Cancelled 回报。")
        return {**self._order_dict(trade), "cancellation": "requested"}
