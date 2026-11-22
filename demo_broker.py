"""In-memory demo ledger with synthetic data. It never connects to IBKR."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import time
import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from core import (PREVIEW_TTL_SECONDS, SUPPORTED_SYMBOLS, ValidationError,
                  close_quantity, decimal_value, normalize_symbol, select_strike, size_order,
                  select_settings_strike, opening_quantity)
from core import affordable_size, budget_strikes
from market_data import chain_page
from entry_orders import build_entry_plan, resolve_entry_price, limit_contract_changed
from position_analytics import enrich_position
from exit_orders import STOP_TYPES


def _new_york_date(instant=None):
    """New York calendar date, including a stdlib-only Windows fallback.

    The fallback applies the US Eastern DST rule in effect since 2007: from
    the second Sunday in March at 07:00 UTC to the first Sunday in November
    at 06:00 UTC. It only serves synthetic demo expirations, not trade hours.
    """
    utc = (instant or datetime.now(timezone.utc)).astimezone(timezone.utc)
    try:
        return utc.astimezone(ZoneInfo("America/New_York")).date()
    except ZoneInfoNotFoundError:
        march_first = datetime(utc.year, 3, 1, 7, tzinfo=timezone.utc)
        november_first = datetime(utc.year, 11, 1, 6, tzinfo=timezone.utc)
        dst_start = march_first + timedelta(days=(6 - march_first.weekday()) % 7 + 7)
        dst_end = november_first + timedelta(days=(6 - november_first.weekday()) % 7)
        offset = -4 if dst_start <= utc < dst_end else -5
        return utc.astimezone(timezone(timedelta(hours=offset))).date()


class DemoBroker:
    def __init__(self, log=lambda message: None, history_path=":memory:"):
        self.log = log
        self.mode = "DEMO"
        self.account = "DEMO-1000"
        self.connected = False
        self._cash = Decimal("1000")
        self._positions = {}
        self._orders = []
        self._pending_exits = {}
        self._pending_entries = {}
        self._pending_covers = {}
        self._close_previews = {}
        self._modify_previews = {}
        self._previews = {}
        self._generation = 0
        from trade_history import TradeHistoryStore
        self._history = TradeHistoryStore(history_path or ":memory:")
        self._history_session = uuid.uuid4().hex
        self._history_sequence = 0
        self._history_pending = []
        self._history_error = ""

    def connect(self, mode="DEMO", host="", port=0, client_id=0,
                account="", live_ack=""):
        if mode != "DEMO":
            raise ValidationError("离线演示只能使用 DEMO 模式。")
        self.connected = True
        self._previews.clear()
        self._modify_previews.clear()
        self._close_previews.clear()
        self.log("离线演示已连接：$1,000 初始现金，全部价格和成交均为本地模拟。")
        return self.snapshot()

    def disconnect(self):
        self.connected = False
        self._previews.clear()
        self._modify_previews.clear()
        self._close_previews.clear()

    def pump(self):
        if self.connected:
            for state in list(self._pending_entries.values()):
                self._advance_entry(state)
            for state in list(self._pending_exits.values()):
                self._advance_exit(state)
            for state in list(self._pending_covers.values()):
                self._advance_cover(state)

    def _available_cash(self):
        reserved = sum((s["reserved"] for s in self._pending_entries.values()), Decimal(0))
        reserved += sum((s["reserved"] for s in self._pending_covers.values()), Decimal(0))
        margin = sum((decimal_value(p.get("demo_margin", 0)) for p in self._positions.values()), Decimal(0))
        return max(Decimal(0), self._cash - reserved - margin)

    def _require_connected(self):
        if not self.connected:
            raise ValidationError("请先连接离线演示。")

    def expirations(self, symbol):
        self._require_connected()
        normalize_symbol(symbol)
        current = _new_york_date()
        expirations = []
        for offset in range(12):
            candidate = current + timedelta(days=offset)
            if candidate.weekday() < 5:
                expirations.append(candidate.strftime("%Y%m%d"))
            if len(expirations) == 7:
                break
        return expirations

    @staticmethod
    def _spot(symbol="SPY"):
        return Decimal({"SPX": "6004", "QQQ": "500.4", "SPY": "600.4", "XSP": "600.4"}.get(symbol, "100.4"))

    @classmethod
    def _strikes(cls, symbol):
        step = 5 if symbol == "SPX" else 1
        center = int(cls._spot(symbol)) // step * step
        return range(max(step, center - 50 * step), center + 52 * step, step)

    @classmethod
    def _quote(cls, strike, right, symbol="SPY"):
        spot = cls._spot(symbol)
        k = Decimal(str(strike))
        intrinsic = max(Decimal(0), spot - k if right == "C" else k - spot)
        distance = abs(k - spot)
        extrinsic = max(Decimal("0.05"), Decimal("0.75") - Decimal("0.15") * distance)
        ask = (intrinsic + extrinsic).quantize(Decimal("0.01"))
        bid = max(Decimal("0.01"), ask - Decimal("0.02"))
        return spot, bid, ask

    def trading_classes(self, symbol, expiry):
        symbol = normalize_symbol(symbol)
        if expiry not in self.expirations(symbol):
            raise ValidationError("请选择有效的演示到期日。")
        return ["SPXW", "SPX"] if symbol == "SPX" else [symbol.replace(" ", "")]

    def _contract(self, symbol, expiry, right, strike, trading_class=""):
        classes = self.trading_classes(symbol, expiry)
        trading_class = trading_class or classes[0]
        if trading_class not in classes:
            raise ValidationError("所选类别不属于演示标的与到期日。")
        local_symbol = f"{trading_class} {expiry} {right}{strike:g} [演示]"
        conid = int.from_bytes(hashlib.sha256(local_symbol.encode()).digest()[:4], "big")
        return {"conid": conid, "symbol": symbol, "expiry": expiry,
                "right": right, "strike": float(strike), "multiplier": 100,
                "trading_class": trading_class, "local_symbol": local_symbol}

    def chain(self, symbol, expiry, trading_class="", offset=0, limit=40, reference_price=None, visible_strikes=None):
        self._require_connected()
        symbol = normalize_symbol(symbol)
        classes = self.trading_classes(symbol, expiry)
        trading_class = trading_class or classes[0]
        if trading_class not in classes:
            raise ValidationError("所选类别不属于演示标的与到期日。")
        spot = float(self._spot(symbol))
        strikes, offset, has_prev, has_next = chain_page(self._strikes(symbol), spot, offset, limit)
        rows = []
        for strike in strikes:
            row = {"strike": strike}
            for right, side in (("C", "call"), ("P", "put")):
                contract = self._contract(symbol, expiry, right, strike, trading_class)
                _, bid, ask = self._quote(strike, right, symbol)
                row[side] = {"conid": contract["conid"], "local_symbol": contract["local_symbol"],
                             "contract": deepcopy(contract), "right": right, "symbol": symbol,
                             "expiry": expiry, "trading_class": trading_class, "strike": strike, "multiplier": 100,
                             "bid": float(bid), "ask": float(ask), "last": float((bid + ask) / 2),
                             "quote_type": "合成演示", "age": 0.0,
                             "bid_age": 0.0, "ask_age": 0.0, "tick_age": 0.0}
            rows.append(row)
        return {"symbol": symbol, "expiry": expiry, "trading_class": trading_class,
                "classes": classes, "spot": spot, "spot_kind": "合成演示",
                "rows": rows, "offset": offset, "has_prev": has_prev,
                "has_next": has_next, "updated_at": time.time(), "mode": self.mode}

    def preview_pair(self, settings):
        result = {}
        for right in ("C", "P"):
            try:
                result[right] = self.preview({**deepcopy(settings), "right": right})
            except (ValueError, RuntimeError) as exc:
                result[right] = {"error": str(exc)}
        return result

    def _check_open_position(self, contract, action):
        current = self._positions.get(contract["conid"])
        if current and current["quantity"] * (1 if action == "BUY" else -1) < 0:
            raise ValidationError("该合约已有相反方向持仓；请在持仓页先平仓，开仓按钮不会混作平仓。")

    def _seller_size(self, settings, price):
        quantity = opening_quantity(settings)
        available = self._available_cash()
        budget_mode = settings.get("budget_mode", "percent")
        if budget_mode == "amount":
            requested = decimal_value(settings.get("cash_amount"), "指定美元金额")
            if requested <= 0:
                raise ValidationError("指定美元金额必须大于 0。")
        elif budget_mode == "percent":
            percent = decimal_value(settings.get("percent", 50), "现金比例")
            if not 0 < percent <= 100:
                raise ValidationError("现金比例必须大于 0 且不超过 100%。")
            requested = available * percent / 100
        else:
            raise ValidationError("预算方式必须为 percent 或 amount。")
        budget = min(available, requested)
        per_fee = decimal_value(settings.get("commission_per_contract", 1), "每张手续费预留")
        minimum = decimal_value(settings.get("min_commission", 1), "最低手续费预留")
        if min(per_fee, minimum) < 0:
            raise ValidationError("手续费预留不能为负数。")
        fees = max(minimum, quantity * per_fee)
        credit = decimal_value(price) * 100 * quantity
        # Synthetic rehearsal only: no claim to approximate IBKR risk margins.
        margin = Decimal(500) * quantity
        if margin + fees > budget:
            raise ValidationError(f"离线演示卖方保证金预算不足：按固定 $500/张，{quantity} 张加费用需 ${margin + fees:.2f}，预算 ${budget:.2f}；非 IBKR 保证金。")
        return dict(quantity=quantity, raw_quantity=float(quantity), rounding_mode="manual",
                    budget=float(budget), budget_mode=budget_mode, requested_budget=float(requested),
                    estimated_premium=float(credit), estimated_credit=float(credit),
                    estimated_net_credit=float(credit - fees), fee_reserve=float(fees),
                    initial_margin_change=float(margin), maintenance_margin_change=float(margin),
                    margin_source="离线演示固定 $500/张，非 IBKR 保证金", buffered_cost=float(margin + fees),
                    actual_percent=float((margin + fees) / available * 100) if available else 0,
                    budget_difference=float(margin + fees - budget), unit_cost=500 + float(per_fee),
                    margin_sufficient=margin + fees <= budget)

    def _entry_blocked(self, conid):
        if self._pending_entries or self._pending_covers:
            return True
        covered = {}
        for state in self._pending_exits.values():
            cid = state["conid"]
            if state["spec"]["type"] not in STOP_TYPES:
                return True
            covered[cid] = covered.get(cid, 0) + state["order"]["remaining"]
        return any(self._positions.get(cid, {}).get("quantity", 0) < qty for cid, qty in covered.items())

    def preview_add(self, conid, expected, request):
        from position_add import add_settings
        self._require_connected()
        p = self._positions.get(conid)
        if not expected or not p or p["quantity"] != expected:
            raise ValidationError("持仓数量或方向已变化，请重新选择持仓。")
        if self._entry_blocked(conid):
            raise ValidationError("当前有未完成的演示订单，请先核对订单。")
        basis = None
        if request["method"] == "principal" and expected > 0 and not request.get("principal"):
            basis = self._history.position_opening_cost(self.account, self.mode, conid, expected, self._history_session)
        settings, info = add_settings(request, expected, basis)
        settings.update({key: p[key] for key in ("symbol", "right", "expiry", "trading_class", "strike", "conid")})
        settings.update(selection_mode="strike")
        return self._preview_contract(settings, info)

    def preview(self, settings):
        return self._preview_contract(settings)

    def _preview_contract(self, settings, add_info=None):
        from position_add import check_add_size, short_budget_lots
        self._require_connected()
        settings = deepcopy(settings)
        symbol = normalize_symbol(settings.get("symbol", "SPY"))
        if settings.get("expiry") not in self.expirations(symbol):
            raise ValidationError("请选择有效的演示到期日。")
        right = settings.get("right", "C")
        action = settings.get("open_action", "BUY")
        if action not in ("BUY", "SELL"):
            raise ValidationError("开仓方式必须为 BUY 或 SELL。")
        rank = None
        if settings.get("selection_mode") == "budget":
            for rank, strike in budget_strikes(self._strikes(symbol), self._spot(symbol), right, settings):
                _, _, ask = self._quote(strike, right, symbol)
                candidate = self._contract(symbol, settings["expiry"], right, strike, settings.get("trading_class", ""))
                _, limit, _ = resolve_entry_price(settings, right, candidate["conid"])
                if affordable_size(self._available_cash(), ask, 100, settings, limit)["quantity"]:
                    break
            else:
                raise ValidationError("所选预算不足以购买任一有效虚值档的一张合约（含费）。")
        else:
            strike = select_settings_strike(self._strikes(symbol), self._spot(symbol), right, settings)
        spot, bid, ask = self._quote(strike, right, symbol)
        expiry = settings["expiry"]
        contract = self._contract(symbol, expiry, right, strike, settings.get("trading_class", ""))
        conids = settings.get("conids")
        expected_id = conids.get(right) if isinstance(conids, dict) else settings.get("conid")
        if expected_id is not None and (isinstance(expected_id, bool) or str(expected_id) != str(contract["conid"])):
            raise ValidationError("选中合约编号与标的、到期日、类别、方向或行权价不一致，请重新选合约。")
        self._check_open_position(contract, action)
        order_mode, limit_price, auto_limit = resolve_entry_price(settings, right, contract["conid"])
        plan = build_entry_plan(action, order_mode,
                                {"bid": bid, "ask": ask}, [(0, .01)],
                                limit_price=limit_price, auto_limit=auto_limit,
                                slippage_pct=settings.get("slippage_pct", 5))
        if plan.get("limit_seeded"):
            plan["limit_reseeded"] = limit_contract_changed(settings, right, contract["conid"])
        if add_info and action == "SELL" and settings["budget_mode"] == "amount":
            one = self._seller_size({**settings, "seller_quantity": 1}, plan["sizing_price"])
            settings["seller_quantity"] = short_budget_lots(settings, one)
        sizing = self._seller_size(settings, plan["sizing_price"]) if action == "SELL" else size_order(self._available_cash(), settings.get("percent", 50), plan["sizing_price"], 100,
                            plan["reserve_slippage_pct"],
                            settings.get("commission_per_contract", 1),
                            settings.get("min_commission", 1),
                            settings.get("max_contracts", 10000),
                            settings.get("rounding_mode", "nearest"),
                            budget_mode=settings.get("budget_mode", "percent"), cash_amount=settings.get("cash_amount"))
        if add_info:
            check_add_size(sizing, add_info)
            plan.update(add_info)
        preview = {"token": uuid.uuid4().hex, "contract": contract, "spot": float(spot),
                   "bid": float(bid), "ask": float(ask), "quote_type": "SYNTHETIC / 离线合成价格",
                   "cash": float(self._available_cash()), "created_at": time.time(),
                   "mode": self.mode, "account": self.account, "action": action,
                   "opens_short": action == "SELL", **sizing}
        # Background preview must not invalidate a click that is already queued.
        self._previews = {k: v for k, v in self._previews.items()
                          if time.time() - v[0]["created_at"] <= PREVIEW_TTL_SECONDS}
        if rank is not None:
            preview.update(affordable_size(self._available_cash(), ask, 100, settings, plan.get("limit_price")), budget_rank=rank)
        preview.update(**plan, session_label="离线演示",
                       spot_source="合成演示", spot_estimated=False)
        self._previews[preview["token"]] = (deepcopy(preview), settings, self._generation)
        return deepcopy(preview)

    def buy(self, token):
        self._require_connected()
        stored = self._previews.pop(token, None)
        if stored is None:
            raise ValidationError("预览已使用或已失效，请重新预览。")
        preview, settings, generation = stored
        if time.time() - preview["created_at"] > PREVIEW_TTL_SECONDS:
            raise ValidationError("预览超过 10 秒，请刷新后再下单。")
        if generation != self._generation:
            raise ValidationError("现金或持仓已改变，请重新预览。")
        if self._entry_blocked(preview["contract"]["conid"]):
            raise ValidationError("当前有未完成的演示订单，请先等待成交或撤单，再开新仓。")
        quantity = preview["quantity"]
        if quantity < 1:
            raise ValidationError("所选现金比例不足以购买 1 张。")
        contract = preview["contract"]
        action = preview.get("action", "BUY")
        if preview.get("add_method") and self._positions.get(contract["conid"], {}).get("quantity") != preview["position_quantity"]:
            raise ValidationError("持仓已变化，请重新预览加仓。")
        self._check_open_position(contract, action)
        reserved = decimal_value(preview["buffered_cost"])
        if reserved > self._available_cash():
            raise ValidationError("演示现金不足，请重新预览。")
        order = {"order_id": len(self._orders) + 1, "action": action, "quantity": quantity,
                 "filled": 0, "remaining": quantity, "status": "Submitted (离线演示)",
                 "local_symbol": contract["local_symbol"], "avg_fill_price": 0.0,
                 "conid": contract["conid"], "order_type": preview["order_type"],
                 "limit_price": preview["limit_price"], "price_mode": preview["price_mode"],
                 "stop_price": None, "trail_mode": None, "trail_value": None, "limit_offset": None,
                 "tif": "DAY", "outside_rth": False, "modifiable": action == "BUY", "owned": True,
                 "opens_short": action == "SELL"}
        state = {"order": order, "contract": deepcopy(contract), "conid": contract["conid"],
                 "reserved": reserved, "fees": decimal_value(preview["fee_reserve"]),
                 "slippage_pct": settings.get("slippage_pct", 5),
                 "commission_per_contract": settings.get("commission_per_contract", 1),
                 "min_commission": settings.get("min_commission", 1),
                 "margin": Decimal(500) * quantity if action == "SELL" else Decimal(0),
                 "cash_ceiling": (decimal_value(preview["budget"]) if preview.get("budget_mode") == "amount"
                                  or settings.get("selection_mode") == "budget" else None),
                 "created_day": _new_york_date()}
        self._register_order(order, contract)
        self._pending_entries[order["order_id"]] = state
        self._generation += 1
        self._previews.clear()
        self._advance_entry(state)
        if order["remaining"]:
            self.log(f"[离线演示] {order['price_mode']} {action} 开仓单 {quantity} 张等待盘口达到限价；预留 ${reserved:.2f}。")
        return deepcopy(order)

    def _cancel_entry(self, state, reason="已撤单"):
        order = state["order"]
        order.update(status="Cancelled (离线演示)", cancel_reason=reason, modifiable=False)
        self._pending_entries.pop(order["order_id"], None)
        self._generation += 1
        self._previews.clear()
        self.log(f"[离线演示] 开仓单 {order['order_id']} {reason}，已释放预留现金。")

    def _advance_entry(self, state):
        order, contract = state["order"], state["contract"]
        if order["order_id"] not in self._pending_entries:
            return
        if order["tif"] == "DAY" and _new_york_date() != state["created_day"]:
            self._cancel_entry(state, "DAY 纽约日期已切换")
            return
        if order.get("opens_short"):
            self._advance_short_entry(state)
            return
        _, _, raw_ask = self._quote(contract["strike"], contract["right"], contract["symbol"])
        try:
            ask = decimal_value(raw_ask)
        except ValidationError:
            return
        if ask <= 0 or (order["order_type"] == "LMT" and ask > decimal_value(order["limit_price"])):
            return
        quantity = order["remaining"]
        cost = ask * contract["multiplier"] * quantity + state["fees"]
        if state.get("cash_ceiling") is not None and cost > state["cash_ceiling"]:
            self._cancel_entry(state, "最新成交价超出指定美元预算")
            return
        if cost > self._available_cash() + state["reserved"]:
            self._cancel_entry(state, "最新成交价使总现金不足")
            return
        self._cash -= cost
        old = self._positions.get(contract["conid"])
        old_quantity = old["quantity"] if old else 0
        old_cost = Decimal(str(old["avg_cost"])) * old_quantity if old else Decimal(0)
        new_quantity = old_quantity + quantity
        self._positions[contract["conid"]] = {
            **contract, "quantity": new_quantity, "avg_cost": float((old_cost + cost) / new_quantity)}
        order.update(status="Filled (离线演示)", filled=order["filled"] + quantity,
                     remaining=0, avg_fill_price=float(ask), modifiable=False)
        self._pending_entries.pop(order["order_id"], None)
        self._generation += 1
        self._previews.clear()
        self._record_fill(order, contract, quantity, ask, state["fees"], Decimal(0))
        self.log(f"[离线演示] {order['price_mode']} 买单 {order['order_id']} 已模拟成交 {quantity} 张 @ {ask}。")

    def _advance_short_entry(self, state):
        order, contract = state["order"], state["contract"]
        try:
            _, raw_bid, _ = self._quote(contract["strike"], contract["right"], contract["symbol"])
            bid = decimal_value(raw_bid)
        except ValidationError:
            return
        if bid <= 0 or (order["order_type"] == "LMT" and bid < decimal_value(order["limit_price"])):
            return
        quantity = order["remaining"]
        margin = Decimal(500) * quantity
        if margin + state["fees"] > self._available_cash() + state["reserved"]:
            self._cancel_entry(state, "演示保证金不足")
            return
        old = self._positions.get(contract["conid"])
        if old and old["quantity"] > 0:
            self._cancel_entry(state, "持仓方向已改变，不能混作平仓")
            return
        credit = bid * contract["multiplier"] * quantity - state["fees"]
        old_quantity = abs(old["quantity"]) if old else 0
        old_credit = decimal_value(old["avg_cost"]) * old_quantity if old else Decimal(0)
        total = old_quantity + quantity
        self._cash += credit
        self._positions[contract["conid"]] = {**contract, "quantity": -total,
            "avg_cost": float((old_credit + credit) / total),
            "demo_margin": float((decimal_value(old.get("demo_margin", 0)) if old else 0) + margin)}
        order.update(status="Filled (离线演示)", filled=order["filled"] + quantity,
                     remaining=0, avg_fill_price=float(bid), modifiable=False)
        self._pending_entries.pop(order["order_id"], None)
        self._generation += 1
        self._previews.clear()
        self._record_fill(order, contract, quantity, bid, state["fees"], Decimal(0))
        self.log(f"[离线演示] SELL 卖方开仓 {quantity} 张 @ {bid}；模拟保证金 $500/张，不代表 IBKR 要求。")

    def _record_fill(self, order, contract, quantity, price, commission, realized_pnl):
        self._history_sequence += 1
        self._history_pending.append(dict(
            exec_id=f"DEMO:{self._history_session}:{order['order_id']}:{self._history_sequence}",
            timestamp=datetime.now(timezone.utc), **{key: contract.get(key) for key in
                ("conid", "symbol", "local_symbol", "expiry", "right", "strike", "multiplier")},
            sec_type="OPT", currency="USD", action=order["action"], quantity=float(quantity), price=float(price),
            commission=float(commission), commission_currency="USD", realized_pnl=float(realized_pnl),
            pnl_currency="USD", source="DEMO"))
        self._flush_history()

    def _flush_history(self):
        while self._history_pending:
            try:
                self._history.record_execution(self.account, self.mode, self._history_pending[0])
            except Exception as exc:
                self._history_error = f"演示成交已发生，但历史保存失败，刷新历史时将重试：{exc}"
                self.log(self._history_error)
                return
            self._history_pending.pop(0)
        self._history_error = ""

    def history(self, period="today", tz_name="Asia/Shanghai"):
        self._require_connected()
        self._flush_history()
        result = self._history.history(self.account, self.mode, period=period, tz_name=tz_name)
        if self._history_error:
            result["coverage_label"] += " " + self._history_error
        return result

    def last_session_pnl(self):
        self._require_connected()
        self._flush_history()
        return self._history.last_session_pnl(self.account, self.mode)

    def execution_history(self, period="today", tz_name="Asia/Shanghai", refresh=False):
        result = self.history(period, tz_name)
        result.update(api_refresh_error="", api_history_note="离线合成演示历史，仅记录本程序生成的虚构成交。")
        return result

    def import_execution_history(self, path, tz_name="America/New_York"):
        self._require_connected()
        return self._history.import_flex(path, self.account, self.mode, tz_name=tz_name)

    def import_flex_reports(self, reports, tz_name="America/New_York"):
        raise ValidationError("Flex 在线同步仅用于对应的 IBKR 账户；演示模式不接收真实报表。")

    def _record(self, action, quantity, price, contract, realized_pnl=0):
        self._generation += 1
        self._previews.clear()
        order = {"order_id": len(self._orders) + 1, "action": action,
                 "quantity": quantity, "filled": quantity, "remaining": 0,
                 "status": "Filled (离线演示)", "local_symbol": contract["local_symbol"],
                 "avg_fill_price": float(price), "conid": contract["conid"],
                 "order_type": "MKT", "limit_price": None, "stop_price": None,
                 "trail_mode": None, "trail_value": None, "limit_offset": None,
                 "tif": "DAY", "outside_rth": False}
        order.update(price_mode="MKT", modifiable=False, owned=True)
        self._register_order(order, contract)
        self._record_fill(order, contract, quantity, price, Decimal(quantity), realized_pnl)
        self.log(f"[离线演示] {action} {quantity} 张 {contract['local_symbol']} @ {price}，已模拟成交。")
        return deepcopy(order)

    def positions(self):
        self._require_connected()
        results = []
        for position in self._positions.values():
            _, bid, ask = self._quote(position["strike"], position["right"], position["symbol"])
            enriched = enrich_position(position, quote=dict(bid=float(bid), ask=float(ask), last=float((bid + ask) / 2),
                bid_age=0, ask_age=0, last_age=0, quote_type="合成演示", synthetic=True), orders=self._orders)
            pending = any(s["conid"] == position["conid"] for s in list(self._pending_exits.values()) + list(self._pending_covers.values()))
            enriched.update(close_pending=pending, close_status="已有待成交平仓单" if pending else "可预览平仓")
            matches = [s for s in self._pending_exits.values() if s["conid"] == position["conid"]]
            if (len(matches) == 1 and matches[0]["spec"]["type"] in STOP_TYPES
                    and 0 < matches[0]["order"]["remaining"] <= position["quantity"]):
                enriched["can_add_with_stop"] = True
                enriched["protection_id"] = matches[0]["order"]["order_id"]
                enriched["close_status"] = f'保护止损 #{enriched["protection_id"]} · 快捷平仓会先撤此单'
            results.append(enriched)
        return deepcopy(results)

    def _register_order(self, order, contract):
        order.update({name: contract.get(name) for name in ("symbol", "expiry", "right", "strike", "trading_class")})
        order["placed_at"] = time.time()
        self._orders.append(order)

    def _display_orders(self):
        rows = self.orders()
        for row in rows:
            if row.get("remaining", 0) > 0 and row.get("strike"):
                _, bid, ask = self._quote(row["strike"], row["right"], row["symbol"])
                row["quote"] = dict(bid=float(bid), ask=float(ask), last=None, bid_age=0, ask_age=0, quote_type="合成演示")
        return rows

    def orders(self):
        self._require_connected()
        return deepcopy(self._orders)

    def _quote_cost_stop(self, position, multiple):
        from exit_orders import cost_stop_quote
        _, bid, _ = self._quote(position["strike"], position["right"], position["symbol"])
        return cost_stop_quote(position["avg_cost"], position["multiplier"], position["quantity"],
                               float(bid), [(0, .01)], multiple)

    def prepare_cost_stop(self, conid, multiple=1):
        self._require_connected()
        position = self._positions.get(int(conid))
        if not position or position["quantity"] <= 0:
            raise ValidationError("请选择有持仓的买方期权。")
        quote = self._quote_cost_stop(position, multiple)
        pending = [s for s in self._pending_exits.values() if s["conid"] == conid]
        if any(s["conid"] == conid for s in self._pending_entries.values()):
            raise ValidationError("该合约有未完成开仓单，请先核对。")
        if pending:
            state = pending[0]
            order = state["order"]
            if (len(pending) != 1 or order["order_type"] != "STP" or state["triggered"] or order["filled"]
                    or order["remaining"] != position["quantity"] or order["tif"] != "DAY"):
                raise ValidationError("该合约已有其他未完成卖单，请在订单页核对。")
            if quote["stop_price"] <= order["stop_price"]:
                raise ValidationError(f'原止损已在 {order["stop_price"]:g}，请选择更高的触发价。')
            quote.update(order_id=order["order_id"], old_stop=order["stop_price"])
        return dict(conid=int(conid), **quote)

    def set_breakeven_stop(self, conid):
        self._require_connected()
        self.pump()
        conid = int(conid)
        position = self._positions.get(conid)
        if not position or position["quantity"] <= 0:
            raise ValidationError("请选择有持仓的买方期权。")
        guard = self._quote_cost_stop(position, 1)
        if any(s["conid"] == conid for s in (*self._pending_entries.values(), *self._pending_covers.values())):
            raise ValidationError("所选合约已有未完成订单，请先撤单或等待成交。")
        stops = [s for s in self._pending_exits.values() if s["conid"] == conid]
        if stops:
            if (any(s["spec"]["type"] not in STOP_TYPES or not s["order"].get("owned")
                    or s["order"]["remaining"] <= 0 for s in stops)
                    or sum(s["order"]["remaining"] for s in stops) > position["quantity"]):
                raise ValidationError("所选保护止损已变化，请刷新持仓。")
            old = stops[0]["order"]
            if (len(stops) == 1 and old["order_type"] == "STP" and old["tif"] == "DAY"
                    and old["remaining"] == position["quantity"] and old["stop_price"] == guard["stop_price"]):
                return dict(deepcopy(old), stop_unchanged=True)
            for state in stops:
                self._cancel_exit(state)
        try:
            spec = dict(type="STP", tif="DAY", stop_price=guard["stop_price"])
            return self.close(conid, 100, spec, cost_guard=guard)
        except Exception as exc:
            if stops:
                raise ValidationError(f"旧止损已结束，请核对新保本单状态和剩余持仓。{exc}") from exc
            raise

    def raise_cost_stop(self, cost_guard):
        from exit_orders import check_cost_stop
        self._require_connected()
        self.pump()
        quote = self.prepare_cost_stop(cost_guard["conid"], cost_guard["multiple"])
        if not quote.get("order_id") or any(cost_guard.get(k) != quote.get(k) for k in ("order_id", "old_stop")):
            raise ValidationError("原止损单已变化或已结束，请刷新持仓。")
        check_cost_stop(cost_guard, quote, dict(type="STP", stop_price=cost_guard["stop_price"]), cost_guard["quantity"])
        state = self._pending_exits[quote["order_id"]]
        state["spec"]["stop_price"] = state["order"]["stop_price"] = quote["stop_price"]
        self._generation += 1
        return {**deepcopy(state["order"]), "modification": "amend_acknowledged"}

    def close(self, conid, percent, order_spec=None, cost_guard=None, protection_id=None):
        if protection_id is not None:
            if order_spec is not None or cost_guard is not None:
                raise ValidationError("撤保护单只适用于平仓。")
            preview = self.preview_close(conid, percent, protection_id=protection_id)
            return self.close_confirm(preview["token"])
        self._require_connected()
        conid = int(conid)
        if any(state["conid"] == conid for state in self._pending_entries.values()):
            raise ValidationError("该演示合约有未完成开仓单，请先撤单或等待成交。")
        exits = [s for s in self._pending_exits.values() if s["conid"] == conid]
        if exits:
            if order_spec is None and cost_guard is None and len(exits) == 1 and exits[0]["spec"]["type"] in STOP_TYPES:
                preview = self.preview_close(conid, percent, protection_id=exits[0]["order"]["order_id"])
                return self.close_confirm(preview["token"])
            raise ValidationError("该演示合约已有未完成卖单，请先撤单或等待触发，避免重复卖出。")
        if any(state["conid"] == conid for state in self._pending_covers.values()):
            raise ValidationError("该演示合约已有未完成买回单，请先撤单或等待成交。")
        position = self._positions.get(conid)
        if not position:
            raise ValidationError("找不到该演示持仓，可能已经平仓。")
        if position["quantity"] < 0:
            return self._cover_short(position, percent, order_spec)
        quantity = close_quantity(position["quantity"], percent)
        _, bid, _ = self._quote(position["strike"], position["right"], position["symbol"])
        bid = decimal_value(bid)
        if order_spec is not None:
            from exit_orders import validate_exit_spec
            spec = validate_exit_spec(deepcopy(order_spec), [(0, .01)])
            if cost_guard is not None:
                from exit_orders import check_cost_stop
                check_cost_stop(cost_guard, self.prepare_cost_stop(conid, cost_guard.get("multiple", 1)), spec, quantity)
            if bid <= 0:
                raise ValidationError("演示盘口无有效正数买价，不能提交挂单。")
            order = {"order_id": len(self._orders) + 1, "action": "SELL",
                     "quantity": quantity, "filled": 0, "remaining": quantity,
                     "status": "Submitted (离线演示)", "local_symbol": position["local_symbol"],
                     "avg_fill_price": 0.0, "conid": conid, "order_type": spec["type"],
                     **{key: spec.get(key) for key in ("limit_price", "stop_price", "trail_mode",
                                                       "trail_value", "limit_offset", "tif")},
                     "outside_rth": False, "triggered": False, "high_water": None}
            order.update(price_mode=spec["type"], modifiable=spec["type"] == "LMT", owned=True, protection=cost_guard is not None)
            state = {"order": order, "conid": conid, "contract": deepcopy(position),
                     "spec": spec, "high_water": bid, "created_day": _new_york_date(),
                     "triggered": False, "stop": None}
            if spec["type"] in ("TRAIL", "TRAIL LIMIT"):
                self._trail_stop(state, bid)
                if state["stop"] <= 0:
                    raise ValidationError("追踪幅度使初始演示止损价非正数，请减小追踪幅度。")
            self._register_order(order, position)
            self._pending_exits[order["order_id"]] = state
            self._generation += 1
            self._previews.clear()
            self.log(f"[离线演示] 已提交 {spec['type']} 卖单 {quantity} 张；按合成买价判断触发，未触发不扣持仓。")
            if spec["tif"] == "DAY":
                self.log("[离线演示] DAY 挂单在纽约日期切换时失效；未模拟交易所的实际每日收单时刻。")
            self._advance_exit(state, bid=bid)
            return deepcopy(order)
        # Demo sells use a documented $1 per-contract fee assumption.
        fee = Decimal(quantity)
        realized = (bid * position["multiplier"] - decimal_value(position["avg_cost"])) * quantity - fee
        self._cash += bid * position["multiplier"] * quantity - fee
        position["quantity"] -= quantity
        contract = deepcopy(position)
        if not position["quantity"]:
            del self._positions[conid]
        return self._record("SELL", quantity, bid, contract, realized)

    def _cover_short(self, position, percent, order_spec=None):
        if order_spec is not None and order_spec.get("type") != "LMT":
            raise ValidationError("空头持仓请使用快捷买入平仓；本版尚不支持空头条件平仓单。")
        quantity = close_quantity(abs(position["quantity"]), percent)
        _, _, raw_ask = self._quote(position["strike"], position["right"], position["symbol"])
        ask = decimal_value(raw_ask)
        if ask <= 0:
            raise ValidationError("缺少有效卖一价，不能买入平仓。")
        if order_spec is not None:
            from exit_orders import validate_exit_spec
            spec = validate_exit_spec(deepcopy(order_spec), [(0, .01)])
            released = decimal_value(position.get("demo_margin", 0)) * quantity / abs(position["quantity"])
            reserve = max(Decimal(0), decimal_value(spec["limit_price"]) * position["multiplier"] * quantity + quantity - released)
            if reserve > self._available_cash():
                raise ValidationError("买回限价及费用超过演示可用资金与释放保证金，未提交。")
            order = dict(order_id=len(self._orders) + 1, action="BUY", quantity=quantity, filled=0,
                         remaining=quantity, avg_fill_price=0.0, status="Submitted (离线演示)",
                         local_symbol=position["local_symbol"], conid=position["conid"], order_type="LMT",
                         limit_price=spec["limit_price"], price_mode="LMT", tif="DAY", outside_rth=False,
                         closes_short=True, modifiable=False, owned=True)
            state = dict(order=order, conid=position["conid"], contract=deepcopy(position),
                         reserved=reserve, created_day=_new_york_date())
            self._register_order(order, position)
            self._pending_covers[order["order_id"]] = state
            self._generation += 1
            self._previews.clear()
            self._advance_cover(state)
            return deepcopy(order)
        margin = decimal_value(position.get("demo_margin", 0))
        released = margin * quantity / abs(position["quantity"])
        cost = ask * position["multiplier"] * quantity + Decimal(quantity)
        if cost > self._available_cash() + released:
            raise ValidationError("演示买入平仓现金不足，未提交。")
        realized = decimal_value(position["avg_cost"]) * quantity - cost
        contract = deepcopy(position)
        self._cash -= cost
        position["quantity"] += quantity
        position["demo_margin"] = float(margin - released)
        if position["quantity"] == 0:
            del self._positions[position["conid"]]
        return self._record("BUY", quantity, ask, contract, realized)

    def _cancel_cover(self, state, reason="已撤单"):
        state["order"].update(status="Cancelled (离线演示)", cancel_reason=reason, modifiable=False)
        self._pending_covers.pop(state["order"]["order_id"], None)
        self._generation += 1
        self._previews.clear()

    def _advance_cover(self, state):
        order = state["order"]
        if _new_york_date() != state["created_day"]:
            self._cancel_cover(state, "DAY 纽约日期已切换")
            return
        position = self._positions.get(state["conid"])
        if not position or position["quantity"] >= 0 or abs(position["quantity"]) < order["remaining"]:
            self._cancel_cover(state, "空头持仓已变化，防止买成多头")
            return
        _, _, raw_ask = self._quote(position["strike"], position["right"], position["symbol"])
        ask = decimal_value(raw_ask)
        if ask <= 0 or ask > decimal_value(order["limit_price"]):
            return
        quantity = order["remaining"]
        margin = decimal_value(position.get("demo_margin", 0))
        released = margin * quantity / abs(position["quantity"])
        cost = ask * position["multiplier"] * quantity + quantity
        if cost > self._available_cash() + state["reserved"] + released:
            self._cancel_cover(state, "买回资金不足")
            return
        realized = decimal_value(position["avg_cost"]) * quantity - cost
        self._cash -= cost
        position["quantity"] += quantity
        position["demo_margin"] = float(margin - released)
        if position["quantity"] == 0:
            del self._positions[state["conid"]]
        order.update(status="Filled (离线演示)", filled=order["filled"] + quantity, remaining=0, avg_fill_price=float(ask))
        self._pending_covers.pop(order["order_id"], None)
        self._generation += 1
        self._previews.clear()
        self._record_fill(order, state["contract"], quantity, ask, quantity, realized)

    def preview_close(self, conid, percent=100, order_mode="MKT", limit_price=None, protection_id=None):
        self._require_connected()
        self.pump()
        conid = int(conid)
        position = self._positions.get(conid)
        if not position:
            raise ValidationError("所选持仓已不存在，请刷新持仓。")
        conflicts = [s for s in (*self._pending_entries.values(), *self._pending_exits.values(), *self._pending_covers.values()) if s["conid"] == conid]
        if protection_id is not None:
            if (len(conflicts) != 1 or self._pending_exits.get(protection_id) is not conflicts[0]
                    or conflicts[0]["spec"]["type"] not in STOP_TYPES
                    or not 0 < conflicts[0]["order"]["remaining"] <= position["quantity"]):
                raise ValidationError("所选保护止损已变化，请刷新持仓。")
        elif conflicts:
            raise ValidationError("所选合约已有未完成订单，请先撤单或等待成交。")
        quantity = close_quantity(abs(position["quantity"]), percent)
        action = "SELL" if position["quantity"] > 0 else "BUY"
        _, bid, ask = self._quote(position["strike"], position["right"], position["symbol"])
        auto_limit = order_mode == "LMT" and (limit_price is None or str(limit_price).strip() == "")
        plan = build_entry_plan(action, order_mode, {"bid": bid, "ask": ask}, [(0, .01)],
                                limit_price=None if auto_limit else limit_price, auto_limit=auto_limit)
        premium = plan["sizing_price"] * position["multiplier"] * quantity
        fees = float(quantity)
        pnl = ((plan["sizing_price"] * position["multiplier"] - abs(position["avg_cost"]))
               * quantity * (1 if action == "SELL" else -1) - fees)
        preview = dict(token=uuid.uuid4().hex, contract=deepcopy(position), action=action, quantity=quantity,
                       position_quantity=position["quantity"], bid=float(bid), ask=float(ask),
                       fee_reserve=fees, estimated_premium=premium, estimated_pnl=pnl,
                       estimated_cost=premium + fees if action == "BUY" else fees,
                       estimated_proceeds=premium - fees if action == "SELL" else -(premium + fees),
                       close_intent=True, expected_position=position["quantity"],
                       created_at=time.time(), mode=self.mode, account=self.account,
                       session_kind="regular", session_label="离线合成演示", route="DEMO", **plan)
        self._close_previews = {k: v for k, v in self._close_previews.items() if time.time() - v[0]["created_at"] <= PREVIEW_TTL_SECONDS}
        if protection_id is not None:
            preview["protection_id"] = protection_id
        self._close_previews[preview["token"]] = (deepcopy(preview), percent, self._generation)
        return preview

    def close_confirm(self, token):
        self._require_connected()
        saved = self._close_previews.pop(token, None)
        if saved is None:
            raise ValidationError("平仓预览已使用或失效，请重新预览。")
        preview, percent, generation = saved
        self.pump()
        position = self._positions.get(preview["contract"]["conid"])
        if (time.time() - preview["created_at"] > PREVIEW_TTL_SECONDS or generation != self._generation
                or not position or position["quantity"] != preview["position_quantity"]):
            raise ValidationError("持仓或预览已变化，请重新预览平仓。")
        spec = None if preview["order_type"] == "MKT" else dict(type="LMT", limit_price=preview["limit_price"], tif="DAY")
        if preview.get("protection_id") is not None:
            state = self._pending_exits.get(preview["protection_id"])
            if not state or state["conid"] != position["conid"] or state["spec"]["type"] not in STOP_TYPES:
                raise ValidationError("原止损已变化，请刷新。")
            self._cancel_exit(state)
        order = self.close(position["conid"], percent, spec)
        order["price_mode"] = preview["price_mode"]
        self._orders[-1]["price_mode"] = preview["price_mode"]
        return order

    def _trail_stop(self, state, bid):
        spec, order = state["spec"], state["order"]
        state["high_water"] = max(state["high_water"], bid)
        high, trail = state["high_water"], decimal_value(spec["trail_value"])
        derived = high * (1 - trail / 100) if spec["trail_mode"] == "percent" else high - trail
        floors = [derived]
        if spec.get("stop_price") is not None:
            floors.append(decimal_value(spec["stop_price"]))
        if state["stop"] is not None:
            floors.append(state["stop"])
        state["stop"] = max(floors)
        order["stop_price"], order["high_water"] = float(state["stop"]), float(high)

    def _cancel_exit(self, state, reason="已撤单"):
        order = state["order"]
        order["status"] = "Cancelled (离线演示)"
        order["modifiable"] = False
        order["cancel_reason"] = reason
        self._pending_exits.pop(order["order_id"], None)
        self._generation += 1
        self._previews.clear()
        self.log(f"[离线演示] 卖单 {order['order_id']} {reason}，未成交持仓保留。")

    def _advance_exit(self, state, bid=None):
        order, spec = state["order"], state["spec"]
        if order["order_id"] not in self._pending_exits:
            return
        if spec["tif"] == "DAY" and _new_york_date() != state["created_day"]:
            self._cancel_exit(state, "DAY 纽约日期已切换")
            return
        position = self._positions.get(state["conid"])
        if not position or position["quantity"] < order["remaining"]:
            self._cancel_exit(state, "可用演示持仓不足，拒绝卖成空头")
            return
        if bid is None:
            _, bid, _ = self._quote(position["strike"], position["right"], position["symbol"])
        try:
            bid = decimal_value(bid)
        except ValidationError:
            return
        if bid <= 0:
            return
        kind = spec["type"]
        if kind in ("TRAIL", "TRAIL LIMIT") and not state["triggered"]:
            self._trail_stop(state, bid)
        if kind in ("STP", "STP LMT", "TRAIL", "TRAIL LIMIT") and not state["triggered"]:
            stop = state["stop"] if kind.startswith("TRAIL") else decimal_value(spec["stop_price"])
            if bid <= stop:
                state["triggered"], order["triggered"] = True, True
                if kind == "TRAIL LIMIT":
                    from order_policy import protected_limit
                    raw_limit = stop - decimal_value(spec["limit_offset"])
                    if raw_limit <= 0:
                        self._cancel_exit(state, "追踪止损减限价偏移量后非正数")
                        return
                    order["limit_price"] = protected_limit("SELL", raw_limit, 0, [(0, .01)])
                self.log(f"[离线演示] 卖单 {order['order_id']} 已由合成买价触发；止损价 {stop:g}。")
        fill = False
        if kind == "MKT":
            fill = True
        elif kind == "LMT":
            fill = bid >= decimal_value(order["limit_price"])
        elif state["triggered"]:
            fill = kind in ("STP", "TRAIL") or bid >= decimal_value(order["limit_price"])
        if not fill:
            return
        quantity = order["remaining"]
        realized = (bid * position["multiplier"] - decimal_value(position["avg_cost"])) * quantity - quantity
        self._cash += bid * position["multiplier"] * quantity - Decimal(quantity)
        position["quantity"] -= quantity
        if position["quantity"] == 0:
            del self._positions[state["conid"]]
        previous_filled = order["filled"]
        average = ((decimal_value(order["avg_fill_price"]) * previous_filled + bid * quantity)
                   / (previous_filled + quantity))
        order.update(status="Filled (离线演示)", filled=previous_filled + quantity,
                     remaining=0, avg_fill_price=float(average), modifiable=False)
        self._pending_exits.pop(order["order_id"], None)
        self._generation += 1
        self._previews.clear()
        self._record_fill(order, state["contract"], quantity, bid, quantity, realized)
        self.log(f"[离线演示] {kind} 卖单 {order['order_id']} 已模拟成交 {quantity} 张 @ {bid}；手续费按 $1/张。")

    def close_all(self, symbol=None):
        self._require_connected()
        symbol = normalize_symbol(symbol) if symbol is not None else None
        targets = [conid for conid, position in self._positions.items()
                   if symbol is None or position["symbol"] == symbol]
        return [self.close(conid, 100) for conid in targets]

    def _pending_state(self, order_id):
        try:
            order_id = int(order_id)
        except (TypeError, ValueError):
            raise ValidationError("请选择有效订单编号。") from None
        state = self._pending_entries.get(order_id) or self._pending_exits.get(order_id) or self._pending_covers.get(order_id)
        if state is None or state["order"]["remaining"] <= 0:
            raise ValidationError("订单已成交、已撤销或不属于本程序，不能修改。")
        return state

    def preview_modify(self, order_id, order_mode, limit_price=None):
        self._require_connected()
        self.pump()
        state = self._pending_state(order_id)
        order, contract = state["order"], state["contract"]
        if order.get("closes_short"):
            raise ValidationError("买回挂单请先撤销，再从持仓重新预览平仓。")
        if order.get("opens_short"):
            raise ValidationError("卖方开仓挂单请先撤销，再重新预览保证金并开仓；本版不直接改单。")
        if order["order_type"] not in ("MKT", "LMT"):
            raise ValidationError("改单仅支持普通 MKT/LMT 挂单；止损和追踪单请先撤销，再重新下单。")
        _, bid, ask = self._quote(contract["strike"], contract["right"], contract["symbol"])
        plan = build_entry_plan(order["action"], order_mode, {"bid": bid, "ask": ask},
                                [(0, .01)], limit_price=limit_price,
                                slippage_pct=state.get("slippage_pct", 5))
        quantity = order["remaining"]
        reserved = Decimal(0)
        fee_reserve = Decimal(0)
        if order["action"] == "BUY":
            fee_reserve = max(decimal_value(state["min_commission"]),
                              decimal_value(state["commission_per_contract"]) * quantity)
            reserved = (decimal_value(plan["sizing_price"]) * contract["multiplier"] * quantity
                        * (1 + decimal_value(plan["reserve_slippage_pct"]) / 100) + fee_reserve)
            if state.get("cash_ceiling") is not None and reserved > state["cash_ceiling"]:
                raise ValidationError("修改后买单超过原先指定的美元预算；请撤单后按新预算下单。")
            if reserved > self._available_cash() + state["reserved"]:
                raise ValidationError("修改后买单的剩余张数超过可用现金；请撤单后按新预算下单。")
        else:
            position = self._positions.get(state["conid"])
            if not position or position["quantity"] < quantity:
                raise ValidationError("当前可用持仓不足以覆盖剩余卖单。")
        preview = {"token": uuid.uuid4().hex, "order_id": order["order_id"],
                   "old_order_type": order["order_type"], "old_limit_price": order["limit_price"],
                   "old_stop_price": order.get("stop_price"), "new_order_type": plan["order_type"],
                   "new_limit_price": plan["limit_price"], "quantity": order["quantity"],
                   "filled": order["filled"], "remaining": quantity, "action": order["action"],
                   "local_symbol": order["local_symbol"], "bid": float(bid), "ask": float(ask),
                   "created_at": time.time(), "mode": self.mode, "account": self.account,
                   "method": "amend" if order["order_type"] == plan["order_type"] else "replace",
                   "reserved_cash": float(reserved), **plan}
        if order["action"] == "BUY":
            preview.update(slippage_pct=float(decimal_value(state["slippage_pct"])),
                           commission_per_contract=float(decimal_value(state["commission_per_contract"])),
                           min_commission=float(decimal_value(state["min_commission"])),
                           fee_reserve=float(fee_reserve))
        self._modify_previews = {k: v for k, v in self._modify_previews.items()
                                 if time.time() - v[0]["created_at"] <= PREVIEW_TTL_SECONDS}
        self._modify_previews[preview["token"]] = (deepcopy(preview), deepcopy(order), self._generation)
        return deepcopy(preview)

    def modify_order(self, token):
        self._require_connected()
        guard = getattr(self, "_auto_mid_guard", None)
        if guard is not None and not guard():
            raise ValidationError("自动追价已停止或到时，未提交修改。")
        stored = self._modify_previews.pop(token, None)
        if stored is None:
            raise ValidationError("改单预览已使用或失效，请重新预览。")
        preview, original, generation = stored
        if time.time() - preview["created_at"] > PREVIEW_TTL_SECONDS:
            raise ValidationError("改单预览超过 10 秒，请重新预览。")
        self.pump()
        state = self._pending_state(preview["order_id"])
        order = state["order"]
        if generation != self._generation or order != original:
            raise ValidationError("订单成交状态、持仓或现金已改变，请重新预览改单。")
        if order["action"] == "BUY":
            reserve = decimal_value(preview["reserved_cash"])
            if reserve > self._available_cash() + state["reserved"]:
                raise ValidationError("修改后的预留现金不足，请撤单后重新下单。")
            state["reserved"] = reserve
            state["fees"] = decimal_value(preview["fee_reserve"])
        else:
            position = self._positions.get(state["conid"])
            if not position or position["quantity"] < order["remaining"]:
                raise ValidationError("当前持仓不足，拒绝修改卖单。")
            # Ordinary sell-order replacements carry no trigger fields.
            state.update(spec={"type": preview["order_type"], "tif": order["tif"]},
                         triggered=False, stop=None, high_water=Decimal(0))
        if preview["method"] == "replace":
            previous_id = order["order_id"]
            new_state = deepcopy(state)
            new_order = new_state["order"]
            if order["action"] == "BUY":
                self._cancel_entry(state, "改单：模拟确认撤销后改发新类型")
            else:
                self._cancel_exit(state, "改单：模拟确认撤销后改发新类型")
            new_order.update(order_id=len(self._orders) + 1, quantity=order["remaining"],
                             filled=0, remaining=order["remaining"], avg_fill_price=0.0,
                             replaces_order_id=previous_id, status="Submitted (离线演示)",
                             modifiable=True)
            self._register_order(new_order, new_state['contract'])
            if order["action"] == "BUY":
                self._pending_entries[new_order["order_id"]] = new_state
            else:
                self._pending_exits[new_order["order_id"]] = new_state
            state, order = new_state, new_order
        order.update(order_type=preview["order_type"], limit_price=preview["limit_price"],
                     price_mode=preview["price_mode"], outside_rth=preview["outside_rth"],
                     stop_price=None, trail_mode=None, trail_value=None, limit_offset=None,
                     triggered=False, high_water=None)
        self._generation += 1
        self._previews.clear()
        self._modify_previews.clear()
        self.log(f"[离线演示] 订单 {order['order_id']} 已改为 {preview['price_mode']}，剩余 {order['remaining']} 张不变；方式 {preview['method']}。")
        if order["action"] == "BUY":
            self._advance_entry(state)
        else:
            self._advance_exit(state)
        return deepcopy(order)

    def cancel_order(self, order_id):
        self._require_connected()
        self.pump()
        state = self._pending_state(order_id)
        if state["order"]["order_id"] in self._pending_entries:
            self._cancel_entry(state)
        elif state["order"]["order_id"] in self._pending_covers:
            self._cancel_cover(state)
        else:
            self._cancel_exit(state)
        return deepcopy(state["order"])

    def cancel_own(self):
        self._require_connected()
        pending_entries = list(self._pending_entries.values())
        pending = list(self._pending_exits.values())
        covers = list(self._pending_covers.values())
        for state in pending_entries:
            self._cancel_entry(state)
        for state in pending:
            self._cancel_exit(state)
        for state in covers:
            self._cancel_cover(state)
        if not pending and not pending_entries and not covers:
            self.log("离线演示当前没有待撤订单。")
        return len(pending) + len(pending_entries) + len(covers)

    def snapshot(self):
        if not self.connected:
            funds = {key: None for key in ("net_liquidation", "net_liquidation_currency", "usd_cash",
                                          "settled_cash", "available_funds", "cash", "base_currency", "updated_at")}
            funds.update(ready=False, error="已断开", source="离线合成演示")
            return {"mode": self.mode, "account": self.account, "cash": None,
                    "positions": [], "orders": [], "connected": False,
                    "financial": funds, "cash_ready": False}
        self.pump()
        mark_value = sum(self._quote(p["strike"], p["right"], p["symbol"])[1 if p["quantity"] > 0 else 2]
                         * p["quantity"] * p["multiplier"] for p in self._positions.values())
        equity = float(self._cash + mark_value)
        day = _new_york_date()
        if getattr(self, "_pnl_day", None) != day:
            self._pnl_day, self._pnl_start = day, equity
        funds = {"daily_pnl": equity - self._pnl_start, "net_liquidation": equity,
                 "net_liquidation_currency": "USD", "total_cash_value": float(self._cash), "usd_cash": float(self._cash),
                 "settled_cash": float(self._cash), "available_funds": float(self._available_cash()),
                 "cash": float(self._available_cash()), "base_currency": "USD", "ready": self.connected,
                 "error": "", "updated_at": time.time(), "source": "离线合成演示"}
        return {"mode": self.mode, "account": self.account, "cash": float(self._available_cash()),
                "positions": self.positions(),
                "orders": self._display_orders(), "connected": self.connected,
                "financial": funds, "cash_ready": self.connected}

    def refresh_funds(self):
        self._require_connected()
        return self.snapshot()

    def refresh_positions(self):
        self._require_connected()
        return self.snapshot()
