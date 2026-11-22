"""Qualified option-chain discovery and read-only streaming chain display.

Display fallbacks (last/previous close) never enter an order's quote path.
The mixin expects the owning broker to provide _require, _subscribe, _quote,
_finite and the ib_async IB instance. No code in this module submits orders.
"""
from __future__ import annotations

from datetime import datetime
from collections import Counter
import math
import re
import time
from zoneinfo import ZoneInfo

from core import normalize_symbol
from market_runtime import (CHAIN_STRIKE_BUDGET, MAX_DISPLAY_STRIKES,
                            pinned_quote_ids, release_quote_stream, selection_expiry_floor)


class MarketDataError(RuntimeError):
    pass


def _positive_contract_id(value):
    """Normalize the wire string/int representation without accepting floats.

    ib_async 2.1.0's option-chain decoder leaves underlyingConId as a string,
    despite OptionChain declaring an int annotation. Contract details usually
    contain ints. Compare their numeric identities, not Python runtime types.
    """
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    text = str(value).strip()
    if not re.fullmatch(r"[0-9]+", text):
        return None
    result = int(text)
    return result if result > 0 else None


def _same_contract_id(actual, expected):
    actual_id, expected_id = _positive_contract_id(actual), _positive_contract_id(expected)
    return actual_id is not None and expected_id is not None and actual_id == expected_id


