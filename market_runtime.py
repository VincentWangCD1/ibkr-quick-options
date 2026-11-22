"""Bounded market-data refresh and verified, contract-specific calendars.

Refreshing a subscription only requests IBKR to send the current book again;
it never marks a cached value fresh. The broker's normal tick callbacks and
strict live-data checks remain the only source of quote freshness.
"""
from __future__ import annotations

from copy import copy
from datetime import datetime, timedelta, timezone
import time
from zoneinfo import ZoneInfo

from trading_sessions import _parse_hours, _CalendarError


NY = ZoneInfo("America/New_York")
CHAIN_STRIKE_BUDGET = 40
MAX_DISPLAY_STRIKES = 10000


def selection_expiry_floor(symbol, now=None):
    """Hide already-expired PM index expiries from the selector after 16 ET.

    This is only a rejection/filter. It never authorizes an order. Actual
    execution still requires this exact contract's IBKR trading calendars,
    including earlier AM-expiry closes and exchange holidays/early closes.
    Cboe XSP specifications: expiring series close at 15:00 Chicago / 16:00 ET.
    SPXW PM series have the same latest expiry-day close.
    """
    current = (now or datetime.now(timezone.utc)).astimezone(NY)
    day = current.date()
    if str(symbol).upper() in ("SPX", "XSP") and current.hour >= 16:
        day += timedelta(days=1)
    return day.strftime("%Y%m%d")


def expired_selection_reason(contract, now=None):
    if getattr(contract, "secType", "") != "OPT":
        return ""
    expiry = str(getattr(contract, "lastTradeDateOrContractMonth", ""))[:8]
    if len(expiry) != 8 or not expiry.isdigit():
        return ""
    current = (now or datetime.now(timezone.utc)).astimezone(NY)
    floor = selection_expiry_floor(getattr(contract, "symbol", ""), current)
    if expiry < floor:
        return (f"所选 {expiry} 到期合约已过最后交易时段（纽约 {current:%m-%d %H:%M}）；"
                "请刷新并选择下一有效到期日。夜盘交易不延长已到期合约的寿命。")
    return ""


def pinned_quote_ids(broker):
    now = time.monotonic()
    pins = getattr(broker, "_priority_quote_ids", {})
    broker._priority_quote_ids = {key: deadline for key, deadline in pins.items() if deadline > now}
    return set(broker._priority_quote_ids)


def pin_quote_contract(broker, contract, seconds=30.0):
    pinned_quote_ids(broker)
    broker._priority_quote_ids[contract.conId] = time.monotonic() + seconds


def release_quote_stream(broker, conid):
    """Cancel our stream and detach its obsolete ib_async request mapping.

    ib_async 2.1 reuses Ticker objects by contract hash and leaves the old
    reqId2Ticker entry behind when cancelling. Remove only the cancelled
    market-data request, and release its ticker cache only if no other live
    subscription owns that ticker. This also prevents late old-request ticks
    from being mistaken for a response to a new subscription.
    """
    ticker = broker._tickers.get(conid)
    if ticker is None:
        return False
    wrapper = getattr(broker.ib, "wrapper", None)
    kinds = getattr(wrapper, "ticker2ReqId", None)
    reqid = kinds.get("mktData", {}).get(ticker) if isinstance(kinds, dict) else None
    broker.ib.cancelMktData(ticker.contract)
    broker._tickers.pop(conid, None)
    if reqid is not None:
        mapping = getattr(wrapper, "reqId2Ticker", {})
        if isinstance(mapping, dict) and mapping.get(reqid) is ticker:
            mapping.pop(reqid, None)
        active_elsewhere = any(ticker in entries for entries in kinds.values())
        if not active_elsewhere:
            cache = getattr(wrapper, "tickers", {})
            if isinstance(cache, dict):
                for key in [key for key, value in cache.items() if value is ticker]:
                    cache.pop(key, None)
            pending = getattr(wrapper, "pendingTickers", None)
            if isinstance(pending, set):
                pending.discard(ticker)
    broker._price_times.pop(conid, None)
    getattr(broker, "_display_times", {}).pop(conid, None)
    getattr(broker, "_market_errors", {}).pop(conid, None)
    return True


