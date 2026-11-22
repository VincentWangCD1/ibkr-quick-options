"""Contract-specific trading sessions; no symbol or weekday assumptions.

IBKR defines liquidHours as regular trading hours and supplies timeZoneId for
both calendars. Since TWS 970, each interval endpoint includes its date; the
older same-day endpoint syntax is supported as well.
https://www.interactivebrokers.com/docs/tws-api/ref/contract-details
https://www.interactivebrokers.com/en/general/tws-notes-970.php

This is a calendar decision, not proof of trading permission, routing support,
market-data freshness, or absence of a halt. The caller must still apply its
normal contract, quote, order-type, and broker checks. In extended sessions the
caller must build a priced LIMIT order; a calendar alone never supplies price.
IBKR outsideRth allows eligible orders to trigger/fill outside RTH. Cboe says
market orders are not actionable in the option GTH/Curb sessions:
https://www.interactivebrokers.com/docs/tws-api/ref/order
https://www.cboe.com/document/tech-spec/content/technical-specifications/equity-options-extended-trading-hours-faq/market-makers/
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


UTC = timezone.utc


@dataclass(frozen=True)
class SessionDecision:
    kind: str
    reason: str
    order_type: str | None = None
    outside_rth: bool = False
    session_end: datetime | None = None


@dataclass(frozen=True)
class _Interval:
    start: datetime
    end: datetime


@dataclass(frozen=True)
class _Calendar:
    intervals: tuple[_Interval, ...]
    dates: frozenset[date]
    closed_dates: frozenset[date]


class _CalendarError(ValueError):
    pass


def _field(details, name):
    return details.get(name, "") if isinstance(details, Mapping) else getattr(details, name, "")


def _day(text):
    if not re.fullmatch(r"[0-9]{8}", text):
        raise _CalendarError("日期必须为 YYYYMMDD")
    try:
        return datetime.strptime(text, "%Y%m%d").date()
    except ValueError as exc:
        raise _CalendarError("日历日期无效") from exc


def _wall_time(day, clock, *, is_end=False):
    if clock == "2400" and is_end:
        return datetime.combine(day + timedelta(days=1), datetime.min.time())
    if not re.fullmatch(r"[0-9]{4}", clock):
        raise _CalendarError("时刻必须为 HHMM")
    hour, minute = int(clock[:2]), int(clock[2:])
    if hour > 23 or minute > 59:
        raise _CalendarError("日历时刻无效")
    return datetime.combine(day, datetime.min.time()).replace(hour=hour, minute=minute)


def _utc_boundary(wall, zone):
    """Reject nonexistent/ambiguous DST boundaries instead of guessing a fold."""
    candidates = set()
    for fold in (0, 1):
        candidate = wall.replace(tzinfo=zone, fold=fold).astimezone(UTC)
        if candidate.astimezone(zone).replace(tzinfo=None) == wall:
            candidates.add(candidate)
    if not candidates:
        raise _CalendarError("日历边界处于夏令时跳时，不存在该本地时刻")
    if len(candidates) != 1:
        raise _CalendarError("日历边界处于夏令时重复时刻，无法确定具体时段")
    return candidates.pop()


def _merge(intervals):
    merged = []
    for item in sorted(intervals, key=lambda interval: (interval.start, interval.end)):
        if merged and item.start < merged[-1].end:
            merged[-1] = _Interval(merged[-1].start, max(merged[-1].end, item.end))
        else:
            merged.append(item)
    return tuple(merged)


def _parse_hours(raw, zone):
    if not isinstance(raw, str) or not raw.strip():
        raise _CalendarError("日历为空")
    text = raw.strip().rstrip(";")
    entries = text.split(";")
    intervals = []
    dates, closed_dates, open_dates = set(), set(), set()
    for entry in entries:
        match = re.fullmatch(r"([0-9]{8}):(.*)", entry.strip())
        if not match:
            raise _CalendarError("日历条目格式无效")
        raw_day, sessions = match.groups()
        base_day = _day(raw_day)
        dates.add(base_day)
        if sessions.strip() == "CLOSED":
            closed_dates.add(base_day)
            continue
        open_dates.add(base_day)
        for session in sessions.split(","):
            parsed = re.fullmatch(r"(?:(?P<start_day>[0-9]{8}):)?(?P<start>[0-9]{4})-"
                                  r"(?:(?P<end_day>[0-9]{8}):)?(?P<end>[0-9]{4})", session.strip())
            if not parsed:
                raise _CalendarError("交易区间格式无效")
            start_day = _day(parsed["start_day"]) if parsed["start_day"] else base_day
            end_day = _day(parsed["end_day"]) if parsed["end_day"] else start_day
            start_wall = _wall_time(start_day, parsed["start"])
            end_wall = _wall_time(end_day, parsed["end"], is_end=True)
            # Older endpoints omit the end date. Only a strictly earlier
            # closing clock implies the next day; equal clocks are ambiguous.
            if not parsed["end_day"] and end_wall < start_wall:
                end_wall += timedelta(days=1)
            if end_wall <= start_wall:
                raise _CalendarError("交易区间结束时间不晚于开始时间")
            start, end = _utc_boundary(start_wall, zone), _utc_boundary(end_wall, zone)
            if end <= start:
                raise _CalendarError("交易区间顺序无效")
            intervals.append(_Interval(start, end))
            dates.add(start_day)
    if closed_dates & open_dates:
        raise _CalendarError("同一日期同时标为 CLOSED 和可交易，日历冲突")
    return _Calendar(_merge(intervals), frozenset(dates), frozenset(closed_dates))


def _active(calendar, now):
    return next((item for item in calendar.intervals if item.start <= now < item.end), None)


def decide_session(details, now: datetime) -> SessionDecision:
    """Classify an aware instant using this exact contract's IBKR calendars.

    Boundaries are start-inclusive/end-exclusive. session_end is UTC and marks
    the next policy transition (regular start/end or trading close). Missing,
    stale, invalid, or DST-ambiguous calendars fail closed. No fallback to a
    ticker's stock hours, weekday calendar, or a hard-coded market schedule is
    permitted. Refresh ContractDetails when the supplied dates no longer cover
    the relevant session; recheck this decision immediately before submission.
    """
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        return SessionDecision("closed", "当前时间缺少时区，无法核对合约交易日历")
    current = now.astimezone(UTC)
    zone_name = _field(details, "timeZoneId")
    if not isinstance(zone_name, str) or not zone_name.strip():
        return SessionDecision("closed", "IBKR 未返回合约时区 timeZoneId，暂不允许下单")
    try:
        zone = ZoneInfo(zone_name.strip())
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        return SessionDecision("closed", "无法识别 IBKR 合约时区，请刷新合约详情")
    try:
        trading = _parse_hours(_field(details, "tradingHours"), zone)
    except (_CalendarError, OverflowError) as exc:
        return SessionDecision("closed", f"IBKR tradingHours 无法确认：{exc}")
    try:
        liquid = _parse_hours(_field(details, "liquidHours"), zone)
    except (_CalendarError, OverflowError) as exc:
        return SessionDecision("closed", f"IBKR liquidHours 无法确认：{exc}")

    trading_now = _active(trading, current)
    local_day = current.astimezone(zone).date()
    if trading_now is None:
        if local_day in trading.closed_dates:
            return SessionDecision("closed", "IBKR 合约日历显示当日休市（CLOSED）")
        return SessionDecision("closed", "当前不在 IBKR 返回的合约交易时段内；可刷新合约日历")
    if local_day in trading.closed_dates:
        return SessionDecision("closed", "IBKR 日历的 CLOSED 日期与跨日交易区间冲突，请刷新合约详情")

    liquid_now = _active(liquid, current)
    if liquid_now:
        if local_day in liquid.closed_dates:
            return SessionDecision("closed", "IBKR liquidHours 的 CLOSED 日期与跨日区间冲突，请刷新合约详情")
        return SessionDecision("regular", "IBKR 合约常规交易时段；使用市价单", "MKT", False,
                               min(trading_now.end, liquid_now.end))

    # An overnight window can begin the day before the listed regular session.
    # An old liquid date alone cannot classify the following morning as GTH.
    end_day = (trading_now.end - timedelta(microseconds=1)).astimezone(zone).date()
    if local_day not in liquid.dates and not (local_day < end_day and end_day in liquid.dates):
        return SessionDecision("closed", "liquidHours 未覆盖当前交易时段，无法确认常规或延长交易")

    next_regular = [item.start for item in liquid.intervals if current < item.start < trading_now.end]
    phase_end = min([trading_now.end] + next_regular)
    return SessionDecision("extended", "IBKR 合约延长交易时段；使用保价限价单，允许常规时段外成交", "LMT", True, phase_end)
