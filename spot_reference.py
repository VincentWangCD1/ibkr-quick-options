"""SPX/XSP references from synchronized SPY closes, with ES backup.

This module never places orders and never manufactures option prices. Its
historical bars are requested only when the user runs the application. A live
SPY times a prior-session ratio (or ES plus a fixed basis) is an estimate,
never a live index quote. TRADES is not dividend-adjusted; IB does adjust splits.

IBKR historical bars: https://www.interactivebrokers.com/docs/tws-api/doc/md/historical-bars
CME ES: https://www.cmegroup.com/education/lessons/product-e-mini-s-and-p
"""
from __future__ import annotations

from copy import copy
import asyncio
from datetime import date, datetime, time as daytime, timedelta, timezone
import math
import re
import time
from zoneinfo import ZoneInfo


NY = ZoneInfo("America/New_York")


class ReferenceError(RuntimeError):
    pass


def _positive(value):
    try:
        result = float(value)
        return result if math.isfinite(result) and 0 < result < 1e12 else None
    except (TypeError, ValueError, OverflowError):
        return None


def _date(value):
    if isinstance(value, datetime):
        return value.astimezone(NY).date() if value.tzinfo else value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not re.fullmatch(r"[0-9]{8}", text):
        raise ReferenceError("历史日线没有可确认的交易日期")
    return datetime.strptime(text, "%Y%m%d").date()


def _minute_timestamp(value):
    # formatDate=2 yields timezone-aware UTC datetimes in ib_async. Never guess
    # the timezone of a naive TWS/login-time string when aligning two markets.
    if isinstance(value, datetime) and value.tzinfo is not None:
        result = value.astimezone(timezone.utc)
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        result = datetime.fromtimestamp(value, timezone.utc)
    elif isinstance(value, str) and re.fullmatch(r"[0-9]{9,11}", value):
        result = datetime.fromtimestamp(int(value), timezone.utc)
    else:
        raise ReferenceError("分钟线时间戳缺少明确时区，不能校准 SPX 与 ES")
    if result.second or result.microsecond:
        raise ReferenceError("分钟线时间戳未对齐到整分钟")
    return result


def previous_spx_close(bars, today):
    """Latest returned completed SPX date strictly before today's NY date."""
    by_day = {}
    for bar in bars:
        day = _date(bar.date)
        value = _positive(bar.close)
        if day >= today or value is None:
            continue
        if day in by_day and by_day[day] != value:
            raise ReferenceError("SPX 日线同一日期有冲突收盘价，不能校准")
        by_day[day] = value
    if not by_day:
        raise ReferenceError("没有上一已完成交易日的 SPX 历史收盘价")
    day = max(by_day)
    if (today - day).days > 10:
        raise ReferenceError("最近返回的 SPX 日线已超过 10 个自然日，不能作为当前校准基准")
    return {"basis_date": day.isoformat(), "daily_close": by_day[day], "day": day}


def _minute_prices(bars, basis_day, name):
    by_time = {}
    for bar in bars:
        stamp = _minute_timestamp(bar.date)
        if stamp.astimezone(NY).date() != basis_day:
            continue
        close = _positive(bar.close)
        if close is None:
            continue
        if stamp in by_time and by_time[stamp] != close:
            raise ReferenceError(f"{name} 分钟线同一时间有冲突收盘价")
        by_time[stamp] = close
    return by_time


def closing_spx_minute(bars, basis_day, daily_close=None):
    by_time = _minute_prices(bars, basis_day, "SPX")
    if not by_time:
        raise ReferenceError("没有基准交易日的 SPX 常规时段分钟线")
    stamp = max(by_time)
    value = by_time[stamp]
    return stamp, value


def scheduled_close(schedule, basis_day):
    """Read an actual historical RTH end, never invent a 16:00 session."""
    try:
        zone = ZoneInfo(schedule.timeZone)
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise ReferenceError("SPY 历史交易时段没有明确时区") from exc
    ends = []
    for session in schedule.sessions:
        if _date(session.refDate) != basis_day:
            continue
        try:
            start = datetime.strptime(session.startDateTime, "%Y%m%d-%H:%M:%S").replace(tzinfo=zone)
            end = datetime.strptime(session.endDateTime, "%Y%m%d-%H:%M:%S").replace(tzinfo=zone)
        except (ValueError, TypeError) as exc:
            raise ReferenceError("SPY 历史交易时段格式无法确认") from exc
        if start >= end or end.astimezone(NY).date() != basis_day or end.second:
            raise ReferenceError("SPY 历史常规时段结束日期或时间无效")
        ends.append(end.astimezone(timezone.utc))
    if not ends:
        raise ReferenceError("基准日没有 SPY 已确认常规交易时段，不能猜测收盘时刻")
    return max(ends)