def reset_quote_stream(broker, contract):
    """Request a fresh book at most once per contract every two seconds.

    Caller must resubscribe and await new ticks. No timestamps are invented.
    """
    now = time.monotonic()
    resets = getattr(broker, "_quote_reset_times", {})
    if now - resets.get(contract.conId, -1e9) < 2.0:
        return False
    if not release_quote_stream(broker, contract.conId):
        return False
    resets[contract.conId] = now
    broker._quote_reset_times = resets
    return True


def _calendar_covers_now(detail, now):
    """Require parseable broker calendars covering today/the active overnight."""
    try:
        zone_name = getattr(detail, "timeZoneId", "")
        if not isinstance(zone_name, str) or not zone_name.strip():
            return False
        zone = ZoneInfo(zone_name.strip())
        trading = _parse_hours(getattr(detail, "tradingHours", ""), zone)
        liquid = _parse_hours(getattr(detail, "liquidHours", ""), zone)
        day = now.astimezone(zone).date()
        active = next((item for item in trading.intervals if item.start <= now < item.end), None)
        end_day = ((active.end - timedelta(microseconds=1)).astimezone(zone).date()
                   if active else day)
        return ((day in trading.dates or active is not None)
                and (day in liquid.dates or (active is not None and day < end_day and end_day in liquid.dates)))
    except (_CalendarError, ValueError, TypeError, KeyError, OverflowError):
        return False


def _same_option(requested, candidate):
    fields = ("conId", "symbol", "secType", "currency", "right", "strike", "tradingClass")
    if any(getattr(requested, field, None) != getattr(candidate, field, None) for field in fields):
        return False
    return (getattr(candidate, "secType", "") == "OPT"
            and getattr(candidate, "currency", "") == "USD"
            and str(getattr(requested, "multiplier", "")) == str(getattr(candidate, "multiplier", "")) == "100"
            and str(getattr(requested, "lastTradeDateOrContractMonth", ""))[:8]
                == str(getattr(candidate, "lastTradeDateOrContractMonth", ""))[:8])


def refresh_execution_calendar(broker, detail, now=None):
    """Repair incomplete bulk metadata using an exact-conId request.

    Never borrow a calendar from a different strike, expiry or exchange, and
    never infer a timezone from the symbol. A verified cache is reusable only
    for the same conId/route/New York day and for five minutes. A bad response
    cannot overwrite an earlier verified calendar.
    """
    now = now or datetime.now(timezone.utc)
    if _calendar_covers_now(detail, now):
        return detail
    contract = detail.contract
    if expired_selection_reason(contract, now):
        return detail
    route = getattr(contract, "exchange", "") or "SMART"
    key = (contract.conId, route, now.astimezone(NY).date())
    cache = getattr(broker, "_verified_calendar_cache", {})
    cached = cache.get(key)
    if cached and time.monotonic() - cached[0] < 300 and _calendar_covers_now(cached[1], now):
        return cached[1]
    query = copy(contract)
    query.exchange = route
    received = broker.ib.reqContractDetails(query)
    candidates = []
    for candidate in received:
        if not _same_option(contract, getattr(candidate, "contract", None)):
            continue
        original_under = getattr(detail, "underConId", None)
        if original_under not in (None, 0, "", "0") and str(getattr(candidate, "underConId", "")) != str(original_under):
            continue
        if route not in str(getattr(candidate, "validExchanges", "")).split(","):
            continue
        if _calendar_covers_now(candidate, now):
            candidates.append(candidate)
    if len(candidates) != 1:
        return detail
    verified = copy(candidates[0])
    verified.contract = copy(verified.contract)
    verified.contract.exchange = route
    cache[key] = (time.monotonic(), verified)
    broker._verified_calendar_cache = cache
    if callable(getattr(broker, "log", None)):
        broker.log(f"已按精确合约刷新交易日历：{contract.localSymbol or contract.conId} / {route}。")
    return verified