def chain_page(strikes, spot, offset=0, limit=40):
    """A display window; zero means all rows, independently of subscriptions."""
    if isinstance(offset, bool) or not isinstance(offset, (int, float)) or not math.isfinite(offset) or int(offset) != offset:
        raise MarketDataError("期权链翻页偏移必须为整数。")
    if (isinstance(limit, bool) or not isinstance(limit, (int, float)) or not math.isfinite(limit)
            or int(limit) != limit or not 0 <= int(limit) <= MAX_DISPLAY_STRIKES):
        raise MarketDataError(f"每页档数应为 1 到 {MAX_DISPLAY_STRIKES} 的整数，或选择所有档数。")
    strikes = sorted(set(float(x) for x in strikes if math.isfinite(float(x)) and float(x) > 0))
    if not strikes:
        return [], 0, False, False
    limit = int(limit)
    if spot is None or not math.isfinite(float(spot)) or float(spot) <= 0:
        return [], 0, False, False
    if limit == 0:
        return strikes, 0, False, False
    # Boundary is between strictly lower strikes and strikes at/above spot,
    # so both sides occupy half the page around the ITM/OTM divider.
    center = next((i for i, strike in enumerate(strikes) if strike >= spot), len(strikes))
    base = max(0, min(center - limit // 2, len(strikes) - limit))
    start = max(0, min(base + int(offset), max(0, len(strikes) - limit)))
    return strikes[start:start + limit], start - base, start > 0, start + limit < len(strikes)


class MarketDataMixin:
    def _underlying(self, symbol):
        self._require()
        symbol = normalize_symbol(symbol)
        if symbol not in self._underlyings:
            from ib_async import Index, Stock
            if symbol in ("SPX", "XSP"):
                contract = Index(symbol, "CBOE", "USD")
            else:
                primary = {"SPY": "ARCA", "QQQ": "NASDAQ"}.get(symbol, "")
                contract = Stock(symbol, "SMART", "USD", primaryExchange=primary)
            qualified = self.ib.qualifyContracts(contract)
            if len(qualified) != 1 or not qualified[0] or qualified[0].conId <= 0:
                raise MarketDataError(f"无法唯一确认 {symbol} 的美元股票/ETF 或指定指数合约。")
            contract = qualified[0]
            expected = "IND" if symbol in ("SPX", "XSP") else "STK"
            if contract.secType != expected or contract.currency != "USD":
                raise MarketDataError("标的合约类型或币种与请求不符，停止匹配。")
            self._underlyings[symbol] = contract
        if not hasattr(self, "_watched_symbols"):
            self._watched_symbols = set()
        self._watched_symbols.add(symbol)
        self._watched_symbols.add(self._underlyings[symbol].symbol)
        return self._underlyings[symbol]

    def _load_chains(self, symbol):
        symbol = normalize_symbol(symbol)
        underlying = self._underlying(symbol)
        today = datetime.now(ZoneInfo("America/New_York")).strftime("%Y%m%d")
        key = (symbol, today)
        if key not in self._chains:
            received = self.ib.reqSecDefOptParams(underlying.symbol, "", underlying.secType, underlying.conId)
            chains, rejected = [], Counter()
            for chain in received:
                if str(chain.multiplier) != "100":
                    rejected["非100乘数"] += 1
                elif not _same_contract_id(chain.underlyingConId, underlying.conId):
                    rejected["标的ID不匹配或无效"] += 1
                elif (not chain.tradingClass
                      or (symbol == "SPX" and chain.tradingClass not in ("SPX", "SPXW"))
                      or (symbol == "XSP" and chain.tradingClass not in ("XSP", "XSPW"))):
                    rejected["类别不匹配"] += 1
                else:
                    chains.append(chain)
            reasons = "、".join(f"{reason} {count}" for reason, count in rejected.items()) or "无"
            if callable(getattr(self, "log", None)):
                self.log(f"{symbol} 期权链校验：返回 {len(received)} 条，匹配 {len(chains)} 条；排除：{reasons}。")
            if not chains:
                if received:
                    raise MarketDataError(f"{symbol} 返回的 {len(received)} 条期权链均未通过校验：{reasons}。")
                raise MarketDataError(f"TWS 未返回 {symbol} 的期权链，请核对标的和账户权限。")
            self._chains[key] = chains
        return self._chains[key]

    def expirations(self, symbol):
        today = selection_expiry_floor(symbol)
        return sorted({expiry for chain in self._load_chains(symbol) for expiry in chain.expirations
                       if re.fullmatch(r"\d{8}", expiry) and expiry >= today})

    def trading_classes(self, symbol, expiry):
        symbol = normalize_symbol(symbol)
        if expiry not in self.expirations(symbol):
            if isinstance(expiry, str) and re.fullmatch(r"\d{8}", expiry) and expiry < selection_expiry_floor(symbol):
                raise MarketDataError(f"{expiry} 到期合约已过最后交易时段，请选择下一有效到期日。")
            raise MarketDataError("请选择当前标的有效的到期日。")
        values = {c.tradingClass for c in self._load_chains(symbol) if expiry in c.expirations}
        preferred = "SPXW" if symbol == "SPX" else symbol.replace(" ", "")
        return sorted(values, key=lambda value: (value != preferred, value))

    def _choose_class(self, symbol, expiry, trading_class=""):
        classes = self.trading_classes(symbol, expiry)
        if trading_class:
            if trading_class not in classes:
                raise MarketDataError("所选期权类别不属于当前标的和到期日，请重新选择。")
            return trading_class
        preferred = "SPXW" if symbol == "SPX" else symbol.replace(" ", "")
        if preferred in classes:
            return preferred
        if len(classes) == 1:
            return classes[0]
        raise MarketDataError("当前到期日有多个期权类别，请在类别框明确选择。")

    def _contracts(self, symbol, expiry, right, trading_class=""):
        from ib_async import Option
        symbol = normalize_symbol(symbol)
        if right not in ("C", "P"):
            raise MarketDataError("期权方向必须是 C 或 P。")
        trading_class = self._choose_class(symbol, expiry, trading_class)
        underlying = self._underlying(symbol)
        day = datetime.now(ZoneInfo("America/New_York")).strftime("%Y%m%d")
        key = (symbol, expiry, right, trading_class, day)
        if key not in self._chain_details:
            query = Option(underlying.symbol, expiry, exchange="SMART", right=right,
                           currency="USD", multiplier="100", tradingClass=trading_class)
            all_details = {}
            for detail in self.ib.reqContractDetails(query):
                contract = detail.contract
                if (contract and contract.secType == "OPT" and contract.symbol == underlying.symbol
                        and contract.lastTradeDateOrContractMonth[:8] == expiry
                        and contract.right == right and str(contract.multiplier) == "100"
                        and contract.tradingClass == trading_class and contract.currency == "USD"
                        and contract.conId > 0 and math.isfinite(contract.strike) and contract.strike > 0
                        and _same_contract_id(detail.underConId, underlying.conId)
                        and "SMART" in detail.validExchanges.split(",")):
                    contract.exchange = "SMART"
                    all_details[contract.conId] = detail
                    self._details[contract.conId] = detail
            if not all_details:
                raise MarketDataError("所选到期日、类别和方向没有已确认的标准可路由合约。")
            self._chain_details[key] = list(all_details.values())
        return self._chain_details[key]

    @staticmethod
    def _display_number(value):
        try:
            value = float(value)
            return value if math.isfinite(value) and value >= 0 else None
        except (ValueError, TypeError):
            return None

    def _display_spot(self, underlying):
        self._spot_reference_meta = {}
        ticker = self._subscribe(underlying)
        now = time.monotonic()
        stamps = self._price_times.get(underlying.conId, {})
        fresh = lambda field: now - stamps.get(field, -1e9) <= self.QUOTE_TTL
        bid, ask = self._display_number(ticker.bid), self._display_number(ticker.ask)
        last, close = self._display_number(ticker.last), self._display_number(ticker.close)
        if ticker.marketDataType == 1 and ticker.halted not in (1, 2):
            if bid and ask and bid <= ask and fresh(1) and fresh(2):
                return (bid + ask) / 2, "实时中间价"
            if last and fresh(4):
                return last, "实时最新价"
        if underlying.symbol in ("SPX", "XSP"):
            if not hasattr(self, "_spot_reference"):
                from spot_reference import SpotReference
                self._spot_reference = SpotReference(self.ib, self._subscribe, getattr(self, "log", lambda _: None),
                                                     historical_timeout=6, retry_seconds=300,
                                                     resolve_underlying=self._underlying)
            reference = self._spot_reference.get(underlying, self._price_times)
            self._spot_reference_meta = reference
            if reference.get("spot") is not None:
                return reference["spot"], reference["kind"]
        if last:
            return last, "历史/延迟最新价，仅供定位行权价"
        if close:
            return close, "前收盘价，仅供定位行权价"
        return None, "无有效参考价，等待标的行情；可输入备用参考价定位"

    def _display_leg(self, detail, subscribe=True):
        if detail is None:
            return {"conid": None, "local_symbol": "", "bid": None, "ask": None,
                    "last": None, "quote_type": "无该合约", "age": None}
        contract = detail.contract
        metadata = {"conid": contract.conId, "local_symbol": contract.localSymbol,
                    "symbol": contract.symbol, "expiry": contract.lastTradeDateOrContractMonth[:8],
                    "right": contract.right, "strike": float(contract.strike),
                    "trading_class": contract.tradingClass, "multiplier": float(contract.multiplier)}
        if not subscribe and contract.conId not in self._tickers:
            return {**metadata, "bid": None, "ask": None, "last": None,
                    "quote_type": "未订阅·滚动到此档加载", "age": None,
                    "bid_age": None, "ask_age": None, "tick_age": None, "subscribed": False}
        ticker = self._subscribe(contract)
        stamps = self._price_times.get(contract.conId, {})
        display = getattr(self, "_display_times", {}).get(contract.conId, stamps)
        age_of = lambda field: max(0.0, time.monotonic() - display[field]) if field in display else None
        bid_age, ask_age, tick_age = age_of(1), age_of(2), age_of("tick")
        price_times = [display.get(1), display.get(2)]
        age = (max(0.0, time.monotonic() - min(price_times))
               if all(stamp is not None for stamp in price_times) else None)
        bid, ask, last = (self._display_number(getattr(ticker, field, None))
                          for field in ("bid", "ask", "last"))
        kind = {1: "实时订阅", 2: "冻结", 3: "延迟", 4: "延迟冻结"}.get(ticker.marketDataType, "未知行情类型")
        if ticker.halted in (1, 2):
            kind = "停牌"
        elif ticker.marketDataType == 1:
            if bid is None or ask is None:
                kind = "等待盘口报价"
            elif bid > ask:
                kind = "盘口异常"
            elif age is None:
                kind = "等待盘口回报"
            elif age > self.QUOTE_TTL:
                kind = "实时订阅·盘口久未更新"
        error = getattr(self, "_market_errors", {}).get(contract.conId)
        if error:
            code = error[0]
            if code in (10090, 10091):
                kind += "·部分权限受限"
            elif bid is None or ask is None:
                kind = "额度不足" if code == 100 else "缺少API行情权限"
        return {**metadata, "subscribed": True,
                "bid": bid, "ask": ask, "last": last, "quote_type": kind,
                "age": round(age, 1) if age is not None else None,
                "bid_age": bid_age, "ask_age": ask_age, "tick_age": tick_age}

    def chain(self, symbol, expiry, trading_class="", offset=0, limit=40, reference_price=None,
              visible_strikes=None):
        self._require()
        symbol = normalize_symbol(symbol)
        trading_class = self._choose_class(symbol, expiry, trading_class)
        underlying = self._underlying(symbol)
        self._spot_reference_meta = {}
        spot, spot_kind = self._display_spot(underlying)
        reference = getattr(self, "_spot_reference_meta", {})
        if spot is None and reference_price is not None:
            if isinstance(reference_price, bool) or self._display_number(reference_price) in (None, 0):
                raise MarketDataError("备用参考价必须为有限正数。")
            spot, spot_kind = float(reference_price), "手动备用参考价，仅供看链"
            reference = {"spot": spot, "kind": spot_kind, "source": "MANUAL", "estimated": True}
        if spot is None:
            return {"symbol": symbol, "expiry": expiry, "trading_class": trading_class,
                    "classes": self.trading_classes(symbol, expiry), "spot": None, "spot_kind": spot_kind,
                    "spot_ref": reference, "rows": [], "offset": 0, "has_prev": False, "has_next": False,
                    "awaiting_reference": True, "updated_at": time.time(), "mode": self.mode}
        calls = self._contracts(symbol, expiry, "C", trading_class)
        puts = self._contracts(symbol, expiry, "P", trading_class)
        by_right = {}
        for right, details in (("call", calls), ("put", puts)):
            by_right[right] = {}
            for detail in details:
                strike = float(detail.contract.strike)
                if strike in by_right[right]:
                    raise MarketDataError("同一类别/行权价存在重复合约，不能安全展示，请换类别。")
                by_right[right][strike] = detail
        strikes, offset, has_prev, has_next = chain_page(
            set(by_right["call"]) | set(by_right["put"]), spot, offset, limit)
        # Listing all strikes must not exhaust IBKR's market-data line quota.
        # Subscribe at most 40 Call/Put pairs around the viewport (or ATM until
        # the UI has its first viewport), keeping every other row selectable.
        if visible_strikes is None:
            watched, _, _, _ = chain_page(strikes, spot, 0, CHAIN_STRIKE_BUDGET)
        else:
            try:
                supplied = [float(value) for value in visible_strikes if not isinstance(value, bool)]
            except (ValueError, TypeError, OverflowError) as exc:
                raise MarketDataError("可见行权价必须为有效数字。") from exc
            available = set(strikes)
            watched = list(dict.fromkeys(value for value in supplied if value in available))[:CHAIN_STRIKE_BUDGET]
        watched = set(watched)
        active = {underlying.conId}
        for strike in watched:
            for right in ("call", "put"):
                detail = by_right[right].get(strike)
                if detail:
                    active.add(detail.contract.conId)
        # Cancel only streams previously owned by the chain panel, not arbitrary
        # subscriptions used by the preview. They can be safely resubscribed.
        previous = getattr(self, "_chain_stream_ids", set())
        pinned = pinned_quote_ids(self)
        for conid in previous - active - pinned:
            release_quote_stream(self, conid)
        # Retain ownership of pinned old rows so they can be released after
        # preview use expires, instead of slowly leaking subscriptions.
        self._chain_stream_ids = active | (previous & pinned)
        rows = [{"strike": strike,
                 "call": self._display_leg(by_right["call"].get(strike), strike in watched),
                 "put": self._display_leg(by_right["put"].get(strike), strike in watched)}
                for strike in strikes]
        return {"symbol": symbol, "expiry": expiry, "trading_class": trading_class,
                "classes": self.trading_classes(symbol, expiry), "spot": spot,
                "spot_kind": spot_kind, "spot_ref": reference, "rows": rows, "offset": offset,
                "awaiting_reference": False,
                "watched_strikes": sorted(watched), "subscription_leg_budget": CHAIN_STRIKE_BUDGET * 2,
                "has_prev": has_prev, "has_next": has_next,
                "updated_at": time.time(), "mode": self.mode}
