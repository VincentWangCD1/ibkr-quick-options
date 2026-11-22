"""IBKR TWS/Gateway adapter. All methods run on ONE worker thread.

Only explicit buy/close calls can transmit an order. Connection/preview never
trade. Market orders have no guaranteed maximum execution cost. This adapter
deliberately fails closed when it cannot establish currency, session or quotes.
"""
from __future__ import annotations

import copy
import sqlite3
import math
import re
import time
import uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from core import (close_quantity, select_strike, select_settings_strike, opening_quantity,
                  size_order, normalize_symbol, SUPPORTED_SYMBOLS)
from core import affordable_size, budget_strikes
from account_data import AccountData
from market_data import MarketDataMixin
from trading_sessions import decide_session
from order_policy import protected_limit
from entry_orders import build_entry_plan, resolve_entry_price, limit_contract_changed
from order_amendments import OrderAmendmentsMixin
from short_orders import ShortOrdersMixin
from position_broker import PositionBrokerMixin
from market_runtime import (expired_selection_reason, refresh_execution_calendar,
                            pin_quote_contract, pinned_quote_ids, reset_quote_stream,
                            release_quote_stream)


class BrokerError(RuntimeError):
    pass


class BudgetScanPending(BrokerError):
    pass


class IBBroker(MarketDataMixin, OrderAmendmentsMixin, ShortOrdersMixin, PositionBrokerMixin):
    TOKEN_TTL = 10.0
    QUOTE_TTL = 10.0
    ORDER_PREFIX = "QOPT1:"

    def __init__(self, log=lambda message: None, history_path=None):
        # Lazy import: the offline demo has no third-party dependency.
        from ib_async import IB
        self.ib = IB()
        self.ib.RequestTimeout = 20
        self.ib.RaiseRequestErrors = True
        self.log = log
        self._history_path = history_path
        self._history_store = None
        self._account_data = AccountData(self.ib, self.log)
        self._financial = {"ready": False, "cash": None, "error": "账户资金尚未读取"}
        self._watched_symbols = set(SUPPORTED_SYMBOLS)
        self._chain_stream_ids = set()
        self.mode = "PAPER"
        self.account = ""
        self.client_id = 31
        self._cash = 0.0
        self._cash_time = 0.0
        self._last_account_values = []
        self._cash_field_signature = None
        self._underlyings = {}
        self._chains = {}
        self._details = {}
        self._route_details = {}
        self._price_rules = {}
        self._chain_details = {}
        self._tickers = {}
        self._price_times = {}
        self._display_times = {}
        self._market_errors = {}
        self._order_warnings = {}
        self._order_rejections = {}
        self._last_funds_error = None
        self._tokens = {}
        self._close_tokens = {}
        self._modify_tokens = {}
        self._amend_pending = set()
        self._order_sizing_settings = {}
        self._uncertain = False
        self._dirty_account = False
        self._debit_reserve = 0.0
        self._reserve_cash_baseline = None
        self._reserve_usd_baseline = None
        self._reserve_trade = None
        self._raw_cash = 0.0
        self._close_reservations = {}
        self._close_sync_at = -1e9
        self._position_stream_ids = set()
        self._short_margin_hold = None
        self._cover_cash_hold = None
        self._account_sync_values = None
        self._subscribed_account = ""
        self.ib.errorEvent += self._on_error
        self.ib.disconnectedEvent += self._on_disconnect
        self._position_sync_times = []
        self._unconfirmed_opens = {}
        self._position_download_updates = None
        self.ib.positionEvent += self._on_position_update
        self.ib.orderStatusEvent += self._on_order_status
        self.ib.execDetailsEvent += self._on_execution
        self.ib.accountValueEvent += self._on_account_value
        from trade_history import attach_history_callbacks
        attach_history_callbacks(self.ib, self._get_history_store,
                                 lambda: (self.mode, self.account), self.log)

    @staticmethod
    def _finite(value, positive=False):
        try:
            value = float(value)
            return math.isfinite(value) and (value > 0 if positive else value >= 0)
        except (TypeError, ValueError):
            return False

    def _require(self):
        if not self.ib.isConnected() or not self.account:
            raise BrokerError("尚未连接并验证 IBKR 账户。")
        if self._uncertain:
            raise BrokerError("上笔请求结果不明确，已锁定发单。请先在 TWS 核对订单/成交，再断开并重新连接；不要直接重复下单。")

    def connect(self, mode="PAPER", host="127.0.0.1", port=7497,
                client_id=31, account="", live_ack=""):
        if mode not in ("PAPER", "LIVE"):
            raise BrokerError("IBKR 连接模式必须是 PAPER 或 LIVE。")
        account = account.strip().upper()
        if int(client_id) <= 0:
            raise BrokerError("Client ID 必须大于 0，请勿使用会绑定手工订单的 0。")
        prefix = r"DU\d+" if mode == "PAPER" else r"U\d+"
        if account and not re.fullmatch(prefix, account):
            raise BrokerError("账户类型与所选模拟/实盘模式不符。")
        if self.ib.isConnected():
            self.disconnect()
        else:
            # A socket drop ends the library session before connect() is called
            # again. Clear our detached tickers/holds even in that case, while
            # keeping the durable execution-history store independent.
            self._clear_session_state()
        self.mode, self.client_id = mode, int(client_id)
        try:
            self.ib.connect(host, int(port), clientId=self.client_id,
                            account=account, timeout=12, readonly=False,
                            raiseSyncErrors=True)
            accounts = tuple(dict.fromkeys(self.ib.managedAccounts()))
            if not accounts:
                raise BrokerError("TWS 未返回可访问的账户，请检查当前登录。")
            if not account:
                if len(accounts) > 1:
                    choices = tuple(a for a in accounts if re.fullmatch(prefix, a))
                    if not choices:
                        raise BrokerError("TWS 没有与所选模拟/实盘模式匹配的个人账户。")
                    self.disconnect()
                    return {**self.snapshot(), "account_choices": choices}
                account = accounts[0]
            if account not in accounts:
                raise BrokerError("填写的账户不在本次 TWS 登录可访问账户中。")
            if not re.fullmatch(prefix, account):
                raise BrokerError("账户类型与所选模拟/实盘模式不符；已中止连接。端口号不能证明账户类型。")
            self.account = account
            self._uncertain = False
            self._subscribed_account = account
            try:
                self._account_pnl = self.ib.reqPnL(account, "")
            except Exception as exc:
                self._account_pnl = None
                self.log(f"当日盈亏订阅未就绪（{type(exc).__name__}）。")
            self.ib.reqMarketDataType(1)
            # Rebuild current server evidence before displaying the new
            # session. Open orders are not cancelled or resubmitted here.
            self._download_open_orders()
            self._position_records(fresh=True, include_short=True)
            try:
                self._sync_cash(force=True)
            except BrokerError as exc:
                self.log(f"TWS 已连接，资金暂不可用于开仓：{exc}")
            self.log(f"已连接 {self.mode} / {self.account} / Client ID {self.client_id}；资金状态见顶部卡片")
            return self.snapshot()
        except Exception:
            self.ib.disconnect()
            self.account = ""
            raise

    def disconnect(self):
        self.ib.disconnect()
        self._clear_session_state()

    def _clear_session_state(self):
        self._account_pnl = None
        self._tokens.clear()
        self._close_tokens.clear()
        self._modify_tokens.clear()
        self._amend_pending.clear()
        self._order_sizing_settings.clear()
        self.account = ""
        self._cash = 0.0
        self._underlyings.clear()
        self._chains.clear()
        self._details.clear()
        self._route_details.clear()
        self._price_rules.clear()
        self._chain_details.clear()
        self._tickers.clear()
        self._price_times.clear()
        self._display_times.clear()
        self._market_errors.clear()
        self._order_warnings.clear()
        self._order_rejections.clear()
        self._last_funds_error = None
        self._debit_reserve = 0.0
        self._reserve_cash_baseline = None
        self._reserve_usd_baseline = None
        self._reserve_trade = None
        self._raw_cash = 0.0
        self._close_reservations.clear()
        self._position_sync_times.clear()
        self._unconfirmed_opens.clear()
        self._position_download_updates = None
        self._position_stream_ids.clear()
        self._missing_external_orders = set()
        self._close_sync_at = -1e9
        self._short_margin_hold = None
        self._cover_cash_hold = None
        self._account_sync_values = None
        self._financial = {"ready": False, "cash": None, "error": "已断开"}
        self._chain_stream_ids.clear()
        for name in ("_priority_quote_ids", "_quote_reset_times", "_quote_generations",
                     "_verified_calendar_cache", "_spot_reference_meta"):
            value = getattr(self, name, None)
            if isinstance(value, dict):
                value.clear()
        if hasattr(self, "_spot_reference"):
            self._spot_reference.clear()

    def pump(self):
        self.ib.sleep(0.01)
        if (self._position_sync_times and self._position_sync_times[0] <= time.monotonic()
                and self.account and self.ib.isConnected() and not self._uncertain):
            self._position_sync_times.pop(0)
            try:
                positions = self._position_records(fresh=True, include_short=True)
                if self._close_reservations:
                    self._refresh_close_reservations(force=True, positions=positions)
                self.log(f"成交后已核对持仓，当前有 {len(positions)} 个标准期权合约。")
            except Exception as exc:
                self.log(f"成交后更新持仓失败（{type(exc).__name__}），可点击刷新持仓。")

            if self._unconfirmed_opens and not self._position_sync_times:
                now = time.monotonic()
                deadline = max(until for _, until in self._unconfirmed_opens.values())
                if deadline > now:
                    self._position_sync_times.append(min(now + 10, deadline))
                else:
                    self.log("开仓成交已回报，持仓仍待券商确认，可点击「刷新持仓 / 回报」。")

    def _schedule_position_sync(self, trade):
        now = time.monotonic()
        conid = trade.contract.conId
        direction = 1 if trade.order.action == "BUY" else -1
        if self._ours(trade):
            role = self._short_order_role(trade.order)
            if role == "OPEN_SHORT" or (role is None and trade.order.action == "BUY"):
                self._unconfirmed_opens[conid] = (direction, now + 120)
            else:
                self._unconfirmed_opens.pop(conid, None)
        elif conid in self._unconfirmed_opens and self._unconfirmed_opens[conid][0] != direction:
            self._unconfirmed_opens.pop(conid)
        first = min(self._position_sync_times[0], now + 0.5) if self._position_sync_times else now + 0.5
        self._position_sync_times = [first, now + 3]

    def _on_position_update(self, position):
        if self._position_download_updates is not None:
            self._position_download_updates[(position.account, position.contract.conId)] = position
        else:
            self._observe_open_position(position)

    def _observe_open_position(self, position):
        conid = position.contract.conId
        pending = self._unconfirmed_opens.get(conid)
        if position.account == self.account and pending and position.position * pending[0] > 0:
            del self._unconfirmed_opens[conid]

    def _on_disconnect(self):
        self._tokens.clear()
        self._close_tokens.clear()
        self._modify_tokens.clear()
        self._price_times.clear()
        self._display_times.clear()
        self.log("IBKR 连接已断开；预览失效。程序不会自动重发任何订单。")

    def _on_error(self, req_id, code, message, contract=None):
        # Some TWS builds send Chinese text as literal unicode escape sequences.
        def readable(match):
            value = int(match.group(1), 16)
            return chr(value) if value >= 32 and not 0xD800 <= value <= 0xDFFF else match.group(0)
        message = re.sub(r"\\u([0-9a-fA-F]{4})", readable, str(message))
        if code in (201, 202):
            trades = self.ib.trades()
            if isinstance(trades, (list, tuple)):
                matches = [trade for trade in trades if trade.order.account == self.account
                           and self._ours(trade) and trade.order.orderId == req_id]
                if len(matches) == 1:
                    self._order_rejections[self._close_order_key(matches[0])] = {"code": code, "reconciled": False}
        if code == 2109:
            # IB may accept the order while ignoring outsideRth. Preserve this
            # on the exact order row; Submitted never proves overnight coverage.
            self._order_warnings[req_id] = "IBKR 已忽略盘外设置；请在 TWS 核对有效时段。 " + message
        if code in (354, 10089, 10090, 10091, 10167, 10168, 100):
            ticker = self.ib.wrapper.reqId2Ticker.get(req_id)
            target = contract or getattr(ticker, "contract", None)
            conid = getattr(target, "conId", None)
            if isinstance(conid, int) and conid > 0:
                self._market_errors[conid] = (code, message)
        if code == 2100 and (self._account_sync_values is not None
                             or getattr(self._account_data, "_refreshing", False) is True):
            self.log("IBKR [2100] 账户数据订阅取消通知；当前正在重新同步资金，是否成功以资金检查结果为准。")
            return
        self.log(f"IBKR [{code}] 请求 {req_id}: {message}")
        if code in (1100, 1101, 1102, 1300):
            self._tokens.clear()
            self._price_times.clear()
            if any(not t.isDone() for t in self.ib.trades()
                   if t.order.account == self.account and self._ours(t)):
                self._uncertain = True

    def _on_order_status(self, trade):
        if trade.order.account == self.account and self._ours(trade):
            self._dirty_account = True
            s = trade.orderStatus
            if s.status == "Filled":
                self._schedule_position_sync(trade)
            self.log(f"订单 {trade.order.orderId}: {s.status} | 已成交 {s.filled} / 剩余 {s.remaining}")

    def _on_execution(self, trade, fill):
        if trade.order.account == self.account:
            self._dirty_account = True
            self._tokens.clear()
            self._close_tokens.clear()
            e = fill.execution
            self._schedule_position_sync(trade)
            self.log(f"成交 {trade.contract.localSymbol}: {e.side} {e.shares} 张 @ {e.price}")

    def _on_account_value(self, value):
        if (self._account_sync_values is not None and value.account == self.account
                and not getattr(value, "modelCode", "")):
            self._account_sync_values[(value.tag, value.currency)] = value

    def _ours(self, trade):
        return (trade.order.clientId == self.client_id
                and trade.order.orderRef.startswith(self.ORDER_PREFIX))

    def _sync_cash(self, force=False):
        self._require()
        if not force and not self._dirty_account and time.monotonic() - self._cash_time < 3:
            if not self._financial.get("ready"):
                raise BrokerError(self._financial.get("error") or "资金尚未就绪")
            return self._cash
        self._financial = self._account_data.refresh(self.account)
        self._cash_time = time.monotonic()
        self._dirty_account = False
        if not self._financial.get("ready"):
            self._cash = None
            self._tokens.clear()
            raise BrokerError(self._financial.get("error") or "资金尚未就绪，禁止开仓。")
        cash = self._financial["cash"]
        self._raw_cash = cash
        # A new download can still precede the balance update after a fill.
        # Hold a conservative local reserve until the broker cash has reflected
        # that debit. This intentionally does not release on a Filled event.
        if self._debit_reserve and self._reserve_cash_baseline is not None:
            reflected = max(0.0, self._reserve_cash_baseline - cash)
            trade = self._reserve_trade
            premium = sum(float(f.execution.shares) * float(f.execution.price)
                          * float(trade.contract.multiplier) for f in trade.fills) if trade else 0.0
            terminal = trade is not None and trade.isDone()
            executed = sum(float(f.execution.shares) for f in trade.fills) if trade else 0.0
            reported = float(trade.orderStatus.filled) if trade else 0.0
            cancelled_empty = (terminal and trade.orderStatus.status in ("Cancelled", "ApiCancelled")
                               and reported == 0 and executed == 0)
            complete_executions = (terminal and executed > 0 and abs(executed - reported) < 1e-9)
            reports = [f.commissionReport for f in trade.fills] if trade else []
            fees_known = (bool(reports) and all(r.execId and r.currency == "USD"
                          and math.isfinite(float(r.commission)) and abs(float(r.commission)) < 1e100
                          for r in reports))
            total_debit = premium + sum(max(0.0, float(r.commission)) for r in reports) if fees_known else math.inf
            if cancelled_empty or (complete_executions and fees_known and premium > 0
                                   and (reflected >= total_debit - 0.01 or self._ledger_confirms_debit(trade, total_debit))):
                self._debit_reserve = 0.0
                self._reserve_cash_baseline = None
                self._reserve_trade = None
                self._reserve_usd_baseline = None
            else:
                cash = min(cash, max(0.0, self._reserve_cash_baseline - self._debit_reserve))
        self._cash = cash
        self._cash_time = time.monotonic()
        self._dirty_account = False
        return cash

    def _ledger_confirms_debit(self, trade, debit):
        # Compare like with like. The minimum budget can be settlement-limited
        # both before and after the USD ledger has posted the entire purchase.
        before = getattr(self, "_reserve_usd_baseline", None)
        after = self._financial.get("usd_cash")
        if not all(self._finite(value) for value in (before, after, debit)):
            return False
        if before - after >= debit - .005:
            return True
        # A fast round trip can occur between balance downloads, including
        # a fully closed trade with zero net change after fees.
        bought = sum(float(fill.execution.shares) for fill in trade.fills)
        times = [getattr(fill.execution, "time", None) for fill in trade.fills]
        if not times or any(not isinstance(stamp, datetime) for stamp in times):
            return False
        last_buy = max(times)
        fills = self.ib.fills()
        if not isinstance(fills, (list, tuple)):
            return False
        seen, sold, proceeds = set(), 0., 0.
        for fill in fills:
            e, c = fill.execution, fill.contract
            if e.acctNumber != self.account or c.conId != trade.contract.conId or e.time <= last_buy:
                continue
            if e.execId in seen:
                continue
            seen.add(e.execId)
            fee = fill.commissionReport
            if (e.side != "SLD" or fee.execId != e.execId or fee.currency != "USD"
                    or not self._finite(fee.commission) or abs(fee.commission) >= 1e100
                    or not self._finite(e.price, positive=True) or not self._finite(e.shares, positive=True)):
                return False
            sold += float(e.shares)
            proceeds += float(e.shares) * float(e.price) * float(c.multiplier) - float(fee.commission)
        if abs(after - before) < .005:
            return sold == bought and abs(proceeds - debit) < .005
        return 0 < sold <= bought and abs((after - before) - (proceeds - debit)) <= .010001

    def refresh_funds(self):
        self._require()
        try:
            self._sync_cash(force=True)
            self._last_funds_error = None
        except BrokerError as exc:
            if str(exc) != self._last_funds_error:
                self.log(f"账户仍已连接；开仓暂停：{exc}")
                self._last_funds_error = str(exc)
        return self.snapshot()

    def _subscribe(self, contract):
        if contract.conId not in self._tickers:
            # Bound the subscription count as the user changes strikes.
            if len(self._tickers) >= 96:
                protected = pinned_quote_ids(self)
                oldest = next((conid for conid in self._tickers if conid not in protected), None)
                if oldest is None:
                    raise BrokerError("实时行情订阅已满，请稍后重试。")
                release_quote_stream(self, oldest)
            self._price_times[contract.conId] = {}
            self._display_times[contract.conId] = {}
            self._market_errors.pop(contract.conId, None)
            generations = getattr(self, "_quote_generations", {})
            generation = generations.get(contract.conId, 0) + 1
            generations[contract.conId] = generation
            self._quote_generations = generations
            ticker = self.ib.reqMktData(contract, "", False, False)
            def updated(t):
                if (self._tickers.get(contract.conId) is not t
                        or self._quote_generations.get(contract.conId) != generation):
                    return  # Ignore callbacks from an already cancelled stream.
                times = self._price_times.setdefault(contract.conId, {})
                display = self._display_times.setdefault(contract.conId, {})
                now = time.monotonic()
                for tick in t.ticks:
                    # A side's size update confirms that side of the book,
                    # only after its real price was observed. Last/Greeks do
                    # not refresh either bid/ask; delayed ticks stay display-only.
                    if tick.tickType in (1, 2, 4) and self._finite(tick.price):
                        times[tick.tickType] = now
                    if tick.tickType in (0, 3):
                        side = 1 if tick.tickType == 0 else 2
                        price = t.bid if side == 1 else t.ask
                        if side in times and self._finite(price, positive=True) and self._finite(tick.size, positive=True):
                            times[side] = now
                    side = {0: 1, 1: 1, 3: 2, 2: 2, 4: 4,
                            66: 1, 69: 1, 67: 2, 70: 2, 68: 4}.get(tick.tickType)
                    if side is not None:
                        display[side] = now
                        display["tick"] = now
            ticker.updateEvent += updated
            self._tickers[contract.conId] = ticker
        return self._tickers[contract.conId]

    def _quote(self, contract, underlying=False, wait=4.0, required_side=None):
        pin_quote_contract(self, contract)
        stamps = self._price_times.get(contract.conId, {})
        previous = self._tickers.get(contract.conId)
        sides = (1,) if required_side == 'bid' else (2,) if required_side == 'ask' else (1, 2)
        usable_last = (underlying and previous is not None and previous.marketDataType == 1
                       and previous.halted not in (1, 2) and self._finite(previous.last, positive=True)
                       and time.monotonic() - stamps.get(4, -1e9) <= self.QUOTE_TTL)
        if (wait > 0 and contract.conId in self._tickers
                and not usable_last
                and any(time.monotonic() - stamps.get(side, -1e9) > self.QUOTE_TTL for side in sides)):
            reset_quote_stream(self, contract)
        ticker = self._subscribe(contract)
        deadline = time.monotonic() + wait
        while True:
            self._require()
            now = time.monotonic()
            stamps = self._price_times.get(contract.conId, {})
            fresh = lambda field: now - stamps.get(field, -1e9) <= self.QUOTE_TTL
            if ticker.marketDataType == 1 and ticker.halted not in (1, 2):
                if (self._finite(ticker.bid, positive=True)
                        and self._finite(ticker.ask, positive=True)
                        and ticker.bid <= ticker.ask and fresh(1) and fresh(2)
                        and (underlying or (self._finite(ticker.bidSize, positive=True)
                                            and self._finite(ticker.askSize, positive=True)))):
                    return {"bid": float(ticker.bid), "ask": float(ticker.ask),
                            "spot": (float(ticker.bid) + float(ticker.ask)) / 2}
                if required_side:
                    bid_ok = self._finite(ticker.bid, positive=True) and fresh(1)
                    ask_ok = self._finite(ticker.ask, positive=True) and fresh(2)
                    bid = float(ticker.bid) if bid_ok and self._finite(ticker.bidSize, positive=True) else None
                    ask = float(ticker.ask) if ask_ok and self._finite(ticker.askSize, positive=True) else None
                    side_price = bid if required_side == "bid" else ask
                    if side_price is not None and not (bid_ok and ask_ok and ticker.bid > ticker.ask):
                        return {"bid": bid, "ask": ask, "spot": None}
                if underlying and self._finite(ticker.last, positive=True) and fresh(4):
                    return {"bid": None, "ask": None, "spot": float(ticker.last)}
            if now >= deadline:
                label = {"bid": "买一", "ask": "卖一"}.get(required_side, "买一和卖一")
                def side_status(field, stamp):
                    price = getattr(ticker, field)
                    if not self._finite(price, positive=True):
                        return "无报价"
                    if not self._finite(getattr(ticker, field + "Size"), positive=True):
                        return "无有效挂量"
                    return f"{now - stamps[stamp]:.0f} 秒前" if stamp in stamps else "时间未知"
                kind = "停牌" if ticker.halted in (1, 2) else {1:"实时", 2:"冻结", 3:"延迟", 4:"延迟冻结"}.get(ticker.marketDataType, "行情类型未知")
                raise BrokerError(f"{contract.localSymbol or contract.symbol} 无最近 {self.QUOTE_TTL:g} 秒内实时有效报价"
                                  f"（需要{label}及有效挂量）。{kind} · 买一 {side_status('bid', 1)} / 卖一 {side_status('ask', 2)}。请刷新该合约预览。")
            self.ib.sleep(0.05)

    def _session(self, detail):
        decision = decide_session(detail, datetime.now(timezone.utc))
        if decision.kind == "closed":
            raise BrokerError(decision.reason)
        return decision

    def _execution_detail(self, detail):
        """Use SMART, or an explicitly verified CBOE calendar for this conId.

        A stock's calendar never authorizes its option. If the SMART option
        calendar lacks an extended session, query that same option on CBOE.
        """
        now = datetime.now(timezone.utc)
        reason = expired_selection_reason(detail.contract, now)
        if reason:
            raise BrokerError(reason)
        detail = refresh_execution_calendar(self, detail, now)
        if decide_session(detail, now).kind != "closed":
            return detail
        if "CBOE" not in str(getattr(detail, "validExchanges", "")).split(","):
            self._session(detail)
        key = (detail.contract.conId, now.date())
        cached = self._route_details.get(key)
        if cached and time.monotonic() - cached[0] < 300:
            routed = cached[1]
        else:
            contract = copy.copy(detail.contract)
            contract.exchange = "CBOE"
            matches = [d for d in self.ib.reqContractDetails(contract)
                       if d.contract and d.contract.conId == contract.conId
                       and d.contract.secType == "OPT"
                       and d.contract.currency == "USD"
                       and all(getattr(d.contract, field, None) == getattr(contract, field, None)
                               for field in ("symbol", "right", "strike", "tradingClass",
                                             "lastTradeDateOrContractMonth", "multiplier"))]
            routed = matches[0] if len(matches) == 1 else None
            if routed:
                routed.contract = copy.copy(routed.contract)
                routed.contract.exchange = "CBOE"
            self._route_details[key] = (time.monotonic(), routed)
        if routed is not None and decide_session(routed, now).kind != "closed":
            return routed
        self._session(detail)

    def _increments(self, detail):
        exchanges = str(getattr(detail, "validExchanges", "")).split(",")
        rules = str(getattr(detail, "marketRuleIds", "")).split(",")
        route = detail.contract.exchange
        rule_id = None
        if route in exchanges:
            index = exchanges.index(route)
            if index < len(rules) and rules[index].isdigit() and int(rules[index]) > 0:
                rule_id = int(rules[index])
        if rule_id is not None:
            if rule_id not in self._price_rules:
                entries = self.ib.reqMarketRule(rule_id)
                if not entries:
                    raise BrokerError("无法读取该路由的价格阶梯，暂不能构造延长时段限价。")
                self._price_rules[rule_id] = [(p.lowEdge, p.increment) for p in entries]
            return self._price_rules[rule_id]
        minimum = getattr(detail, "minTick", None)
        if not self._finite(minimum, positive=True):
            raise BrokerError("合约没有有效最小报价单位，暂不能构造限价。")
        # Older TWS may expose only minTick. IB WhatIf still validates the
        # resulting order; we never retry a rejected price with a guessed grid.
        return [(0, minimum)]

    def _execution_plan(self, detail, action, quote, settings=None):
        session = self._session(detail)
        settings = settings or {}
        right = getattr(detail.contract, "right", settings.get("right", "C"))
        conid = getattr(detail.contract, "conId", None)
        mode, price, seed = resolve_entry_price(settings, right, conid)
        plan = build_entry_plan(action, mode, quote,
                                self._increments(detail) if mode != "MKT" or session.kind == "extended" else [],
                                session_kind=session.kind, limit_price=price, auto_limit=seed,
                                slippage_pct=settings.get("slippage_pct", 5))
        return {**plan, "limit_reseeded": limit_contract_changed(settings, right, conid),
                "session_kind": session.kind,
                "session_label": "延长交易时段" if session.kind == "extended" else "常规交易时段",
                "route": detail.contract.exchange}

    def _selection_spot(self, symbol):
        underlying = self._underlying(symbol)
        try:
            spot = self._quote(underlying, underlying=True, wait=0.3 if symbol in ("SPX", "XSP") else 4)["spot"]
            return spot, "实时标的价格", False
        except BrokerError:
            if symbol not in ("SPX", "XSP"):
                raise
        self._display_spot(underlying)
        reference = getattr(self, "_spot_reference_meta", {})
        if self._fresh_reference(reference):
            return reference["spot"], reference["kind"], True
        raise BrokerError(reference.get("error") or f"{symbol} 无有效实时价格或已校准的实时估算，不能匹配下单。")

    def _fresh_reference(self, reference):
        return (reference.get("anchor_verified") is True and reference.get("estimated") is True
                and reference.get("source") in ("SPY_RATIO_ESTIMATE", "ES_ADJUSTED_ESTIMATE")
                and self._finite(reference.get("spot"), positive=True)
                and self._finite(reference.get("quote_age"))
                and self._finite(reference.get("quote_checked_at"))
                and 0 <= time.monotonic() - reference["quote_checked_at"]
                and reference["quote_age"] + time.monotonic() - reference["quote_checked_at"] <= self.QUOTE_TTL)

    @staticmethod
    def _contract_dict(c):
        return {"conid": c.conId, "symbol": c.symbol,
                "expiry": c.lastTradeDateOrContractMonth[:8], "right": c.right,
                "trading_class": c.tradingClass,
                "strike": float(c.strike), "multiplier": float(c.multiplier),
                "local_symbol": c.localSymbol or f"{c.symbol} {c.lastTradeDateOrContractMonth} {c.right}{c.strike}"}

    def _budget_contract(self, details, spot, right, settings):
        groups = {}
        for detail in details:
            groups.setdefault(detail.contract.strike, []).append(detail)
        ranks = budget_strikes(groups, spot, right, settings)
        cash = self._sync_cash()
        # Keep a match stable during automatic refresh. A click starts a fresh search.
        cache = getattr(self, "_budget_scans", {})
        self._budget_scans = cache
        key = (self.account, self.mode, settings["symbol"], settings["expiry"], settings.get("trading_class"),
               settings.get("budget_search_id"), settings.get("depth"), settings.get("moneyness"),
               settings.get("budget_mode"), settings.get("percent"), settings.get("cash_amount"),
               settings.get("commission_per_contract"), settings.get("min_commission"), settings.get("slippage_pct"),
               settings.get("max_contracts"), settings.get("order_mode"),
               (settings.get("limit_prices") or {}).get(right), settings.get("limit_price") if not settings.get("limit_prices") else None)
        old = cache.get(right, {})
        state = old if old.get("key") == key else {}
        cache[right] = state
        state["key"] = key
        if state.get("match") in groups:
            matched = state["match"]
            rank = next((n for n, strike in ranks if strike == matched), None)
            if rank is not None and len(groups[matched]) == 1:
                contract = groups[matched][0].contract
                try:
                    quote = self._quote(contract, wait=1)
                except BrokerError:
                    quote = {}
                _, limit, _ = resolve_entry_price(settings, right, contract.conId)
                if quote and affordable_size(cash, quote["ask"], contract.multiplier, settings, limit)["quantity"]:
                    return groups[matched][0], rank
            state.pop("match", None)
        first = ranks[0] if ranks else None
        offset = state.get("offset", 0) if state.get("first") == first and state.get("cash") == cash else 0
        state.update(first=first, cash=cash, offset=0)
        for start in range(offset, len(ranks), 12):
            if start >= offset + 36:
                state["offset"] = start
                raise BudgetScanPending(f"已查到虚值第 {ranks[start-1][0]} 档，继续向外查找…")
            batch = [(rank, groups[strike][0].contract) for rank, strike in ranks[start:start + 12]
                     if len(groups[strike]) == 1]
            temporary = set()
            selected = None
            try:
                for _, contract in batch:
                    if contract.conId not in self._tickers:
                        temporary.add(contract.conId)
                    elif min(self._price_times.get(contract.conId, {}).get(k, -1e9) for k in (1, 2)) < time.monotonic() - self.QUOTE_TTL:
                        reset_quote_stream(self, contract)
                    self._subscribe(contract)
                deadline = time.monotonic() + 0.8
                while time.monotonic() < deadline and any(
                        time.monotonic() - self._price_times.get(c.conId, {}).get(2, -1e9) > self.QUOTE_TTL
                        for _, c in batch):
                    self.ib.sleep(.08)
                    self._require()
                for rank, contract in batch:
                    ticker = self._tickers.get(contract.conId)
                    stamps = self._price_times.get(contract.conId, {})
                    if (ticker is None or ticker.marketDataType != 1 or ticker.halted in (1, 2)
                            or not all(self._finite(x, positive=True) for x in (ticker.bid, ticker.ask, ticker.bidSize, ticker.askSize))
                            or ticker.bid > ticker.ask or any(time.monotonic() - stamps.get(k, -1e9) > self.QUOTE_TTL for k in (1, 2))):
                        continue
                    _, limit, _ = resolve_entry_price(settings, right, contract.conId)
                    if affordable_size(cash, ticker.ask, contract.multiplier, settings, limit)["quantity"]:
                        selected = contract.conId
                        state["match"] = contract.strike
                        return groups[contract.strike][0], rank
            finally:
                for conid in temporary - {selected}:
                    release_quote_stream(self, conid)
        raise BrokerError("所查虚值档中，没有取得有效双边报价且预算够买一张的合约。")

    def preview(self, settings):
        self._require()
        settings = copy.deepcopy(settings)
        symbol, expiry, right = settings["symbol"], settings["expiry"], settings["right"]
        detail_list = self._contracts(symbol, expiry, right, settings.get("trading_class", ""))
        action = settings.get("open_action", "BUY")
        if action not in ("BUY", "SELL"):
            raise BrokerError("请选择买入期权或卖出期权。")
        explicit = settings.get("selection_mode") == "strike"
        if explicit:
            spot, spot_source, spot_estimated = None, "指定行权价，无需标的价选档", False
        else:
            spot, spot_source, spot_estimated = self._selection_spot(symbol)
        if settings.get("selection_mode") == "budget":
            detail, rank = self._budget_contract(detail_list, spot, right, settings)
            result = self._preview_open_contract(self._execution_detail(detail), settings, spot, spot_source, spot_estimated)
            result["budget_rank"] = rank
            return result
        strike = select_settings_strike([d.contract.strike for d in detail_list], spot, right, settings)
        candidates = [d for d in detail_list if d.contract.strike == strike]
        conid = (settings.get("conids") or {}).get(right, settings.get("conid"))
        if explicit and conid is not None:
            candidates = [d for d in candidates if d.contract.conId == int(conid)]
        if len(candidates) != 1:
            raise BrokerError("同一行权价有多个交易类别/合约，无法唯一匹配；请换到期日，勿猜测合约。")
        detail = self._execution_detail(candidates[0])
        return self._preview_open_contract(detail, settings, spot, spot_source, spot_estimated)

    def _add_position(self, conid, expected, expected_cost=None):
        records = [p for p in self._position_records(fresh=True, include_short=True)
                   if p.contract.conId == conid]
        if len(records) != 1 or not expected or records[0].position != expected:
            raise BrokerError("持仓数量或方向已变化，请重新选择持仓并预览加仓。")
        if expected_cost is not None and records[0].avgCost != expected_cost:
            raise BrokerError("该持仓的开仓成本已变化，请重新预览加仓。")
        held = self._close_reservations.get(conid)
        if held:
            active = [t for t in self._open_orders() if t.contract.conId == conid]
            if not (self._covered_stops(active, expected) and any(self._same_close_order(held[0], t) for t in active)):
                self._check_close_reservation(conid, abs(expected))
        return records[0]

    def preview_add(self, conid, expected, request):
        from position_add import add_settings
        self._require()
        position = self._add_position(conid, expected)
        if self._opening_blocked(conid):
            raise BrokerError("有占用资金的挂单，或该合约已有委托，请在订单页核对。")
        basis = None
        if request["method"] == "principal" and expected > 0 and not request.get("principal"):
            basis = self._get_history_store().position_opening_cost(self.account, self.mode, conid, expected)
        settings, info = add_settings(request, expected, basis)
        info["position_avg_cost"] = position.avgCost
        detail = self._close_detail(position)
        contract = detail.contract
        settings.update(right=contract.right, expiry=contract.lastTradeDateOrContractMonth[:8])
        return self._preview_open_contract(detail, settings, None, "选中持仓", False, info)

    def _preview_open_contract(self, detail, settings, spot, spot_source, spot_estimated, add_info=None):
        from position_add import check_add_size, short_budget_lots
        right, expiry = settings["right"], settings["expiry"]
        action = settings.get("open_action", "BUY")
        contract = detail.contract
        if (contract.right != right or contract.lastTradeDateOrContractMonth[:8] != expiry
                or contract.currency != "USD" or str(contract.multiplier) != "100"):
            raise BrokerError("合约与所选到期日、方向或币种不一致，未生成预览。")
        self._details[contract.conId] = detail
        session = self._session(detail)
        if spot_estimated and session.kind != "extended":
            raise BrokerError("常规时段需真实指数报价；SPY/ES 估算仅用于延长时段选档。")
        quote = self._quote(contract)
        plan = self._execution_plan(detail, action, quote, settings)
        cash = self._sync_cash()
        if spot_estimated and (not plan["outside_rth"] or not self._fresh_reference(self._spot_reference_meta)):
            raise BrokerError("估算参考已过期或进入常规时段，请重新匹配；未发单。")
        if add_info:
            plan.update(add_info)
        if action == "SELL":
            if add_info and settings["budget_mode"] == "amount":
                single = self._preview_short(detail, {**settings, "seller_quantity": 1}, plan, quote)
                settings["seller_quantity"] = short_budget_lots(settings, single)
            sized = self._preview_short(detail, settings, plan, quote)
        else:
            self._reject_short_for_long_open(contract)
            sized = self._plan_size(cash, quote, contract, settings, plan)
        if add_info:
            check_add_size(sized, add_info)
        token = uuid.uuid4().hex
        result = {"token": token, "contract": self._contract_dict(contract),
                  "spot": spot, "bid": quote["bid"], "ask": quote["ask"],
                  "quote_type": "LIVE / 实时期权", "cash": cash, **sized, **plan,
                  "action": action, "opens_short": action == "SELL",
                  "spot_source": spot_source, "spot_estimated": spot_estimated,
                  "created_at": time.time(), "mode": self.mode, "account": self.account}
        self._tokens = {k: v for k, v in self._tokens.items() if time.monotonic() - v[2] <= self.TOKEN_TTL}
        self._tokens[token] = (copy.deepcopy(result), settings, time.monotonic())
        return result

    def preview_pair(self, settings):
        """Two independent immutable previews; an unavailable side cannot mask the other."""
        result = {}
        for right in ("C", "P"):
            try:
                result[right] = self.preview({**copy.deepcopy(settings), "right": right})
            except BudgetScanPending as exc:
                result[right] = {"error": str(exc), "scan_pending": True}
            except Exception as exc:
                result[right] = {"error": str(exc)}
        # The second side can take longer than TOKEN_TTL. Revalidate each
        # completed side from current live ticks, calendar and shared fresh
        # funds rather than extending an old token blindly. LMT/MID stays fixed.
        try:
            cash = self._sync_cash(force=True)
        except Exception as exc:
            for value in result.values():
                self._tokens.pop(value.get("token"), None)
            return {right: {"error": str(exc)} for right in ("C", "P")}
        for right in ("C", "P"):
            previous = result[right]
            if "token" not in previous:
                continue
            self._tokens.pop(previous["token"], None)
            current_settings = {**copy.deepcopy(settings), "right": right}
            try:
                detail = self._details[previous["contract"]["conid"]]
                self._verify_short_plan(detail, previous)
                quote = self._quote(detail.contract, wait=0)
                if previous.get("spot_estimated") and not self._fresh_reference(self._spot_reference_meta):
                    raise BrokerError("选档估算参考已过期，请刷新预览。")
                update = {} if previous["opens_short"] else self._plan_size(cash, quote, detail.contract, current_settings, previous)
                value = {**previous, **update, "bid": quote["bid"], "ask": quote["ask"],
                         "cash": cash, "created_at": time.time(), "token": uuid.uuid4().hex}
                if previous["order_type"] == "MKT":
                    value["sizing_price"] = quote["bid" if previous["opens_short"] else "ask"]
                    if previous["opens_short"]:
                        value["estimated_credit"] = value["estimated_premium"] = quote["bid"] * previous["quantity"] * float(detail.contract.multiplier)
                self._tokens[value["token"]] = (copy.deepcopy(value), current_settings, time.monotonic())
                result[right] = value
            except Exception as exc:
                result[right] = {"error": str(exc)}
        return result

    def _reject_short_for_long_open(self, contract):
        if any(p.contract.conId == contract.conId and p.position < 0
               for p in self._position_records(fresh=True, include_short=True)):
            raise BrokerError("该合约已有空头，请先用买回平仓管理；买入开仓不会自动回补或反手。")

    def _plan_size(self, cash, quote, contract, settings, plan):
        if settings.get("selection_mode") == "budget":
            return affordable_size(cash, quote["ask"], contract.multiplier, settings, plan.get("limit_price"))
        if plan["order_type"] == "LMT":
            return self._size(cash, plan["limit_price"], contract, {**settings, "slippage_pct": 0})
        return self._size(cash, quote["ask"], contract, settings)

    @staticmethod
    def _size(cash, ask, contract, settings):
        return size_order(cash, settings["percent"], ask, contract.multiplier,
                          slippage_pct=settings.get("slippage_pct", 5),
                          commission_per_contract=settings.get("commission_per_contract", 1),
                          min_commission=settings.get("min_commission", 1),
                          max_contracts=settings.get("max_contracts", 10000),
                          rounding_mode=settings.get("rounding_mode", "nearest"),
                          budget_mode=settings.get("budget_mode", "percent"),
                          cash_amount=settings.get("cash_amount"))

    @staticmethod
    def _order_identity(trade):
        o = trade.order
        return o.clientId, o.orderId, getattr(o, "permId", 0)

    def _download_open_orders(self):
        fresh = self.ib.reqAllOpenOrders()
        if not isinstance(fresh, (list, tuple)):
            raise BrokerError("券商挂单列表尚未返回，请刷新持仓后重试。")
        visible = {self._order_identity(t) for t in fresh if t.order.account == self.account}
        self._missing_external_orders = {self._order_identity(t) for t in self.ib.trades()
            if t.order.account == self.account and not self._ours(t) and not t.isDone()} - visible
        return fresh

    def _open_orders(self):
        self._require()
        fresh = self._download_open_orders()
        # Keep our locally PendingSubmit orders too: TWS may not yet list them.
        found = {self._order_identity(t): t for t in fresh
                 if t.order.account == self.account and not t.isDone()}
        for t in self.ib.trades():
            if (t.order.account == self.account and self._ours(t) and not t.isDone()
                    and not self._inactive_close_reconciled(t)):
                found[self._order_identity(t)] = t
        return list(found.values())

    def _opening_blocked(self, conid, orders=None):
        orders = self._open_orders() if orders is None else orders
        if not orders:
            return False
        by_contract = {}
        for trade in orders:
            if not self._working_sell_stop(trade):
                return True
            by_contract.setdefault(trade.contract.conId, []).append(trade)
        holdings = {p.contract.conId: p.position for p in self._position_records(fresh=True)}
        return any(not self._covered_stops(stops, holdings.get(cid, 0)) for cid, stops in by_contract.items())

    def buy(self, token):
        self._require()
        saved = self._tokens.pop(token, None)
        if saved is None:
            raise BrokerError("预览已失效或已经使用，请刷新后再点击。")
        preview, settings, created = saved
        if time.monotonic() - created > self.TOKEN_TTL:
            raise BrokerError("预览超过 10 秒，请刷新后再点击。")
        if self._opening_blocked(preview['contract']['conid']):
            raise BrokerError("账户存在未完成订单，占用资金或涉及当前合约，请先核对。")
        detail = self._execution_detail(self._details[preview["contract"]["conid"]])
        session = self._session(detail)
        if (session.kind != preview.get("session_kind", "extended" if preview["outside_rth"] else "regular") or detail.contract.exchange != preview["route"]
                or (preview.get("spot_estimated") and session.kind != "extended")):
            raise BrokerError("预览后交易时段或路由改变，请重新匹配。")
        cash = self._sync_cash(force=True)
        self._check_short_margin_hold()
        self._check_cover_cash_hold()
        if self._debit_reserve:
            raise BrokerError("上一笔开仓的现金扣款尚未确认；请稍后刷新，再次开仓。程序不会重复使用该笔现金。")
        if preview.get("add_method"):
            self._add_position(detail.contract.conId, preview["position_quantity"], preview["position_avg_cost"])
        quote = self._quote(detail.contract)
        if preview.get("opens_short"):
            return self._send_short_open(detail, preview, settings, created + self.TOKEN_TTL)
        self._reject_short_for_long_open(detail.contract)
        if preview["order_type"] == "LMT" and preview.get("price_mode", "MKT") == "MKT" and quote["ask"] > preview["limit_price"]:
            raise BrokerError("最新卖一已超过预览限价，请重新匹配；未发单。")
        size = self._plan_size(cash, quote, detail.contract, settings, preview)
        quantity = min(preview["quantity"], size["quantity"])
        if preview.get("add_method") and quantity != preview["quantity"]:
            raise BrokerError("可加仓张数已变化，请重新预览。")
        if quantity < 1:
            raise BrokerError("最新现金/卖价下不足 1 张，未提交订单。")
        return self._send(detail, "BUY", quantity, settings=settings, cash=cash,
                          budget=min(preview["budget"], size["budget"]),
                          deadline=created + self.TOKEN_TTL, plan=preview)

    def _position_records(self, fresh=False, include_short=False):
        self._require()
        if fresh:
            # The active subscription delivers deltas. Restart it for a full book.
            self.ib.client.cancelPositions()
            updates = self._position_download_updates = {}
            try:
                positions = self.ib.reqPositions()
            finally:
                self._position_download_updates = None
            if not isinstance(positions, (list, tuple)):
                raise BrokerError("持仓下载未返回完整列表，未更新持仓缓存。")
            latest = {(p.account, p.contract.conId): p for p in positions}
            # positionEnd and subsequent live changes can share one network read.
            latest.update(updates)
            positions = list(latest.values())
            for position in positions:
                self._observe_open_position(position)
            # reqPositions ends with a complete snapshot. ib_async updates
            # individual rows but does not prune absent rows from an older
            # cache. Reconcile ONLY this account after a successful download.
            books = getattr(getattr(self.ib, "wrapper", None), "positions", None)
            if isinstance(books, dict):
                books[self.account] = {p.contract.conId: p for p in positions
                                       if p.account == self.account and p.position != 0}
        else:
            positions = self.ib.positions(self.account)
        return [p for p in positions if p.account == self.account
                and p.contract.secType == "OPT"
                and p.contract.currency == "USD" and str(p.contract.multiplier) == "100"
                and (p.position != 0 if include_short else p.position > 0)]

    def positions(self):
        return self._position_rows()

    def close(self, conid, percent, order_spec=None, cost_guard=None, protection_id=None):
        if protection_id is not None:
            if order_spec is not None or cost_guard is not None:
                raise BrokerError("撤保护单只适用于所选持仓平仓。")
            preview = self.preview_close(conid, percent, protection_id=protection_id)
            return self.close_confirm(preview["token"])
        self._require()
        if int(conid) in self._close_reservations:
            self._refresh_close_reservations()
        active = [t for t in self._open_orders() if t.contract.conId == int(conid)]
        if active:
            if order_spec is None and cost_guard is None and all(self._working_sell_stop(t) and self._ours(t) for t in active):
                preview = self.preview_close(conid, percent, protection_id=self._protection_ids(active))
                return self.close_confirm(preview["token"])
            raise BrokerError("该合约已有未完成订单，请等待成交或撤单完成后再平仓。")
        records = [p for p in self._position_records(fresh=True, include_short=True) if p.contract.conId == int(conid)]
        self._check_close_reservation(int(conid), abs(float(records[0].position)) if len(records) == 1 else 0.0)
        if len(records) != 1:
            raise BrokerError("账户中没有唯一匹配的已加载标的标准期权持仓。")
        p = records[0]
        quantity = close_quantity(abs(p.position), percent)
        if p.position < 0 and order_spec is not None:
            raise BrokerError("空头持仓请使用快捷买回平仓；自选止损/移动卖单只适用于多头。")
        contract = copy.copy(p.contract)
        contract.exchange = "SMART"
        # Refresh the liquid-hours calendar for closes, including holdings
        # created outside this program or retained across midnight.
        details = self.ib.reqContractDetails(contract)
        details = [d for d in details if d.contract and d.contract.conId == int(conid)]
        if len(details) != 1:
            raise BrokerError("无法确认平仓合约及交易时段。")
        details[0].contract.exchange = "SMART"
        self._details[int(conid)] = details[0]
        if p.position < 0:
            return self._send_short_cover(self._execution_detail(details[0]), quantity)
        extra = {"cost_guard": cost_guard} if cost_guard is not None else {}
        return self._send(self._execution_detail(details[0]), "SELL", quantity, exit_spec=order_spec, **extra)

    def _check_close_reservation(self, conid, observed_position):
        held = self._close_reservations.get(conid)
        if held is None:
            return
        if self._close_reservation_proven(held, observed_position):
            self._close_reservations.pop(conid, None)
            return
        self._refresh_close_reservations()
        if conid not in self._close_reservations:
            return
        raise BrokerError("上一笔平仓的完整成交及持仓扣减尚未确认；已尝试重新同步。请点击刷新持仓或到订单页核对，避免重复平仓变成反向开仓。")

    def close_all(self, symbol=None):
        self._require()
        if not symbol:
            raise BrokerError("请明确选择要全部平仓的标的。")
        symbol = normalize_symbol(symbol)
        canonical = self._underlyings[symbol].symbol if symbol in self._underlyings else symbol
        targets = [p for p in self._position_records(fresh=True, include_short=True) if p.contract.symbol == canonical]
        results = []
        for p in targets:
            try:
                result = self.close(p.contract.conId, 100)
                results.append(result)
                if result["status"] in ("Inactive", "Cancelled", "ApiCancelled", "UNKNOWN"):
                    raise BrokerError(f"订单状态 {result['status']}")
            except Exception as exc:
                raise BrokerError(f"批量平仓已提交 {len(results)} 个合约；其余停止。请核对订单和持仓。原因：{exc}") from exc
        return results

    def _preflight_order(self, contract, order):
        state = self.ib.whatIfOrder(contract, order)
        if state.warningText:
            raise BrokerError(f"IBKR 预检提示，未发单：{state.warningText}")
        return state

    def _send(self, detail, action, quantity, settings=None, cash=None, budget=None, deadline=None, plan=None, exit_spec=None, cost_guard=None):
        from ib_async import MarketOrder, LimitOrder
        self._require()
        session = self._session(detail)
        contract = detail.contract
        custom_exit = exit_spec is not None
        if custom_exit:
            from exit_orders import validate_exit_spec, build_exit_order
            if action != "SELL":
                raise BrokerError("自选卖法仅用于平仓。")
            exit_spec = validate_exit_spec(exit_spec, self._increments(detail))
            market_trigger = exit_spec["type"] in ("STP", "TRAIL")
            if market_trigger and session.kind != "regular":
                raise BrokerError("触发市价的止损/移动卖单本版仅在合约常规时段提交及触发；延长时段请选择限价型卖单。")
            if market_trigger:
                self._quote(contract, required_side="bid")
            else:
                self._quote(contract)
            order = build_exit_order(exit_spec, int(quantity), self.account,
                                     self.ORDER_PREFIX + ("PS:" if cost_guard is not None else "") + uuid.uuid4().hex[:16],
                                     outside_rth=not market_trigger)
            plan = dict(order_type=exit_spec["type"], limit_price=exit_spec.get("limit_price"),
                        outside_rth=order.outsideRth, route=contract.exchange,
                        session_label="延长交易时段" if session.kind == "extended" else "常规交易时段")
        elif plan is None:
            opening_quote = self._quote(contract, required_side='bid') if action == 'SELL' else self._quote(contract)
            plan = self._execution_plan(detail, action, opening_quote, settings)
        if not custom_exit and (plan.get("session_kind", "extended" if plan["outside_rth"] else "regular") != session.kind or plan["outside_rth"] != session.outside_rth
                or plan["route"] != contract.exchange):
            raise BrokerError("交易时段或路由已变化，请重新匹配后发单。")
        common = dict(account=self.account, tif="DAY", outsideRth=plan["outside_rth"],
                      transmit=True, orderRef=self.ORDER_PREFIX + uuid.uuid4().hex[:16])
        if custom_exit:
            pass  # Exact user-selected fields; never convert a stop into a market order.
        elif plan["order_type"] == "LMT":
            if not self._finite(plan.get("limit_price"), positive=True):
                raise BrokerError("延长时段需要明确有效限价。")
            order = LimitOrder(action, int(quantity), plan["limit_price"], **common)
        else:
            order = MarketOrder(action, int(quantity), **common)
        def verify_session():
            latest = self._session(detail)
            if custom_exit:
                if exit_spec["type"] in ("STP", "TRAIL") and latest.kind != "regular":
                    raise BrokerError("预检期间离开常规时段，条件卖单未提交。")
            elif latest.kind != plan.get("session_kind", "extended" if plan["outside_rth"] else "regular") or latest.outside_rth != plan["outside_rth"]:
                raise BrokerError("预检期间交易时段改变，请刷新；未发单。")
        # This credit/permission check is not an order. Never suppress warnings
        # or IB precautionary rules automatically.
        state = self._preflight_order(contract, order)
        verify_session()
        reserve = 0.0
        if action == "BUY":
            # A manual fill during WhatIf may reduce cash without leaving an
            # open order. Refresh once more before calculating the final size.
            cash = self._sync_cash(force=True)
            if plan.get("add_method"):
                self._add_position(contract.conId, plan["position_quantity"], plan["position_avg_cost"])
            else:
                self._reject_short_for_long_open(contract)
            quote = self._quote(contract, wait=0)
            # Refreshing the last quote may only decrease a displayed quantity.
            if plan["order_type"] == "LMT" and plan.get("price_mode", "MKT") == "MKT" and quote["ask"] > plan["limit_price"]:
                raise BrokerError("最新卖一价已超过预览限价，请刷新；未发单。")
            sized = self._plan_size(cash, quote, contract, settings, plan)
            budget = min(budget, sized["budget"])
            if settings.get("selection_mode") != "budget" and settings.get("rounding_mode", "nearest") == "nearest" and settings.get("budget_mode", "percent") == "percent":
                budget = cash  # Target percentage is not a hard cap in rounding mode.
            quantity = min(int(quantity), sized["quantity"])
            commission = max(settings.get("min_commission", 1),
                             quantity * settings.get("commission_per_contract", 1))
            if self._finite(state.commission) and state.commission < 1e100:
                if state.commissionCurrency not in ("", "USD"):
                    raise BrokerError("预检手续费币种不是 USD，未发单。")
                commission = max(commission, float(state.commission))
            unit = (plan["limit_price"] * float(contract.multiplier) if plan["order_type"] == "LMT"
                    else quote["ask"] * float(contract.multiplier) * (1 + settings.get("slippage_pct", 5) / 100))
            # Use a full-order conservative commission estimate even if the
            # quantity is reduced after WhatIf.
            while quantity > 0 and unit * quantity + commission > budget + 1e-9:
                quantity -= 1
            if quantity < 1:
                raise BrokerError("预检后最新价格/手续费超出预算，未发单。")
            if plan.get("add_method") and quantity != plan["quantity"]:
                raise BrokerError("预检后的资金或费用不足以提交已确认的加仓张数，请重新预览。")
            order.totalQuantity = quantity
            reserve = unit * quantity + commission
        else:
            # Re-read holdings after WhatIf so a fill/manual close during the
            # check cannot knowingly turn this close into a short opening.
            current = [p for p in self._position_records(fresh=True)
                       if p.contract.conId == contract.conId]
            if len(current) != 1 or current[0].position < quantity:
                raise BrokerError("预检期间持仓发生变化，请刷新后重新平仓。")
            if plan.get("close_intent") and float(current[0].position) != plan["expected_position"]:
                raise BrokerError("预检期间持仓数量变化，请重新预览平仓；未发单。")
            self._check_close_reservation(contract.conId, float(current[0].position))
            close_baseline = float(current[0].position)
        open_orders = self._open_orders()
        if ((action == "BUY" and self._opening_blocked(contract.conId, open_orders))
                or (action == "SELL" and any(t.contract.conId == contract.conId for t in open_orders))):
            raise BrokerError("预检期间出现该合约的新订单，未发单。")
        self._require()
        verify_session()
        if deadline is not None and time.monotonic() > deadline:
            raise BrokerError("发单预检超过预览有效期，请刷新后重试；尚未发单。")
        if action == "BUY":
            if deadline is not None and time.monotonic() > deadline:
                raise BrokerError("发单预检耗时超过预览有效期，请刷新预览重试；尚未发单。")
            final_quote = self._quote(contract, wait=0)
            ceiling = plan["limit_price"] if plan["order_type"] == "LMT" else quote["ask"]
            if plan.get("price_mode", "MKT") == "MKT" and final_quote["ask"] > ceiling:
                raise BrokerError("预检期间卖一价继续上涨，请刷新张数后再开仓；尚未发单。")
        elif custom_exit:
            # A take-profit limit is intentionally allowed to rest above bid.
            # Conditional orders are broker-managed after the user submits;
            # this application never fires an additional local sell on a tick.
            if market_trigger:
                bid = self._quote(contract, wait=0, required_side="bid")["bid"]
                if cost_guard is not None:
                    from exit_orders import check_cost_stop, cost_stop_quote
                    actual = cost_stop_quote(current[0].avgCost, contract.multiplier, close_baseline, bid, self._increments(detail), cost_guard.get("multiple", 1))
                    check_cost_stop(cost_guard, actual, exit_spec, quantity)
            else:
                self._quote(contract, wait=0)
        elif action == "SELL":
            wait = min(2., max(0., deadline - time.monotonic())) if deadline is not None else 2.
            final_quote = self._quote(contract, wait=wait, required_side="bid" if plan.get("price_mode", "MKT") == "MKT" else None)
            if deadline is not None and time.monotonic() >= deadline:
                raise BrokerError("盘口刷新时预览已过期，请重新预览。")
            if (plan["order_type"] == "LMT" and plan.get("price_mode", "MKT") == "MKT"
                    and final_quote["bid"] < plan["limit_price"]):
                raise BrokerError("预检期间买一价已低于平仓限价，请刷新后重试；尚未发单。")
        self._tokens.clear()
        getattr(self, "_close_tokens", {}).clear()
        # There is deliberately no retry after this line. A socket exception
        # may occur after the server received the order.
        try:
            trade = self.ib.placeOrder(contract, order)
        except Exception as exc:
            self._uncertain = True
            raise BrokerError("发单结果不明确，已锁定。请到 TWS 核对；禁止直接重试。") from exc
        if action == "BUY":
            self._record_buy_against_stop(trade)
        if reserve:
            self._order_sizing_settings[trade.order.orderId] = {
                "slippage_pct": settings.get("slippage_pct", 5),
                "commission_per_contract": settings.get("commission_per_contract", 1),
                "min_commission": settings.get("min_commission", 1),
            }
            if self._reserve_cash_baseline is None:
                self._reserve_cash_baseline = self._raw_cash
                self._reserve_usd_baseline = getattr(self, "_financial", {}).get("usd_cash")
            self._debit_reserve += reserve
            self._reserve_trade = trade
            self._cash = max(0.0, self._cash - reserve)
        if action == "SELL":
            self._close_reservations[contract.conId] = (trade, close_baseline)
        self._dirty_account = True
        if custom_exit:
            pricing = f"{exit_spec['type']} {exit_spec}"
        else:
            pricing = f"限价 {plan['limit_price']:g}" if plan["order_type"] == "LMT" else "市价"
        self.log(f"已提交 {self.mode} {action} {int(order.totalQuantity)} 张 {contract.localSymbol} "
                 f"{pricing} {order.tif} / {plan['session_label']} / {contract.exchange}；提交不代表成交。")
        deadline = time.monotonic() + 3
        while trade.orderStatus.status in ("PendingSubmit", "ApiPending") and time.monotonic() < deadline:
            self.ib.sleep(0.05)
        if trade.orderStatus.status in ("PendingSubmit", "ApiPending"):
            self.log("订单仍等待确认；请查看订单状态，程序不会重发。")
        if (action == "BUY" and trade.orderStatus.status in ("Cancelled", "ApiCancelled", "Inactive")
                and not trade.fills and float(trade.orderStatus.filled) == 0):
            self._debit_reserve = max(0.0, self._debit_reserve - reserve)
            if self._debit_reserve == 0:
                self._reserve_cash_baseline = None
                self._reserve_trade = None
        return self._order_dict(trade)

    def _order_dict(self, trade):
        o, s, c = trade.order, trade.orderStatus, trade.contract
        def number(name):
            value = getattr(o, name, None)
            try:
                return float(value) if 0 <= float(value) < 1e100 else None
            except (TypeError, ValueError):
                return None
        trailing = o.orderType in ("TRAIL", "TRAIL LIMIT")
        percent = number("trailingPercent") if trailing else None
        trail_mode = ("percent" if percent is not None else "amount") if trailing else None
        owned = o.account == self.account and self._ours(trade)
        amendment_pending = o.orderId in self._amend_pending
        modifiable = (owned and not amendment_pending and s.status in ("Submitted", "PreSubmitted")
                      and o.orderType in ("MKT", "LMT")
                      and not self._short_order_role(o)
                      and not (o.parentId or o.ocaGroup or o.conditions or o.algoStrategy or o.hedgeType))
        warning = self._order_warnings.get(o.orderId, "")
        if amendment_pending:
            warning = "改单/撤单结果待确认，已暂停再次改单；请在 TWS 核对。 " + warning
        if self._short_order_role(o):
            warning = "卖方开仓/买回订单暂不快捷改单；可撤单确认后重新预检下单。 " + warning
        return {"order_id": o.orderId, "action": o.action,
                "conid": c.conId, "parent_id": o.parentId, "oca_group": o.ocaGroup,
                "opens_short": self._short_order_role(o) == "OPEN_SHORT",
                "quantity": float(o.totalQuantity), "filled": float(s.filled),
                "remaining": float(s.remaining), "status": s.status,
                "local_symbol": c.localSymbol or c.symbol,
                "avg_fill_price": float(s.avgFillPrice), "account": o.account,
                "order_type": o.orderType, "limit_price": number("lmtPrice") if o.orderType in ("LMT", "STP LMT") else None,
                "stop_price": number("auxPrice") if o.orderType in ("STP", "STP LMT") else number("trailStopPrice") if trailing else None,
                "trail_mode": trail_mode,
                "trail_value": (percent if percent is not None else number("auxPrice")) if trailing else None,
                "limit_offset": number("lmtPriceOffset") if o.orderType == "TRAIL LIMIT" else None,
                "tif": o.tif,
                "outside_rth": o.outsideRth,
                "execution_warning": warning,
                "position_effect": self._short_order_role(o) or ("OPEN_LONG" if o.action == "BUY" else "CLOSE_LONG"),
                "owned": owned, "modifiable": modifiable,
                "order_ref": o.orderRef, "client_id": o.clientId}

    def _display_orders(self):
        rows = []
        active_quotes = {}
        placement_times = getattr(self, "_placement_times", {})
        self._placement_times = placement_times
        for trade in self.ib.trades():
            order, contract = trade.order, trade.contract
            if order.account != self.account or (not self._ours(trade) and (
                    trade.isDone() or self._order_identity(trade) in getattr(self, "_missing_external_orders", set()))):
                continue
            row = self._order_dict(trade)
            row.update(symbol=contract.symbol, expiry=contract.lastTradeDateOrContractMonth[:8],
                       strike=contract.strike, right=contract.right, trading_class=contract.tradingClass,
                       perm_id=getattr(order, "permId", 0))
            log = getattr(trade, "log", [])
            first = log[0] if log else None
            stamp = first.time.timestamp() if first and first.time.tzinfo is not None else None
            submitted = stamp if first and first.status == "PendingSubmit" and not first.message else None
            if row["owned"] and order.orderRef:
                # Replacement orders may inherit an orderRef. Bind to the client order ID too.
                ref = f"{order.clientId}/{order.orderId}/{order.orderRef}"
                key = (self.account, self.mode, ref)
                if key not in placement_times or (placement_times[key] is None and submitted is not None):
                    try:
                        placement_times[key] = self._get_history_store().submission_time(self.account, self.mode, ref, submitted)
                    except sqlite3.Error as exc:
                        self.log(f"订单时间暂用本次会话记录，保存失败（{type(exc).__name__}）。")
                        placement_times[key] = submitted
                submitted = placement_times[key]
            row.update(placed_at=submitted, observed_at=stamp)
            if not trade.isDone() and contract.secType == "OPT" and contract.conId:
                if contract.conId not in active_quotes:
                    active_quotes[contract.conId] = self._position_quote(contract)
                row["quote"] = active_quotes[contract.conId]
            rows.append(row)
        return rows

    def orders(self):
        return [self._order_dict(t) for t in self.ib.trades()
                if t.order.account == self.account and self._ours(t)]

    def cancel_own(self):
        self._require()
        targets = [t for t in self.ib.openTrades()
                   if t.order.account == self.account and self._ours(t)]
        for t in targets:
            self.ib.cancelOrder(t.order)
        self._tokens.clear()
        self.log(f"已请求撤销本程序 {len(targets)} 笔订单；请等待 Cancelled 回报。")
        return len(targets)

    def snapshot(self):
        connected = self.ib.isConnected() and bool(self.account)
        # No network round-trip here; called frequently by the GUI.
        financial = copy.deepcopy(self._financial) if connected else {"ready": False, "cash": None, "error": "已断开"}
        pnl = getattr(self, "_account_pnl", None)
        value = getattr(pnl, "dailyPnL", None)
        if connected and getattr(pnl, "account", None) == self.account and isinstance(value, (int, float)) and math.isfinite(value) and abs(value) < 1e100:
            financial["daily_pnl"] = value
        if financial.get("ready"):
            financial["cash_before_local_hold"] = self._raw_cash
            financial["cash"] = self._cash
        if self._uncertain:
            financial.update(ready=False, cash=None, error="订单结果待核对，发单已锁定")
        return {"mode": self.mode, "account": self.account, "cash": financial.get("cash"),
                "positions": self.positions() if connected and not self._uncertain else [],
                "orders": self._display_orders() if connected else [], "connected": connected,
                "position_sync_pending": connected and bool(self._position_sync_times),
                "position_open_unconfirmed": connected and bool(self._unconfirmed_opens),
                "locked": self._uncertain, "cash_reserve": self._debit_reserve,
                "position_scope": "当前账户全部标准美元期权（乘数100），不随当前标的切换隐藏",
                "financial": financial}
