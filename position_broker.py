"""Read-only position marks, frozen close previews and close-fill reconciliation.

The cached portfolio and mark fields are display references, never permission
to place an order. Close confirmation still requires the broker's strict quote,
calendar, holding and order checks immediately before a single transmission.
"""
from __future__ import annotations

import copy
import math
import time
import uuid
from types import SimpleNamespace
from typing import NamedTuple

from core import close_quantity
from market_runtime import pin_quote_contract, release_quote_stream
from position_analytics import enrich_position
from exit_orders import STOP_TYPES


def failure(message):
    from ib_broker import BrokerError
    return BrokerError(message)


class StopWithAdds(NamedTuple):
    trade: object
    initial_position: float
    buys: tuple


class PositionBrokerMixin:
    CLOSE_SYNC_INTERVAL = 2.0

    def _get_history_store(self):
        if self._history_store is None:
            from trade_history import TradeHistoryStore
            self._history_store = TradeHistoryStore(self._history_path)
        return self._history_store

    def _history_backfill(self):
        from ib_async import ExecutionFilter
        fills = self.ib.reqExecutions(ExecutionFilter(acctCode=self.account))
        if not isinstance(fills, (list, tuple)):
            raise failure("成交同步未返回有效列表。")
        for fill in fills:
            if getattr(fill.execution, "acctNumber", "") == self.account:
                try:
                    self._get_history_store().record_ib_fill(self.mode, fill, account=self.account)
                except Exception as exc:
                    self.log(f"成交历史写入暂未完成（{type(exc).__name__}）；原始成交核验继续保留。")
        return fills

    def last_session_pnl(self):
        if not self.account:
            raise failure("请先连接对应账户后查看最近交易日盈亏。")
        return self._get_history_store().last_session_pnl(self.account, self.mode)

    def execution_history(self, period="today", tz_name="Asia/Shanghai", refresh=False):
        if not self.account:
            raise failure("请先连接并核对账户，再查看该账户的成交历史。")
        error = ""
        if refresh:
            try:
                self._history_backfill()
            except Exception as exc:
                error = f"TWS 成交回补未完成（{type(exc).__name__}），当前仅显示本地记录。"
        result = self._get_history_store().history(self.account, self.mode, period=period, tz_name=tz_name)
        result["api_refresh_error"] = error
        from trade_history import API_HISTORY_NOTE
        result["api_history_note"] = API_HISTORY_NOTE
        return result

    def import_execution_history(self, path, tz_name="America/New_York"):
        if not self.account:
            raise failure("请先连接并核对导入报表对应的账户。")
        return self._get_history_store().import_flex(path, self.account, self.mode, tz_name=tz_name)

    def import_flex_reports(self, reports, tz_name="America/New_York", coverage_note="", query_id=None, report_windows=()):
        if not self.account:
            raise failure("请先连接对应账户。")
        result = self._get_history_store().import_flex_batch([(raw, ".xml") for raw in reports], self.account, self.mode, tz_name,
                                                             query_id=query_id, report_windows=report_windows)
        return dict(result, coverage_note=coverage_note)

    def _position_quote(self, contract):
        routed = copy.copy(contract)
        routed.exchange = "SMART"
        pin_quote_contract(self, routed, seconds=30)
        try:
            ticker = self._subscribe(routed)
        except Exception as exc:
            return {"quote_type": "持仓行情暂不可用：" + str(exc)}
        stamps = self._display_times.get(contract.conId, {})
        now = time.monotonic()
        age = lambda key: max(0., now - stamps[key]) if key in stamps else None
        return {key: self._display_number(getattr(ticker, key, None)) for key in ("bid", "ask", "last")} | {
            "market_data_type": ticker.marketDataType,
            "halted": ticker.halted in (1, 2), "bid_age": age(1), "ask_age": age(2), "last_age": age(4),
            "quote_type": {1: "实时订阅", 2: "冻结", 3: "延迟", 4: "延迟冻结"}.get(ticker.marketDataType, "等待报价")}

    def _position_rows(self):
        if not self.ib.isConnected() or not self.account:
            return []
        records = self._position_records(include_short=True)
        portfolio = self.ib.portfolio(self.account)
        portfolio = portfolio if isinstance(portfolio, (list, tuple)) else []
        by_conid = {p.contract.conId: {"market_price": p.marketPrice, "market_value": p.marketValue,
                                     "unrealized_pnl": p.unrealizedPNL}
                    for p in portfolio if p.account == self.account}
        trades = self.ib.trades()
        trades = trades if isinstance(trades, (list, tuple)) else []
        order_rows = [self._order_dict(t) for t in trades if t.order.account == self.account
                      and self._order_identity(t) not in getattr(self, "_missing_external_orders", set())]
        active = {p.contract.conId for p in records}
        previous = getattr(self, "_position_stream_ids", set())
        for conid in previous - active - getattr(self, "_chain_stream_ids", set()):
            if not any(p[0]["contract"]["conid"] == conid for p in self._tokens.values()):
                release_quote_stream(self, conid)
        self._position_stream_ids = active
        result = []
        for p in records:
            base = {**self._contract_dict(p.contract), "quantity": float(p.position),
                    "avg_cost": float(p.avgCost), "position_side": "SHORT" if p.position < 0 else "LONG",
                    "supported": True}
            row = enrich_position(base, self._position_quote(p.contract), by_conid.get(p.contract.conId), order_rows)
            held = self._close_reservations.get(p.contract.conId)
            status = ""
            if held:
                if self._close_reservation_proven(held, abs(float(p.position))):
                    self._close_reservations.pop(p.contract.conId, None)
                else:
                    status = "上一笔平仓待同步；可点击刷新持仓核对成交"
            if row["active_exit_orders"]:
                exits = "、".join(f'{order["order_type"]} #{order["order_id"]}' for order in row["active_exit_orders"])
                status = f"已有平仓挂单 {exits}，可在订单页查看。撤单回报确认后可重新平仓。"
            candidates = [t for t in trades if t.contract.conId == p.contract.conId
                          and t.order.account == self.account and not t.isDone()
                          and self._order_identity(t) not in getattr(self, "_missing_external_orders", set())]
            if self._covered_stops(candidates, p.position):
                reserved_here = not held or any(self._same_close_order(held[0], t) for t in candidates)
                row["can_add_with_stop"] = reserved_here
                if reserved_here and all(self._ours(t) for t in candidates):
                    row["protection_id"] = self._protection_ids(candidates)
                    status = f'保护止损 #{row["protection_id"]} · 快捷平仓会先撤此单；部分平仓后需为余仓重设止损。'
            row.update(close_pending=bool(status), close_status=status or "可预览平仓")
            result.append(row)
        return result

    def _close_detail(self, position):
        contract = copy.copy(position.contract)
        contract.exchange = "SMART"
        matches = [d for d in self.ib.reqContractDetails(contract)
                   if d.contract and d.contract.conId == contract.conId
                   and all(getattr(d.contract, field, None) == getattr(contract, field, None)
                           for field in ("symbol", "secType", "currency", "right", "strike", "multiplier"))]
        if len(matches) != 1:
            raise failure("无法唯一核对平仓合约及交易时段。")
        matches[0].contract.exchange = "SMART"
        detail = self._execution_detail(matches[0])
        self._details[contract.conId] = detail
        return detail

    def _cost_stop_order(self, conid, quantity):
        orders = [t for t in self._open_orders() if t.contract.conId == conid]
        if not orders:
            self._check_close_reservation(conid, quantity)
            return None
        trade = orders[0]
        o, status = trade.order, trade.orderStatus
        held = self._close_reservations.get(conid)
        if (len(orders) != 1 or not self._ours(trade) or trade.isDone()
                or (held and self._order_identity(held[0]) != self._order_identity(trade))
                or o.action != "SELL" or o.orderType != "STP" or self._short_order_role(o)
                or status.status not in ("PreSubmitted", "Submitted") or status.filled or trade.fills
                or status.remaining != quantity or o.totalQuantity != quantity
                or o.parentId or o.conditions or o.ocaGroup or o.algoStrategy or o.hedgeType
                or o.outsideRth or o.tif != "DAY" or not self._finite(o.auxPrice, positive=True)
                or o.orderId in self._amend_pending):
            raise failure("该合约已有未完成订单。仅可上调本工具已确认、覆盖全部持仓且未部分成交的独立 DAY 止损单，请在订单页核对。")
        return trade

    def _long_stop_position(self, conid):
        positions = [p for p in self._position_records(fresh=True) if p.contract.conId == int(conid)]
        if len(positions) != 1:
            raise failure("请选择有持仓的买方期权。")
        return positions[0]

    def _quote_cost_stop(self, p, detail, multiple):
        from exit_orders import cost_stop_quote
        if self._session(detail).kind != "regular":
            raise failure("成本止损使用 STP，仅可在该合约常规时段设置。")
        bid = self._quote(detail.contract, required_side="bid")["bid"]
        return cost_stop_quote(p.avgCost, p.contract.multiplier, p.position, bid, self._increments(detail), multiple)

    def prepare_cost_stop(self, conid, multiple=1):
        self._require()
        p = self._long_stop_position(conid)
        existing = self._cost_stop_order(int(conid), p.position)
        detail = self._amend_detail(existing.contract) if existing else self._close_detail(p)
        quote = self._quote_cost_stop(p, detail, multiple)
        if existing:
            if quote["stop_price"] <= existing.order.auxPrice:
                raise failure(f"原止损已在 {existing.order.auxPrice:g}，请选择更高的触发价。")
            quote.update(order_id=existing.order.orderId, old_stop=existing.order.auxPrice,
                         order_ref=existing.order.orderRef, perm_id=existing.order.permId)
        return dict(conid=int(conid), **quote)

    def set_breakeven_stop(self, conid):
        from exit_orders import build_exit_order
        self._require()
        conid = int(conid)
        position = self._long_stop_position(conid)
        orders = [t for t in self._open_orders() if t.contract.conId == conid]
        if orders:
            selected = self._protection_ids(orders)
            orders = self._selected_protections(conid, position.position, selected, orders=orders)
            identities = tuple(self._order_fingerprint(t) for t in orders)
        else:
            self._check_close_reservation(conid, position.position)
        detail = self._close_detail(position)
        guard = self._quote_cost_stop(position, detail, 1)
        if orders:
            first = orders[0]
            in_place = (len(orders) == 1 and first.order.orderType == "STP" and first.order.tif == "DAY"
                        and not first.order.outsideRth and first.orderStatus.remaining == guard["quantity"]
                        and first.order.auxPrice == guard["stop_price"])
            created = time.monotonic()
            if not in_place:
                # Check broker acceptance while the old stop can still protect the position.
                spec = dict(type="STP", tif="DAY", stop_price=guard["stop_price"])
                probe = build_exit_order(spec, guard["quantity"], self.account, self.ORDER_PREFIX + "PS:whatif")
                self._preflight_order(detail.contract, probe)
            current = self._long_stop_position(conid)
            trades = self._selected_protections(conid, current.position, selected)
            if ((current.position, current.avgCost) != (guard["quantity"], guard["avg_cost"])
                    or tuple(self._order_fingerprint(t) for t in trades) != identities):
                raise failure("持仓或保护单已变化，请刷新持仓。")
            if in_place:
                return dict(self._order_dict(trades[0]), stop_unchanged=True)
            latest = self._quote_cost_stop(current, detail, 1)
            if latest["stop_price"] != guard["stop_price"]:
                raise failure("持仓成本、数量或触发价已改变，请重新预览成本止损。")
            if tuple(self._order_fingerprint(t) for t in trades) != identities:
                raise failure("保护单或预览已变化，请重新预览。")
            try:
                position = self._cancel_position_stops(trades, conid, current.position, created)
            except Exception as exc:
                raise failure(f"新保本单尚未提交，请核对原止损状态。{exc}") from exc
            if position is None:
                return {"conid": conid, "closed_by_stop": True}
        try:
            if orders:
                guard = self._quote_cost_stop(position, detail, 1)
            spec = dict(type="STP", tif="DAY", stop_price=guard["stop_price"])
            return self._send(detail, "SELL", int(guard["quantity"]), exit_spec=spec, cost_guard=guard)
        except Exception as exc:
            if orders:
                raise failure(f"旧止损已结束，请核对新保本单状态和剩余持仓。{exc}") from exc
            raise

    def raise_cost_stop(self, cost_guard):
        from exit_orders import check_cost_stop
        guard = cost_guard
        conid = guard["conid"]

        def checked_order():
            quote = self.prepare_cost_stop(conid, guard["multiple"])
            if not quote.get("order_id") or any(guard.get(k) != quote.get(k)
                    for k in ("order_id", "old_stop", "order_ref", "perm_id")):
                raise failure("原止损单已变化或已结束，请刷新持仓。")
            check_cost_stop(guard, quote, dict(type="STP", stop_price=guard["stop_price"]), guard["quantity"])
            trade = self._managed_order(quote["order_id"], refresh=False)
            if (trade.isDone() or trade.orderStatus.status not in ("PreSubmitted", "Submitted")
                    or trade.orderStatus.filled or trade.fills or trade.orderStatus.remaining != guard["quantity"]
                    or trade.order.orderRef != guard["order_ref"] or trade.order.auxPrice != guard["old_stop"]):
                raise failure("止损单回报与预览不一致，请刷新后重试。")
            return trade

        trade = checked_order()
        fingerprint = self._order_fingerprint(trade)
        changed = copy.deepcopy(trade.order)
        changed.auxPrice = guard["stop_price"]
        self._whatif_amend(trade, changed)
        trade = checked_order()
        if self._order_fingerprint(trade) != fingerprint:
            raise failure("预检期间原止损单有变化，请重新预览。")
        return self._amend_price(trade, changed, "auxPrice")

    def _working_sell_stop(self, trade):
        order = getattr(trade, "order", None)
        if getattr(order, "orderType", None) not in STOP_TYPES:
            return False
        status = trade.orderStatus
        if (order.account != self.account or order.action != "SELL" or self._short_order_role(order)
                or trade.contract.secType != "OPT" or trade.isDone()
                or status.status not in ("PreSubmitted", "Submitted")
                or order.conditions or order.parentId or order.ocaGroup or order.algoStrategy or order.hedgeType
                or (self._ours(trade) and order.orderId in self._amend_pending)):
            return False
        quantities = (order.totalQuantity, status.filled, status.remaining)
        return (all(self._finite(q) and float(q).is_integer() for q in quantities)
                and status.filled >= 0 and status.remaining > 0 and status.filled + status.remaining == order.totalQuantity)

    def _covered_stops(self, trades, position):
        return (bool(trades) and position > 0 and all(self._working_sell_stop(t) for t in trades)
                and sum(t.orderStatus.remaining for t in trades) <= position)

    @staticmethod
    def _protection_ids(trades):
        ids = sorted(t.order.orderId for t in trades)
        return ids[0] if len(ids) == 1 else tuple(ids)

    def _selected_protections(self, conid, position, selected, *, orders=None):
        if orders is None:
            orders = [t for t in self._open_orders() if t.contract.conId == conid]
        held = self._close_reservations.get(conid)
        if (not self._covered_stops(orders, position) or not all(self._ours(t) for t in orders)
                or self._protection_ids(orders) != selected
                or (held and held[0].orderStatus.filled and position > self._reservation_size(held) - held[0].orderStatus.filled)
                or (held and not any(self._same_close_order(held[0], t) for t in orders))):
            raise failure("所选保护止损已变化，请刷新持仓。")
        return sorted(orders, key=lambda t: t.order.orderId)

    def preview_close(self, conid, percent=100, order_mode="MKT", limit_price=None, protection_id=None):
        self._require()
        conid = int(conid)
        records = [p for p in self._position_records(fresh=True, include_short=True) if p.contract.conId == conid]
        if protection_id is None:
            self._check_close_reservation(conid, abs(float(records[0].position)) if len(records) == 1 else 0)
        # Reconciliation may have downloaded a newer position. Read it again
        # rather than deriving a second close from the pre-reconciliation size.
        records = [p for p in self._position_records(fresh=True, include_short=True) if p.contract.conId == conid]
        if len(records) != 1:
            raise failure("当前账户中没有该标准美元期权持仓，请刷新持仓。")
        protection = None
        if protection_id is not None:
            trades = self._selected_protections(conid, records[0].position, protection_id)
            protection = tuple(self._order_fingerprint(t) for t in trades)
        elif any(t.contract.conId == conid for t in self._open_orders()):
            raise failure("该合约已有未完成订单；请到订单页查看/改单或等待撤单确认。")
        p = records[0]
        quantity = close_quantity(abs(p.position), percent)
        detail = self._close_detail(p)
        action = "SELL" if p.position > 0 else "BUY"
        side = "bid" if action == "SELL" else "ask"
        quote = self._quote(detail.contract, required_side=side if order_mode == "MKT" else None)
        settings = {"order_mode": order_mode, "limit_price": limit_price}
        plan = self._execution_plan(detail, action, quote, settings)
        price = plan["limit_price"] if plan["order_type"] == "LMT" else quote["bid" if action == "SELL" else "ask"]
        premium = price * quantity * float(detail.contract.multiplier)
        fee = max(1., float(quantity))
        token = uuid.uuid4().hex
        value = {**plan, "token": token, "contract": self._contract_dict(detail.contract),
                 "action": action, "quantity": quantity, "position_quantity": float(p.position),
                 "bid": quote["bid"], "ask": quote["ask"], "fee_reserve": fee,
                 "estimated_premium": premium, "estimated_proceeds": premium - fee if action == "SELL" else -premium - fee,
                 "avg_option_price": abs(float(p.avgCost)) / float(detail.contract.multiplier),
                 "estimated_pnl": (price * float(detail.contract.multiplier) - abs(float(p.avgCost)))
                                  * quantity * (1 if p.position > 0 else -1) - fee,
                 "estimated_cost": premium + fee if action == "BUY" else fee,
                 "created_at": time.time(), "mode": self.mode, "account": self.account,
                 "close_intent": True, "expected_position": float(p.position)}
        if protection is not None:
            value.update(protection_id=protection_id, protection_identity=protection)
        tokens = getattr(self, "_close_tokens", {})
        self._close_tokens = {key: saved for key, saved in tokens.items() if time.monotonic() - saved[2] <= self.TOKEN_TTL}
        self._close_tokens[token] = (copy.deepcopy(value), detail, time.monotonic())
        return value

    def close_confirm(self, token):
        self._require()
        saved = getattr(self, "_close_tokens", {}).pop(token, None)
        if saved is None:
            raise failure("平仓预览已失效或已使用，请重新预览。")
        preview, detail, created = saved
        if time.monotonic() - created > self.TOKEN_TTL:
            raise failure("平仓预览已超过有效期，请重新预览；未发单。")
        conid = detail.contract.conId
        positions = [p for p in self._position_records(fresh=True, include_short=True) if p.contract.conId == conid]
        if len(positions) != 1 or float(positions[0].position) != preview["position_quantity"]:
            raise failure("预览后持仓数量或方向已变化，请重新预览；未发单。")
        protection_id = preview.get("protection_id")
        if protection_id is not None:
            self._verify_short_plan(detail, preview)
            self._quote(detail.contract, required_side="bid" if preview.get("price_mode") == "MKT" else None)
            trades = self._selected_protections(conid, positions[0].position, protection_id)
            if (tuple(self._order_fingerprint(t) for t in trades) != preview["protection_identity"]
                    or time.monotonic() - created >= self.TOKEN_TTL):
                raise failure("保护单或预览已变化，请重新预览。")
            try:
                remaining = self._cancel_position_stops(trades, conid, preview["position_quantity"], created)
                size = remaining.position if remaining is not None else 0
                filled = int(preview["position_quantity"] - size)
                preview["filled_by_stop"] = filled
                preview["quantity"] = max(0, preview["quantity"] - filled)
                preview["position_quantity"] = preview["expected_position"] = size
                if not preview["quantity"]:
                    self.log("止损成交已达到本次平仓张数。")
                    return {"closed_by_stop": True, "status": "Filled", "conid": conid,
                            "protection_id": protection_id, "filled_by_stop": preview["filled_by_stop"]}
                self.log(f"保护止损 #{protection_id} 已确认结束，已复核剩余持仓。")
                return self._finish_preview_close(conid, preview, detail, created)
            except Exception as exc:
                raise failure(f"请核对保护止损 #{protection_id} 的状态、平仓委托和剩余持仓。{exc}") from exc
        return self._finish_preview_close(conid, preview, detail, created)

    def _cancel_position_stops(self, trades, conid, position_quantity, created):
        # Freeze quantities and order terms before any cancel yields to IB events.
        original = [(trade, repr(trade.order), trade.orderStatus.filled) for trade in trades]
        filled = 0
        for trade, terms, before in original:
            self._require()
            if not trade.isDone():
                if (repr(trade.order) != terms or trade.order.account != self.account or not self._ours(trade)
                        or trade.contract.conId != conid or time.monotonic() >= created + self.TOKEN_TTL):
                    raise failure("保护单或预览已变化，请重新预览。")
                try:
                    self.ib.cancelOrder(trade.order)
                except Exception as exc:
                    self._uncertain = True
                    raise failure("撤保护单结果未确认，已停止平仓，请在 TWS 核对。") from exc
            left = self._wait_cancelled(trade, verify_protective_exit=True)
            delta = trade.order.totalQuantity - left - before
            if delta < 0:
                raise failure("止损成交数量已修订，请刷新持仓。")
            filled += int(delta)

        expected = position_quantity - filled
        # Position updates can lag terminal status. Only an exact holding authorizes the next order.
        expires = min(created + self.TOKEN_TTL, time.monotonic() + self.CANCEL_WAIT_SECONDS)
        while True:
            records = [p for p in self._position_records(fresh=True, include_short=True) if p.contract.conId == conid]
            if len(records) <= 1 and (records[0].position if records else 0) == expected and expected >= 0:
                break
            if not filled or time.monotonic() >= expires:
                raise failure("撤单后持仓与止损成交或部分成交记录不符，未提交新平仓单。")
            self.ib.sleep(.2)
        held = self._close_reservations.get(conid)
        if held and any(self._same_close_order(held[0], t) for t in trades):
            self._close_reservations.pop(conid)
        return records[0] if expected else None

    def _finish_preview_close(self, conid, preview, detail, created):
        if preview.get("protection_id") is not None:
            positions = [p for p in self._position_records(fresh=True, include_short=True) if p.contract.conId == conid]
            if len(positions) != 1 or positions[0].position != preview["position_quantity"]:
                raise failure("撤单后持仓数量或方向变化，未提交新平仓单。")
        self._check_close_reservation(conid, abs(preview["position_quantity"]))
        if any(t.contract.conId == conid for t in self._open_orders()):
            raise failure("该合约已有未完成订单，请到订单页核对；未重复平仓。")
        self._verify_short_plan(detail, preview)
        side = "bid" if preview["action"] == "SELL" else "ask"
        remaining = max(0., created + self.TOKEN_TTL - time.monotonic())
        self._quote(detail.contract, wait=min(3., remaining),
                    required_side=side if preview.get("price_mode") == "MKT" else None)
        if preview["action"] == "BUY":
            return self._send_short_cover(detail, preview["quantity"], plan=preview, deadline=created + self.TOKEN_TTL)
        return self._send(detail, "SELL", preview["quantity"], plan=preview, deadline=created + self.TOKEN_TTL)

    def _record_buy_against_stop(self, buy):
        conid = buy.contract.conId
        held = self._close_reservations.get(conid)
        if held and held[0].order.orderType in STOP_TYPES and held[0].order.action == "SELL":
            prior = held.buys if isinstance(held, StopWithAdds) else ()
            self._close_reservations[conid] = StopWithAdds(held[0], held[1], (*prior, buy))

    def _reservation_size(self, held):
        baseline = held[1]
        if isinstance(held, StopWithAdds):
            for buy in held.buys:
                fills = [f for f in buy.fills if self._matching_close_fill(buy, f) and f.execution.execId]
                executed = sum(float(f.execution.shares) for f in self._latest_close_fills(fills).values())
                if 0 < executed <= buy.order.totalQuantity and executed == buy.orderStatus.filled:
                    baseline += executed
        return baseline

    def _close_reservation_proven(self, held, observed):
        trade, baseline = held[0], self._reservation_size(held)
        reported = float(trade.orderStatus.filled)
        executed = sum(float(f.execution.shares) for f in trade.fills)
        terminal = trade.isDone()
        empty = terminal and trade.orderStatus.status in ("Cancelled", "ApiCancelled") and reported == executed == 0
        complete = terminal and executed > 0 and abs(executed - reported) < 1e-9
        return empty or (complete and observed <= baseline - executed + 1e-9)

    def _same_close_order(self, left, right):
        a, b = left.order, right.order
        if (a.account != self.account or b.account != self.account or left.contract.conId != right.contract.conId
                or a.action != b.action):
            return False
        perm_a, perm_b = getattr(a, "permId", 0), getattr(b, "permId", 0)
        if isinstance(perm_a, int) and perm_a > 0 and isinstance(perm_b, int) and perm_b > 0:
            return perm_a == perm_b
        return (getattr(a, "clientId", None), getattr(a, "orderId", None), getattr(a, "orderRef", "")) == (
            getattr(b, "clientId", None), getattr(b, "orderId", None), getattr(b, "orderRef", "")) and bool(getattr(a, "orderRef", ""))

    @staticmethod
    def _close_order_key(trade):
        order = trade.order
        return (order.account, getattr(order, "clientId", None), getattr(order, "orderId", None),
                getattr(order, "orderRef", ""), trade.contract.conId)

    def _inactive_close_reconciled(self, trade):
        if getattr(getattr(trade, "orderStatus", None), "status", None) != "Inactive":
            return False
        evidence = getattr(self, "_order_rejections", {}).get(self._close_order_key(trade), {})
        return evidence.get("reconciled") is True

    def _matching_close_fill(self, trade, fill):
        order, execution = trade.order, fill.execution
        if (getattr(execution, "acctNumber", "") != self.account
                or fill.contract.conId != trade.contract.conId
                or str(execution.side).upper() not in ({"SLD", "SELL"} if order.action == "SELL" else {"BOT", "BUY"})):
            return False
        perm, other = getattr(order, "permId", 0), getattr(execution, "permId", 0)
        if isinstance(perm, int) and perm > 0 and isinstance(other, int) and other > 0:
            return perm == other
        return (getattr(order, "clientId", None), getattr(order, "orderId", None)) == (
            getattr(execution, "clientId", None), getattr(execution, "orderId", None)) and bool(getattr(execution, "execId", ""))

    @staticmethod
    def _latest_close_fills(fills):
        # IB replaces the final numeric revision of an execution ID for corrections.
        revisions = {}
        for fill in fills:
            identifier = fill.execution.execId
            prefix, separator, suffix = identifier.rpartition(".")
            key, revision = (prefix, int(suffix)) if separator and suffix.isdigit() else (identifier, -1)
            if key not in revisions or revision > revisions[key][0]:
                revisions[key] = revision, fill
        return {fill.execution.execId: fill for _, fill in revisions.values()}

    def _refresh_close_reservations(self, force=False, positions=None):
        if not self._close_reservations:
            return
        now = time.monotonic()
        if not force and now - getattr(self, "_close_sync_at", -1e9) < self.CLOSE_SYNC_INTERVAL:
            return
        self._close_sync_at = now
        try:
            if positions is None:
                positions = self._position_records(fresh=True, include_short=True)
            server_open = self._download_open_orders()
            fills = self._history_backfill()
            completed = self.ib.reqCompletedOrders(apiOnly=False)
            # Never treat a failed/unknown response as an empty server list.
            if not all(isinstance(items, (list, tuple)) for items in (server_open, fills, completed)):
                return
        except Exception as exc:
            self.log(f"平仓同步暂未完成（{type(exc).__name__}）；保留防重复平仓保护。")
            return
        local = self.ib.trades()
        local = local if isinstance(local, (list, tuple)) else []
        cached_fills = self.ib.fills()
        cached_fills = cached_fills if isinstance(cached_fills, (list, tuple)) else []
        for conid, held in list(self._close_reservations.items()):
            trade, baseline = held[0], self._reservation_size(held)
            if any(self._same_close_order(trade, candidate) and not candidate.isDone() for candidate in server_open):
                continue
            matches = [candidate for candidate in [trade, *local, *completed] if self._same_close_order(trade, candidate)]
            rejected = getattr(self, "_order_rejections", {}).get(self._close_order_key(trade), {})
            terminal = [candidate for candidate in matches if candidate.orderStatus.status in ("Filled", "Cancelled", "ApiCancelled")
                        or (candidate.orderStatus.status == "Inactive" and rejected.get("code") in (201, 202))]
            if not terminal:
                continue
            evidence = [*fills, *cached_fills, *(fill for candidate in matches for fill in candidate.fills)]
            unique = {}
            for fill in evidence:
                if self._matching_close_fill(trade, fill):
                    exec_id = getattr(fill.execution, "execId", "")
                    if exec_id:
                        previous = unique.get(exec_id)
                        previous_report = getattr(previous, "commissionReport", None)
                        if previous is None or not getattr(previous_report, "execId", ""):
                            unique[exec_id] = fill
            unique = self._latest_close_fills(unique.values())
            executed = sum(float(fill.execution.shares) for fill in unique.values())
            if not math.isfinite(executed) or executed < 0:
                continue
            total = float(trade.order.totalQuantity)
            status = "Filled" if any(t.orderStatus.status == "Filled" for t in terminal) else terminal[-1].orderStatus.status
            reported_values = [float(t.orderStatus.filled) for t in matches]
            # Completed-order records expose filledQuantity even when their
            # newly-created OrderStatus still contains a default filled=0.
            for candidate in terminal:
                value = getattr(candidate.order, "filledQuantity", None)
                if self._finite(value) and float(value) < 1e100:
                    reported_values.append(float(value))
            reported = max(reported_values)
            if status == "Filled":
                reported = max(reported, total)
            elif reported < executed and executed > 0:
                cumulative = [float(getattr(fill.execution, "cumQty", 0)) for fill in unique.values()]
                if not cumulative or abs(max(cumulative) - executed) > 1e-9:
                    continue
                reported = executed
            if executed != reported or executed > total:
                continue
            observed = [p for p in positions if p.contract.conId == conid]
            if len(observed) > 1:
                continue
            position = float(observed[0].position) if observed else 0.
            if position and (position > 0) != (trade.order.action == "SELL"):
                continue
            if abs(position) > baseline - executed + 1e-9:
                continue
            if status == "Inactive" and (executed != 0 or reported != 0 or abs(position) != baseline):
                continue
            for candidate in matches:
                candidate.fills = list(unique.values())
                candidate.orderStatus.status = status
                candidate.orderStatus.filled = reported
                candidate.orderStatus.remaining = max(0., total - reported)
            self._close_reservations.pop(conid, None)
            if status == "Inactive":
                rejected["reconciled"] = True
            self.log("已核对上一笔平仓的订单终态、成交明细及实际持仓，解除该合约重复平仓保护。")

    def refresh_positions(self):
        self._require()
        positions = self._position_records(fresh=True, include_short=True)
        if self._close_reservations:
            self._refresh_close_reservations(force=True, positions=positions)
        else:
            self._download_open_orders()
            self._history_backfill()
        return self.snapshot()