def paired_spy_close(spx_bars, spy_bars, basis_day, session_close):
    """Both TRADES minute closes must cover the verified final RTH minute."""
    end = _minute_timestamp(session_close)
    if end.astimezone(NY).date() != basis_day:
        raise ReferenceError("收盘时段与基准交易日不同")
    stamp = end - timedelta(minutes=1)
    spx = _minute_prices(spx_bars, basis_day, "SPX")
    spy = _minute_prices(spy_bars, basis_day, "SPY")
    if stamp not in spx or stamp not in spy:
        raise ReferenceError("SPX/SPY 缺少已确认 RTH 收盘前最后一分钟的同步行情，不能用残缺尾段校准")
    return stamp, spx[stamp], spy[stamp]


def matching_es_close(bars, timestamp):
    matches = []
    for bar in bars:
        stamp = _minute_timestamp(bar.date)
        if stamp == timestamp:
            value = _positive(bar.close)
            if value is not None:
                matches.append(value)
    if not matches:
        raise ReferenceError("找不到与 SPX 收盘分钟完全同时间戳的 ES 分钟线")
    if len(set(matches)) != 1:
        raise ReferenceError("ES 同一收盘分钟存在冲突价格，不能校准")
    return matches[0]


class SpotReference:
    """One instance per IB connection; immutable daily SPY ratio / ES basis.

    subscribe(contract) must return the owning broker's streaming ticker;
    price_times maps conId -> tickType -> time.monotonic() reception timestamp.
    Only normal live ticks 1/2/4 count. The caller tries actual index quotes
    first. resolve_underlying optionally reuses its qualified SPX/SPY cache.
    Estimates may locate strikes only under the caller's session policy; this
    provider never authorizes an order or substitutes option bid/ask prices.
    """
    def __init__(self, ib, subscribe, log=lambda message: None, *, quote_ttl=10,
                 retry_seconds=60, historical_timeout=12, now=None, resolve_underlying=None):
        self.ib, self.subscribe, self.log = ib, subscribe, log
        self.quote_ttl = quote_ttl
        self.retry_seconds = retry_seconds
        self.historical_timeout = historical_timeout
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.resolve_underlying = resolve_underlying
        self._daily = {}
        self._es_contracts = {}
        self._reference_contracts = {}
        self._schedules = {}
        self._bases = {}
        self._failures = {}
        self._last_status = None
        self._cache_day = None

    def clear(self):
        self._daily.clear()
        self._es_contracts.clear()
        self._reference_contracts.clear()
        self._schedules.clear()
        self._bases.clear()
        self._failures.clear()
        self._last_status = None
        self._cache_day = None

    def _cached(self, storage, key, loader):
        if key in storage:
            return storage[key]
        previous = self._failures.get(key)
        if previous and time.monotonic() < previous[0]:
            raise ReferenceError(previous[1])
        try:
            result = loader()
        except Exception as exc:
            error = str(exc) or type(exc).__name__
            self._failures[key] = (time.monotonic() + self.retry_seconds, error)
            raise ReferenceError(error) from exc
        storage[key] = result
        self._failures.pop(key, None)
        return result

    def _history(self, contract, end, duration, size, rth):
        bars = self.ib.reqHistoricalData(
            contract, endDateTime=end, durationStr=duration, barSizeSetting=size,
            whatToShow="TRADES", useRTH=rth, formatDate=2,
            keepUpToDate=False, timeout=self.historical_timeout)
        if not bars:
            raise ReferenceError(f"{contract.localSymbol or contract.symbol} 无可用 {size} 历史行情（可能缺少权限或请求超时）")
        return bars

    def _daily_close(self, underlying, today):
        key = ("SPX-day", today, underlying.conId)
        def load():
            end = datetime.combine(today, daytime.min, NY)
            bars = self._history(underlying, end, "1 M", "1 day", True)
            return previous_spx_close(bars, today)
        return self._cached(self._daily, key, load)

    def _reference_contract(self, symbol, today):
        def load():
            expected = "STK" if symbol == "SPY" else "IND"
            if self.resolve_underlying is not None:
                candidates = [self.resolve_underlying(symbol)]
            else:
                from ib_async import Index, Stock
                request = (Stock("SPY", "SMART", "USD", primaryExchange="ARCA") if symbol == "SPY"
                           else Index("SPX", "CBOE", "USD"))
                candidates = [d.contract for d in self.ib.reqContractDetails(request)]
            valid = {c.conId: c for c in candidates if c and c.conId > 0 and c.symbol == symbol
                     and c.secType == expected and c.currency == "USD"}
            if len(valid) != 1:
                raise ReferenceError(f"不能唯一确认 {symbol}/USD 参考合约")
            contract = copy(next(iter(valid.values())))
            if symbol == "SPY":
                if getattr(contract, "primaryExchange", "") not in ("", "ARCA"):
                    raise ReferenceError("SPY 参考合约的主交易所不是 ARCA")
                contract.exchange, contract.primaryExchange = "SMART", "ARCA"
            return contract
        return self._cached(self._reference_contracts, ("reference-contract", today, symbol), load)

    def _spy_session_close(self, contract, day):
        def load():
            end = datetime.combine(day + timedelta(days=1), daytime.min, NY)
            # ib_async's synchronous schedule helper has no timeout argument.
            # Keep this request bounded on the broker's existing worker loop.
            request = self.ib.reqHistoricalScheduleAsync(contract, 1, endDateTime=end, useRTH=True)
            try:
                schedule = self.ib._run(asyncio.wait_for(request, timeout=self.historical_timeout))
            except Exception:
                # ib_async 2.1 leaves timed-out schedule requests registered.
                # Cancel only this exact future, never another API request.
                futures = getattr(getattr(self.ib, "wrapper", None), "_futures", None)
                if isinstance(futures, dict):
                    for reqid, future in list(futures.items()):
                        if future is request:
                            self.ib.client.cancelHistoricalData(reqid)
                            self.ib.wrapper._endReq(reqid)
                raise
            return scheduled_close(schedule, day)
        return self._cached(self._schedules, ("SPY-RTH", day, contract.conId), load)

    def _spy_basis(self, underlying, contract, today, daily):
        def load():
            day = daily["day"]
            end = self._spy_session_close(contract, day)
            # Requests end at the verified close, so no after-hours prints or
            # later differently timed daily closing value enters this ratio.
            spx_bars = self._history(underlying, end, "600 S", "1 min", True)
            spy_bars = self._history(contract, end, "600 S", "1 min", True)
            stamp, spx_price, spy_price = paired_spy_close(spx_bars, spy_bars, day, end)
            ratio = spx_price / spy_price
            result = {"basis_date": day.isoformat(), "basis_bar_start": stamp.isoformat(),
                      "basis_time": end.isoformat(), "basis_precision": "已确认 RTH 最后同一分钟收盘采样",
                      "spx_close": daily["daily_close"], "spx_basis_price": spx_price,
                      "spx_minute_close": spx_price, "spy_basis_price": spy_price,
                      "spx_daily_close_difference": spx_price - daily["daily_close"],
                      "ratio": ratio, "spy_conid": contract.conId,
                      "anchor_verified": True, "historical_price_type": "TRADES",
                      "adjustment_note": "TRADES 不作股息复权；IB 会调整拆股，未使用 ADJUSTED_LAST"}
            self.log(f"SPX/SPY 日校准：{result['basis_date']} {result['basis_time']}，"
                     f"同分钟系数 {ratio:.8f}；指数日收盘偏差 {result['spx_daily_close_difference']:+.4f}，仅为估算。")
            return result
        return self._cached(self._bases, ("SPX-SPY-ratio", today, underlying.conId, contract.conId), load)

    def _es(self, today):
        key = ("ES-contract", today)
        def load():
            from ib_async import Future
            details = self.ib.reqContractDetails(Future("ES", exchange="CME", currency="USD"))
            candidates = {}
            for detail in details:
                contract = detail.contract
                if not (contract and contract.secType == "FUT" and contract.symbol == "ES"
                        and contract.currency == "USD" and _positive(contract.multiplier) == 50
                        and contract.conId > 0):
                    continue
                exchanges = set(str(getattr(detail, "validExchanges", "")).split(","))
                if contract.exchange not in ("CME", "GLOBEX") and not exchanges.intersection({"CME", "GLOBEX"}):
                    continue
                raw = getattr(detail, "realExpirationDate", "") or contract.lastTradeDateOrContractMonth[:8]
                try:
                    expiry = _date(raw)
                except (ValueError, ReferenceError):
                    continue
                # Do not use a contract on its expiry day: its trading can end
                # intraday. Quarterly ES is explicit; MES and CONTFUT excluded.
                if expiry <= today or expiry.month not in (3, 6, 9, 12):
                    continue
                candidates[contract.conId] = (expiry, contract)
            if not candidates:
                raise ReferenceError("未确认可用的未到期 ES/CME 季度期货合约")
            expiry = min(value[0] for value in candidates.values())
            nearest = [contract for day, contract in candidates.values() if day == expiry]
            if len(nearest) != 1:
                raise ReferenceError("最近到期 ES 合约不唯一，不能猜测换月合约")
            contract = copy(nearest[0])
            contract.exchange = "CME"
            return contract
        return self._cached(self._es_contracts, key, load)

    def _live_es(self, contract, price_times):
        return self._live_proxy(contract, price_times, "ES", "CME")

    def _live_proxy(self, contract, price_times, symbol, venue):
        ticker = self.subscribe(contract)
        if ticker.marketDataType != 1 or ticker.halted in (1, 2):
            raise ReferenceError(f"{symbol} 不是有效实时行情（延迟、冻结或停牌），不能生成实时估算")
        if ticker.contract.conId != contract.conId:
            raise ReferenceError(f"{symbol} 实时报价合约与校准合约不一致")
        stamps = price_times.get(contract.conId, {})
        now = time.monotonic()
        fresh = lambda tick: tick in stamps and 0 <= now - stamps[tick] <= self.quote_ttl
        last = _positive(ticker.last)
        if last and fresh(4):
            return last, f"{symbol} 最新成交", now - stamps[4]
        bid, ask = _positive(ticker.bid), _positive(ticker.ask)
        if bid and ask and bid <= ask and fresh(1) and fresh(2):
            return (bid + ask) / 2, f"{symbol} 买卖中间价", max(now - stamps[1], now - stamps[2])
        raise ReferenceError(f"{symbol} 没有最近 {self.quote_ttl:g} 秒内的有效实时价格；请核对 {venue} 行情权限")

    def _basis(self, underlying, contract, today, daily):
        key = ("SPX-ES-basis", today, underlying.conId, contract.conId)
        def load():
            day = daily["day"]
            end = self._spy_session_close(self._reference_contract("SPY", today), day)
            spx_bars = self._history(underlying, end, "600 S", "1 min", True)
            stamp = end - timedelta(minutes=1)
            values = _minute_prices(spx_bars, day, "SPX")
            if stamp not in values:
                raise ReferenceError("SPX 缺少已确认收盘前最后一分钟，不能校准 ES 差值")
            minute_close = values[stamp]
            # Match the same UTC one-minute interval, NOT ES's own later daily
            # session close or settlement price. The daily close can differ
            # legitimately; pair the actual synchronized minute prices.
            es_bars = self._history(contract, stamp + timedelta(minutes=2), "600 S", "1 min", False)
            es_close = matching_es_close(es_bars, stamp)
            basis = {"basis_date": day.isoformat(), "basis_bar_start": stamp.isoformat(),
                     "basis_time": (stamp + timedelta(minutes=1)).isoformat(),
                     "basis_precision": "已确认 RTH 最后同一分钟收盘采样（不是同一笔 tick）",
                     "spx_close": daily["daily_close"], "spx_minute_close": minute_close,
                     "spx_basis_price": minute_close,
                     "spx_daily_close_difference": minute_close - daily["daily_close"],
                     "es_basis_price": es_close, "offset": minute_close - es_close,
                     "anchor_verified": True,
                     "es_conid": contract.conId, "es_contract": contract.localSymbol or str(contract.conId),
                     "es_expiry": contract.lastTradeDateOrContractMonth}
            self.log(f"SPX/ES 日校准：{basis['basis_date']}，{basis['es_contract']}，"
                     f"同分钟 {basis['basis_bar_start']}，固定差值 {basis['offset']:+.4f}；仅用于估算参考。")
            return basis
        return self._cached(self._bases, key, load)

    def get(self, underlying, price_times):
        symbol = getattr(underlying, "symbol", "")
        result = {"spot": None, "kind": f"{symbol} 参考价不可用", "source": "unavailable", "estimated": False,
                  "basis_date": "", "basis_time": "", "es_contract": "", "es_conid": None,
                  "offset": None, "ratio": None, "error": "", "usable_for_order": False,
                  "anchor_verified": False, "usable_for_matching": False, "symbol": symbol,
                  "scale": 0.1 if symbol == "XSP" else 1.0}
        if symbol not in ("SPX", "XSP") or getattr(underlying, "secType", "") != "IND":
            result["error"] = "SPY/ES 校准参考仅适用于 SPX/XSP 指数"
            return result
        today = self.now().astimezone(NY).date()
        if today != self._cache_day:
            self.clear()
            self._cache_day = today
        try:
            if symbol == "XSP":
                underlying = self._reference_contract("SPX", today)
            daily = self._daily_close(underlying, today)
        except Exception as exc:
            result["error"] = str(exc) or type(exc).__name__
            return self._report(result)
        scale = result["scale"]
        result.update(spot=daily["daily_close"] * scale,
                      kind=f"{symbol} {'SPX前收盘÷10' if symbol == 'XSP' else '前收盘'} {daily['basis_date']}（非实时）",
                      source="SPX_PREVIOUS_CLOSE", basis_date=daily["basis_date"], spx_close=daily["daily_close"])
        failures = []
        try:
            spy = self._reference_contract("SPY", today)
            self._live_proxy(spy, price_times, "SPY", "Network B")
            basis = self._spy_basis(underlying, spy, today, daily)
            current, quote_source, age = self._live_proxy(spy, price_times, "SPY", "Network B")
            if self.now().astimezone(NY).date() != today:
                raise ReferenceError("校准期间纽约日期已变化，请刷新后重建当天锚点")
            estimate = _positive(current * basis["ratio"] * scale)
            if estimate is None:
                raise ReferenceError("SPY 换算结果不是有效正数")
            result.update(basis)
            result.update(spot=estimate, kind=f"{symbol} 估算 · SPY 实时价 × 前收盘同步系数" + (" ÷ 10" if scale != 1 else ""),
                          source="SPY_RATIO_ESTIMATE", estimated=True, spy_price=current,
                          spy_quote_source=quote_source, quote_age=round(age, 3), error="",
                          quote_checked_at=time.monotonic(), usable_for_matching=True)
            return self._report(result)
        except Exception as exc:
            failures.append("SPY 换算：" + (str(exc) or type(exc).__name__))
        try:
            contract = self._es(today)
            result.update(es_contract=contract.localSymbol or str(contract.conId), es_conid=contract.conId)
            # Avoid expensive minute history until a real live ES feed exists.
            self._live_es(contract, price_times)
            basis = self._basis(underlying, contract, today, daily)
            # History requests can take seconds. Re-read after them so the
            # returned estimate is never based on the pre-request quote.
            current, quote_source, age = self._live_es(contract, price_times)
            if self.now().astimezone(NY).date() != today:
                raise ReferenceError("校准期间纽约日期已变化，请刷新后重建当天锚点")
            estimate = _positive((current + basis["offset"]) * scale)
            if estimate is None:
                raise ReferenceError("ES 校准结果不是有效正数")
            result.update(basis)
            result.update(spot=estimate, kind=f"{symbol} 估算 · {basis['es_contract']} 实时价＋前收盘差值" + (" 后÷10" if scale != 1 else ""),
                          source="ES_ADJUSTED_ESTIMATE", estimated=True, es_price=current,
                          es_quote_source=quote_source, quote_age=round(age, 3), error="",
                          quote_checked_at=time.monotonic(), usable_for_matching=True,
                          fallback_reason="；".join(failures))
        except Exception as exc:
            failures.append("ES 备用：" + (str(exc) or type(exc).__name__))
            result["error"] = "；".join(failures)
        return self._report(result)

    def _report(self, result):
        signature = (result["symbol"], result["source"], result["basis_date"], result["es_conid"], result["error"])
        if signature != self._last_status:
            self._last_status = signature
            self.log(f"SPX 参考价来源：{result['kind']}" + (f"；{result['error']}" if result["error"] else ""))
        return result
