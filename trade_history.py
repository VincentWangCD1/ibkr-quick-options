"""Account-scoped execution history, independent of current positions/orders.

Only broker-reported realized P&L is retained; an incomplete local opening
history is never used to invent FIFO costs. IBKR supplies execution corrections
with an incremented final execId component. See the execution-details API and
Trades Flex reporting guide referenced in README. No network calls occur here.
"""
from __future__ import annotations

from copy import copy
from contextlib import closing
import csv
from datetime import date, datetime, time as wall_time, timedelta, timezone
from decimal import Decimal, InvalidOperation
import io
import json
from pathlib import Path
import re
import sqlite3
from threading import RLock
from types import SimpleNamespace
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo


UTC = timezone.utc
PERIODS = {"today": 1, "7d": 7, "30d": 30, "60d": 60, "90d": 90, "mtd": None, "ytd": None}
API_HISTORY_NOTE = "当前账户全部品种、全部下单渠道；TWS 可返回的近期成交仍受 API 范围/客户端权限限制。较早记录请用 Flex 在线同步或导入报表；空白不代表没有交易。"


def period_dates(period, now=None, tz_name="Asia/Shanghai"):
    current = _timestamp(now or datetime.now(UTC)).astimezone(ZoneInfo(tz_name))
    day = current.date()
    if isinstance(period, (tuple, list)):
        if len(period) != 2 or not all(isinstance(value, str) and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value) for value in period):
            raise HistoryError("起止日期请填写 YYYY-MM-DD。")
        try:
            start, end = (date.fromisoformat(value) for value in period)
        except ValueError:
            raise HistoryError("起止日期中有无效的日历日期。") from None
        if start > end:
            raise HistoryError("开始日期须早于或等于结束日期。")
        if end > day:
            raise HistoryError("结束日期须为所选时区的今天或更早。")
        return start, end
    if not isinstance(period, str) or period not in PERIODS:
        raise HistoryError("不支持的历史区间。")
    start = day.replace(day=1) if period == "mtd" else day.replace(month=1, day=1) if period == "ytd" else day - timedelta(days=PERIODS[period] - 1)
    return start, day


# NYSE cash-equity core session closes, solely for historical P&L reporting.
# NYSE's published calendars: https://www.nyse.com/trade/hours-calendars and
# https://www.nyse.com/publicdocs/ICE_NYSE_2025_Yearly_Trading_Calendar.pdf
# 2025-01-09 was the subsequent National Day of Mourning closure.
_NYSE_CLOSED = {
    2025: {"01-01", "01-09", "01-20", "02-17", "04-18", "05-26", "06-19", "07-04", "09-01", "11-27", "12-25"},
    2026: {"01-01", "01-19", "02-16", "04-03", "05-25", "06-19", "07-03", "09-07", "11-26", "12-25"},
    2027: {"01-01", "01-18", "02-15", "03-26", "05-31", "06-18", "07-05", "09-06", "11-25", "12-24"},
    2028: {"01-17", "02-21", "04-14", "05-29", "06-19", "07-04", "09-04", "11-23", "12-25"},
}
_NYSE_EARLY_CLOSE = {
    "2025-07-03", "2025-11-28", "2025-12-24", "2026-11-27", "2026-12-24",
    "2027-11-26", "2028-07-03", "2028-11-24",
}


def last_completed_equity_session(now):
    """Trading date of the latest published NYSE core close, in New York."""
    current = _timestamp(now).astimezone(ZoneInfo("America/New_York"))
    day = current.date()
    while day.year in _NYSE_CLOSED:
        if day.weekday() < 5 and day.isoformat()[5:] not in _NYSE_CLOSED[day.year]:
            close_hour = 13 if day.isoformat() in _NYSE_EARLY_CLOSE else 16
            if day < current.date() or current.hour >= close_hour:
                return day
        day -= timedelta(days=1)
    return None


def execution_gross(value):
    """Use reported trade money; do not invent bond-percent or combo values."""
    if value.get("reported_gross") is not None:
        return abs(float(value["reported_gross"]))
    multiplier = value.get("multiplier")
    if multiplier is None or value.get("sec_type") in ("BOND", "BAG"):
        return None
    return float(Decimal(str(value["quantity"])) * Decimal(str(value["price"])) * Decimal(str(multiplier)))


class HistoryError(ValueError):
    pass


def _number(value, *, optional=False, positive=False, nonnegative=False):
    if value is None or value == "":
        if optional:
            return None
        raise HistoryError("成交记录缺少数值字段。")
    if isinstance(value, bool):
        raise HistoryError("成交记录的数值字段不能是布尔值。")
    try:
        result = Decimal(str(value).replace(",", ""))
    except InvalidOperation as exc:
        raise HistoryError("成交记录包含无效数值。") from exc
    if not result.is_finite() or abs(result) >= Decimal("1e100"):
        if optional:
            return None
        raise HistoryError("成交记录包含未返回或无效的数值。")
    if positive and result <= 0 or nonnegative and result < 0:
        raise HistoryError("成交记录数量、价格或乘数不符合要求。")
    return float(result)


def _timestamp(value, report_timezone=None):
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        if not re.search(r"[T ;](?:\d{2}:\d{2}:\d{2}|\d{6})", text):
            raise HistoryError("成交时间须包含日期、时分秒；不能仅用交易日期代替成交时刻。")
        parsed = None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            pass
        if parsed is None:
            # Flex supports configurable date/time separators and optional
            # named timezone. Interpret it only if the file or caller states it.
            match = re.fullmatch(r"(\d{8}|\d{4}-\d{2}-\d{2})[; T]+(\d{6}|\d{2}:\d{2}:\d{2})(?:\s+([^\s]+))?", text)
            if not match:
                raise HistoryError("成交时间须包含日期、时分秒；不能仅用交易日期代替成交时刻。")
            day, clock, named_zone = match.groups()
            parsed = datetime.strptime(day.replace("-", "") + clock.replace(":", ""), "%Y%m%d%H%M%S")
            report_timezone = named_zone or report_timezone
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        if not report_timezone:
            raise HistoryError("报表成交时间没有时区，请明确选择生成该 Flex 报表使用的时区后导入。")
        try:
            zone = ZoneInfo(report_timezone)
        except (KeyError, ValueError) as exc:
            raise HistoryError("无法识别报表时区，请使用 America/New_York 或 Asia/Shanghai 等名称。") from exc
        candidates = set()
        for fold in (0, 1):
            candidate = parsed.replace(tzinfo=zone, fold=fold).astimezone(UTC)
            if candidate.astimezone(zone).replace(tzinfo=None) == parsed:
                candidates.add(candidate)
        if len(candidates) != 1:
            raise HistoryError("报表时间落在夏令时重复或不存在的时刻，需包含明确 UTC 偏移。")
        parsed = candidates.pop()
    return parsed.astimezone(UTC)


def _scope(account, mode):
    account, mode = str(account or "").strip(), str(mode or "").upper()
    if not account or mode not in ("LIVE", "PAPER", "DEMO"):
        raise HistoryError("历史记录必须指定账户和 LIVE/PAPER/DEMO 模式。")
    return account, mode


def _correction(exec_id, source):
    match = re.fullmatch(r"(.+\..+)\.(\d+)", exec_id)
    if source != "DEMO" and match:
        return match[1], int(match[2])
    return exec_id, 0


def _normalize(record):
    value = dict(record)
    exec_id = str(value.get("exec_id") or "").strip()
    if not exec_id:
        raise HistoryError("缺少稳定的 IB Execution ID，不能可靠去重保存成交。")
    source = str(value.get("source", "API")).upper()
    if source not in ("API", "FLEX", "DEMO"):
        raise HistoryError("未知成交来源。")
    action = {"BOT": "BUY", "SLD": "SELL"}.get(str(value.get("action", "")).upper(), str(value.get("action", "")).upper())
    if action not in ("BUY", "SELL"):
        raise HistoryError("成交方向必须是 BUY/SELL 或 BOT/SLD。")
    stamp = _timestamp(value.get("timestamp"))
    conid = _number(value.get("conid"), positive=True)
    if conid != int(conid):
        raise HistoryError("成交合约 ID 必须为正整数。")
    quantity = _number(value.get("quantity"), nonnegative=True)
    price = _number(value.get("price"))
    sec_type = str(value.get("sec_type") or "UNKNOWN").upper()
    default_multiplier = 1 if sec_type in ("STK", "CASH", "CFD", "FUND", "CRYPTO", "CMDTY") else None
    multiplier = _number(value.get("multiplier") or default_multiplier, positive=True, optional=True)
    currency = str(value.get("currency") or "").upper()
    if not re.fullmatch(r"[A-Z]{3}", currency):
        raise HistoryError("成交记录缺少明确币种。")
    commission = _number(value.get("commission"), optional=True)
    pnl = _number(value.get("realized_pnl"), optional=True)
    fee_currency = str(value.get("commission_currency") or "").upper()
    pnl_currency = str(value.get("pnl_currency") or currency).upper()
    if commission is not None and not re.fullmatch(r"[A-Z]{3}", fee_currency):
        commission = None
    if pnl is not None and not re.fullmatch(r"[A-Z]{3}", pnl_currency):
        pnl = None
    value.update(exec_id=exec_id, timestamp=stamp.isoformat(), conid=int(conid), action=action,
                 quantity=quantity, price=price, multiplier=multiplier, currency=currency,
                 commission=commission, commission_currency=fee_currency,
                 realized_pnl=pnl, pnl_currency=pnl_currency, source=source, sec_type=sec_type,
                 reported_gross=_number(value.get("reported_gross"), optional=True))
    value["_commission_source"] = source if commission is not None else None
    value["_realized_pnl_source"] = source if pnl is not None else None
    for field in ("symbol", "local_symbol", "sec_type", "expiry", "right"):
        value[field] = str(value.get(field) or "")
    value["strike"] = _number(value.get("strike"), optional=True)
    return value


def saved_report_windows(path, account, mode, query_id, tz_name):
    """Read only coverage committed with the executions in this ledger."""
    path = Path(path)
    if not path.is_file():
        return []
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.2)) as db:
            today = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
            rows = db.execute("SELECT first,last FROM flex_coverage WHERE account=? AND mode=? AND query_id=? AND zone=? AND checked_day=?",
                              (account, mode, query_id, tz_name, today)).fetchall()
    except sqlite3.OperationalError:
        # Older ledgers have no coverage metadata. Sync them normally.
        return []
    return [(date.fromisoformat(first), date.fromisoformat(last)) for first, last in rows]


class TradeHistoryStore:
    def __init__(self, path=None):
        self.path = str(path) if path is not None else ":memory:"
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self._db = sqlite3.connect(self.path, timeout=15, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS executions (
              account TEXT NOT NULL, mode TEXT NOT NULL, exec_id TEXT NOT NULL,
              family TEXT NOT NULL, revision INTEGER NOT NULL,
              stamp REAL NOT NULL, payload TEXT NOT NULL,
              PRIMARY KEY(account,mode,exec_id));
            CREATE INDEX IF NOT EXISTS executions_scope ON executions(account,mode,stamp);
            CREATE INDEX IF NOT EXISTS executions_corrections ON executions(account,mode,family,revision,exec_id);
            CREATE INDEX IF NOT EXISTS executions_trade_date ON executions(account,mode,json_extract(payload,'$.trade_date'));
            CREATE TABLE IF NOT EXISTS reports (
              account TEXT NOT NULL, mode TEXT NOT NULL, exec_id TEXT NOT NULL,
              payload TEXT NOT NULL, PRIMARY KEY(account,mode,exec_id));
            CREATE TABLE IF NOT EXISTS order_submission_times (
              account TEXT NOT NULL, mode TEXT NOT NULL, order_ref TEXT NOT NULL, stamp REAL NOT NULL,
              PRIMARY KEY(account,mode,order_ref));
            CREATE TABLE IF NOT EXISTS scopes (
              account TEXT NOT NULL, mode TEXT NOT NULL, recording_since TEXT NOT NULL,
              PRIMARY KEY(account,mode));
            CREATE TABLE IF NOT EXISTS imports (
              account TEXT NOT NULL, mode TEXT NOT NULL, filename TEXT NOT NULL,
              imported_at TEXT NOT NULL, rows INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS flex_coverage (
              account TEXT NOT NULL, mode TEXT NOT NULL, query_id TEXT NOT NULL, zone TEXT NOT NULL,
              first TEXT NOT NULL, last TEXT NOT NULL, checked_day TEXT NOT NULL, PRIMARY KEY(account,mode,query_id,zone,first,last));
            CREATE TABLE IF NOT EXISTS flex_book_entries (
              account TEXT NOT NULL, mode TEXT NOT NULL, trade_id TEXT NOT NULL,
              stamp REAL NOT NULL, payload TEXT NOT NULL,
              PRIMARY KEY(account,mode,trade_id));
            CREATE INDEX IF NOT EXISTS executions_flex_trade_id
              ON executions(account,mode,json_extract(payload,'$.flex_trade_id'));
        """)

    def close(self):
        with self._lock:
            self._db.close()

    def submission_time(self, account, mode, ref, stamp):
        with self._lock, self._db:
            if stamp is not None:
                self._db.execute("INSERT OR IGNORE INTO order_submission_times VALUES(?,?,?,?)", (account, mode, ref, stamp))
            row = self._db.execute("SELECT stamp FROM order_submission_times WHERE account=? AND mode=? AND order_ref=?",
                                   (account, mode, ref)).fetchone()
            return row[0] if row else None

    def _touch(self, account, mode):
        self._db.execute("INSERT OR IGNORE INTO scopes VALUES(?,?,?)", (account, mode, datetime.now(UTC).isoformat()))

    @staticmethod
    def _merge(old, new):
        if not old:
            return new
        if old["conid"] != new["conid"] or old["action"] != new["action"]:
            raise HistoryError("同一成交 ID 的合约或方向冲突，导入已停止。")
        if abs(_timestamp(old["timestamp"]).timestamp() - _timestamp(new["timestamp"]).timestamp()) > 2:
            raise HistoryError("同一成交 ID 的时间冲突。在线同步请核对「Flex 在线同步设置」中的报表原始时区；文件导入请核对右上方的报表原始时区。")
        for field in ("quantity", "price", "multiplier", "currency"):
            if field == "multiplier" and (old[field] is None or new[field] is None):
                continue
            if old[field] != new[field]:
                raise HistoryError("同一成交 ID 的数量、价格或币种冲突；需券商更正版本的 Execution ID。")
        rank = {"API": 1, "DEMO": 1, "FLEX": 2}
        merged = dict(old)
        financial = {"commission", "commission_currency", "realized_pnl", "pnl_currency",
                     "_commission_source", "_realized_pnl_source"}
        if rank[new["source"]] >= rank[old["source"]]:
            merged.update({key: value for key, value in new.items() if key not in financial and value not in (None, "")})
        if old["source"] == "API" and new["source"] == "FLEX":
            # An API client's orderId is not a Flex IB Order ID.
            merged["order_id"] = new.get("order_id")
        if not merged.get("perm_id") and new.get("perm_id"):
            merged["perm_id"] = new["perm_id"]
        # An old API fill without fees must not erase a late commission report
        # or final Flex adjustment. Populate still-unknown fields from either.
        for field, currency in (("commission", "commission_currency"), ("realized_pnl", "pnl_currency")):
            old_source = old.get("_" + field + "_source") or old["source"]
            if new.get(field) is not None and (old.get(field) is None or rank[new["source"]] >= rank[old_source]):
                merged[field], merged[currency] = new[field], new[currency]
                merged["_" + field + "_source"] = new["source"]
        return merged

    def _write(self, account, mode, record):
        value = _normalize(record)
        existing = self._db.execute("SELECT payload FROM executions WHERE account=? AND mode=? AND exec_id=?",
                                    (account, mode, value["exec_id"])).fetchone()
        merged = self._merge(json.loads(existing[0]) if existing else None, value)
        report = self._db.execute("SELECT payload FROM reports WHERE account=? AND mode=? AND exec_id=?",
                                 (account, mode, value["exec_id"])).fetchone()
        if report:
            reported = json.loads(report[0])
            for field, currency in (("commission", "commission_currency"), ("realized_pnl", "pnl_currency")):
                if reported.get(field) is not None and (merged.get("_" + field + "_source") != "FLEX" or merged.get(field) is None):
                    merged[field], merged[currency] = reported[field], reported[currency]
                    merged["_" + field + "_source"] = "API"
        family, revision = _correction(value["exec_id"], value["source"])
        payload = json.dumps(merged, ensure_ascii=False, sort_keys=True)
        self._db.execute("INSERT OR REPLACE INTO executions VALUES(?,?,?,?,?,?,?)",
                         (account, mode, value["exec_id"], family, revision,
                          _timestamp(value["timestamp"]).timestamp(), payload))
        self._touch(account, mode)
        return existing is None

    def record_execution(self, account, mode, record):
        account, mode = _scope(account, mode)
        with self._lock, self._db:
            return self._write(account, mode, record)

    def record_ib_fill(self, mode, fill, account=None):
        execution, contract = fill.execution, fill.contract
        actual = str(getattr(execution, "acctNumber", "") or "")
        if account is not None and actual != account:
            raise HistoryError("成交所属账户与当前历史账户不符。")
        report = getattr(fill, "commissionReport", None)
        valid_report = report is not None and getattr(report, "execId", "") == execution.execId
        # ib_async normalizes unknown realizedPNL to zero. Never trust its
        # post-wrapper fill object for this value; raw callbacks supply it.
        return self.record_execution(actual, mode, dict(
            exec_id=execution.execId, timestamp=execution.time, conid=contract.conId,
            symbol=contract.symbol, local_symbol=contract.localSymbol, sec_type=contract.secType,
            expiry=contract.lastTradeDateOrContractMonth[:8], right=contract.right,
            strike=contract.strike, multiplier=contract.multiplier or None,
            currency=contract.currency, action=execution.side, quantity=execution.shares,
            price=execution.price, source="API", order_id=execution.orderId, perm_id=execution.permId,
            exchange=getattr(execution, "exchange", ""),
            commission=getattr(report, "commission", None) if valid_report else None,
            commission_currency=getattr(report, "currency", "") if valid_report else "",
            realized_pnl=None, pnl_currency=contract.currency))

    def record_ib_commission(self, mode, account, report):
        """Accept the RAW report, before ib_async replaces unknown P&L with 0."""
        account, mode = _scope(account, mode)
        exec_id = str(getattr(report, "execId", "") or "")
        if not exec_id:
            return False
        currency = str(getattr(report, "currency", "") or "").upper()
        valid_currency = re.fullmatch(r"[A-Z]{3}", currency)
        data = dict(commission=_number(getattr(report, "commission", getattr(report, "commissionAndFees", None)), optional=True) if valid_currency else None,
                    commission_currency=currency if valid_currency else None,
                    realized_pnl=_number(getattr(report, "realizedPNL", None), optional=True) if valid_currency else None,
                    pnl_currency=currency if valid_currency else None)
        if data["commission"] is None:
            data["commission_currency"] = None
        if data["realized_pnl"] is None:
            data["pnl_currency"] = None
        with self._lock, self._db:
            old = self._db.execute("SELECT payload FROM reports WHERE account=? AND mode=? AND exec_id=?", (account, mode, exec_id)).fetchone()
            if old:
                data = {**json.loads(old[0]), **{key: val for key, val in data.items() if val is not None}}
            self._db.execute("INSERT OR REPLACE INTO reports VALUES(?,?,?,?)", (account, mode, exec_id, json.dumps(data)))
            existing = self._db.execute("SELECT payload FROM executions WHERE account=? AND mode=? AND exec_id=?", (account, mode, exec_id)).fetchone()
            if existing:
                value = json.loads(existing[0])
                for field, currency_field in (("commission", "commission_currency"), ("realized_pnl", "pnl_currency")):
                    if data.get(field) is not None and (value.get("_" + field + "_source") != "FLEX" or value.get(field) is None):
                        value[field], value[currency_field] = data[field], data[currency_field]
                        value["_" + field + "_source"] = "API"
                self._db.execute("UPDATE executions SET payload=? WHERE account=? AND mode=? AND exec_id=?",
                                 (json.dumps(value, ensure_ascii=False, sort_keys=True), account, mode, exec_id))
            self._touch(account, mode)
        return True

    def position_opening_cost(self, account, mode, conid, held, demo_session=None):
        from position_add import first_buy_cost
        account, mode = _scope(account, mode)
        # Apply corrections (including busts and reassignments) before filtering the contract.
        with self._lock:
            rows = self._db.execute("""
                SELECT payload FROM (
                    SELECT payload, stamp, exec_id, ROW_NUMBER() OVER (
                        PARTITION BY family ORDER BY revision DESC, exec_id DESC) AS latest
                    FROM executions WHERE account=? AND mode=?)
                WHERE latest=1 AND json_extract(payload, '$.conid')=?
                ORDER BY stamp, exec_id""", (account, mode, conid)).fetchall()
        fills = [json.loads(r["payload"]) for r in rows]
        if demo_session:
            fills = [r for r in fills if r["exec_id"].startswith(f"DEMO:{demo_session}:")]
        return first_buy_cost(fills, held)

    def last_session_pnl(self, account, mode, now=None):
        account, mode = _scope(account, mode)
        current = _timestamp(now or datetime.now(UTC))
        session = last_completed_equity_session(current)
        result = dict(date=session.isoformat() if session else None, by_currency={}, fills=0,
                      missing_pnl_count=0, last_execution_at=None)
        if session is None:
            return result
        zone = ZoneInfo("America/New_York")
        start = datetime.combine(session, wall_time(), zone)
        end = datetime.combine(session + timedelta(days=1), wall_time(), zone)
        # Prefer the broker's trading date for overnight trades. Legacy/API
        # rows use their NY timestamp. Corrections can move a fill out of this
        # day; check the latest family revision against the complete ledger.
        with self._lock:
            rows = self._db.execute("""
                WITH candidates AS (
                    SELECT * FROM executions INDEXED BY executions_trade_date
                        WHERE account=? AND mode=? AND json_extract(payload,'$.trade_date')=?
                    UNION ALL
                    SELECT * FROM executions INDEXED BY executions_scope WHERE account=? AND mode=? AND stamp>=? AND stamp<?
                        AND json_extract(payload,'$.trade_date') IS NULL)
                SELECT e.payload FROM candidates e WHERE e.stamp<=? AND json_extract(e.payload,'$.quantity')>0
                  AND NOT EXISTS (SELECT 1 FROM executions newer WHERE newer.account=e.account
                      AND newer.mode=e.mode AND newer.family=e.family AND
                      (newer.revision,newer.exec_id)>(e.revision,e.exec_id))
                ORDER BY e.stamp, e.exec_id""",
                (account, mode, result["date"], account, mode, start.timestamp(), end.timestamp(), current.timestamp())).fetchall()
        amounts = {}
        for row in rows:
            fill = json.loads(row["payload"])
            currency = fill["pnl_currency"] or fill["currency"]
            subtotal = amounts.setdefault(currency, None)
            if fill["realized_pnl"] is None:
                result["missing_pnl_count"] += 1
            else:
                amounts[currency] = (subtotal or Decimal(0)) + Decimal(str(fill["realized_pnl"]))
            result["last_execution_at"] = fill["timestamp"]
        result["by_currency"] = {code: float(amount) if amount is not None else None for code, amount in amounts.items()}
        result["fills"] = len(rows)
        return result

    def history(self, account, mode, period="today", now=None, tz_name="Asia/Shanghai"):
        account, mode = _scope(account, mode)
        try:
            zone = ZoneInfo(tz_name)
        except (KeyError, ValueError) as exc:
            raise HistoryError("无法识别历史展示时区。") from exc
        current = _timestamp(now or datetime.now(UTC)).astimezone(zone)
        start_day, end_day = period_dates(period, current, tz_name)
        start = datetime.combine(start_day, wall_time(), zone)
        end = datetime.combine(end_day + timedelta(days=1), wall_time(), zone)
        with self._lock:
            # Choose newest correction across the full stored scope FIRST;
            # a corrected timestamp may move a fill out of this period.
            records = self._db.execute("SELECT exec_id,family,revision,stamp,payload FROM executions WHERE account=? AND mode=?",
                                       (account, mode)).fetchall()
            scope = self._db.execute("SELECT recording_since FROM scopes WHERE account=? AND mode=?", (account, mode)).fetchone()
            import_count = self._db.execute("SELECT COUNT(*) FROM imports WHERE account=? AND mode=?", (account, mode)).fetchone()[0]
            books = self._db.execute("""
                SELECT b.stamp, b.payload FROM flex_book_entries b
                WHERE b.account=? AND b.mode=? AND b.stamp>=? AND b.stamp<?
                  AND NOT EXISTS (SELECT 1 FROM executions e
                      WHERE e.account=b.account AND e.mode=b.mode AND json_extract(e.payload,'$.flex_trade_id')=b.trade_id)
                ORDER BY b.stamp DESC, b.trade_id""", (account, mode, start.timestamp(), end.timestamp())).fetchall()
        book_entries = []
        for book in books:
            entry = json.loads(book["payload"])
            stamp = datetime.fromtimestamp(book["stamp"], zone)
            book_entries.append({**entry, "date": stamp.strftime("%Y-%m-%d"), "time": stamp.strftime("%H:%M:%S")})
        latest = {}
        for item in records:
            if item["family"] not in latest or (item["revision"], item["exec_id"]) > (latest[item["family"]]["revision"], latest[item["family"]]["exec_id"]):
                latest[item["family"]] = item
        rows, all_times, option_fills = [], [], []
        for item in latest.values():
            value = json.loads(item["payload"])
            if value["quantity"] == 0:
                continue  # A zero-quantity correction busts its prior fill.
            all_times.append(value["timestamp"])
            if value["sec_type"] in ("OPT", "FOP") and item["stamp"] < end.timestamp():
                option_fills.append(value)
            if not start.timestamp() <= item["stamp"] < end.timestamp():
                continue
            displayed = _timestamp(value["timestamp"]).astimezone(zone)
            gross = execution_gross(value)
            signed = Decimal(str(gross)) * (1 if value["action"] == "SELL" else -1) if gross is not None else None
            fee = value.get("commission")
            net = (float(signed - Decimal(str(fee))) if signed is not None and fee is not None and value["commission_currency"] == value["currency"] else None)
            rows.append({**{key: val for key, val in value.items() if not key.startswith("_")},
                         "date": displayed.strftime("%Y-%m-%d"), "time": displayed.strftime("%H:%M:%S"),
                         "gross": gross, "net_cash": net})
        rows.sort(key=lambda item: (item["timestamp"], item["exec_id"]), reverse=True)
        totals = _totals(rows)
        missing_pnl = sum(row["realized_pnl"] is None for row in rows)
        missing_fee = sum(row["commission"] is None for row in rows)
        since = scope[0] if scope else None
        since_text = _timestamp(since).astimezone(zone).strftime("%Y-%m-%d %H:%M") if since else "尚无记录"
        label = f"本地开始记录：{since_text}；已导入报表 {import_count} 次。仅汇总已接收或导入的成交，历史可能不完整。"
        if missing_pnl or missing_fee:
            label += f" {missing_pnl} 笔实现盈亏、{missing_fee} 笔手续费待券商回报；合计为已知小计。"
        from history_groups import closing_trades
        return dict(account=account, mode=mode, period=period, tz_name=tz_name,
                    closing_trades=closing_trades(option_fills, rows, tz_name),
                    start_date=start_day.isoformat(), end_date=end_day.isoformat(), rows=rows,
                    last_session_pnl=self.last_session_pnl(account, mode, current),
                    totals_by_currency=totals, recording_since=since,
                    first_execution_at=min(all_times, default=None), last_execution_at=max(all_times, default=None),
                    coverage_label=label, missing_pnl_count=missing_pnl, missing_commission_count=missing_fee,
                    import_count=import_count, book_entries=book_entries, api_history_note=API_HISTORY_NOTE)

    def import_flex(self, path, account, mode, tz_name=None):
        account, mode = _scope(account, mode)
        file = Path(path)
        if file.suffix.lower() not in (".xml", ".csv") or file.stat().st_size > 50_000_000:
            raise HistoryError("请选择小于 50 MB 的 IBKR Flex 成交明细 XML/CSV。")
        raw = file.read_text(encoding="utf-8-sig")
        return self.import_flex_text(raw, file.suffix.lower(), account, mode, tz_name, file.name)

    def import_flex_text(self, raw, extension, account, mode, tz_name=None, filename="Flex 在线同步"):
        return self.import_flex_batch([(raw, extension)], account, mode, tz_name, filename)

    def import_flex_batch(self, reports, account, mode, tz_name=None, filename="Flex 在线同步", *, query_id=None, report_windows=()):
        account, mode = _scope(account, mode)
        records, books = [], []
        skipped = missing_ids = 0
        for raw, extension in reports:
            parsed, book_rows, ignored, missing = _parse_flex(raw, extension, account, tz_name)
            records.extend(parsed)
            books.extend(book_rows)
            skipped += ignored
            missing_ids += missing
        if missing_ids and not records and not books:
            raise HistoryError(f"{missing_ids} 行缺少可用的 IB Execution ID，未导入。请核对导出文件中该列的实际值。")
        added = new_books = 0
        with self._lock, self._db:
            for record in records:
                added += self._write(account, mode, record)
            for book in books:
                key = (account, mode, book["flex_trade_id"])
                payload = json.dumps(book, ensure_ascii=False)
                stamp = _timestamp(book["timestamp"]).timestamp()
                inserted = self._db.execute("INSERT OR IGNORE INTO flex_book_entries VALUES(?,?,?,?,?)",
                                            (*key, stamp, payload)).rowcount
                if inserted:
                    new_books += 1
                else:
                    previous = json.loads(self._db.execute("SELECT payload FROM flex_book_entries WHERE account=? AND mode=? AND trade_id=?",
                                                          key).fetchone()[0])
                    if (abs(_timestamp(previous["timestamp"]).timestamp() - stamp) > 2
                            or any(previous[field] != book[field] for field in ("quantity", "action", "currency"))
                            or (previous.get("conid") and book.get("conid") and previous["conid"] != book["conid"])):
                        raise HistoryError("同一 Trade ID 的记账明细冲突，请核对报表原始时区和原始记录。")
                    previous.update({field: value for field, value in book.items() if value not in ("", None)})
                    self._db.execute("UPDATE flex_book_entries SET stamp=?,payload=? WHERE account=? AND mode=? AND trade_id=?",
                                     (stamp, json.dumps(previous, ensure_ascii=False), *key))
            self._db.execute("INSERT INTO imports VALUES(?,?,?,?,?)", (account, mode, filename, datetime.now(UTC).isoformat(), len(records) + len(books)))
            if reports and query_id and tz_name and not missing_ids:
                today = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
                self._db.executemany("INSERT OR REPLACE INTO flex_coverage VALUES (?,?,?,?,?,?,?)",
                                     [(account, mode, query_id, tz_name, first.isoformat(), last.isoformat(), today)
                                      for first, last in report_windows])
        return dict(imported=added, duplicates=len(records) - added, skipped=skipped + missing_ids,
                    missing_execution_ids=missing_ids, book_rows=len(books), new_book_entries=new_books,
                    filename=filename, report_timezone=tz_name or "文件内明确时区")


def _totals(rows):
    groups = {}
    def group(currency):
        if currency not in groups:
            groups[currency] = dict(count=0, quantity=0., gross=0., gross_buy=0., gross_sell=0.,
                                    commission=None, realized_pnl=None, net_cash=None,
                                    known_pnl_count=0, missing_pnl_count=0,
                                    known_commission_count=0, missing_commission_count=0,
                                    pnl_complete=True, commission_complete=True)
        return groups[currency]
    def add(target, key, value):
        if value is None:
            return
        target[key] = float(Decimal(str(target[key] or 0)) + Decimal(str(value)))
    for row in rows:
        base = group(row["currency"])
        base["count"] += 1
        for key in ("quantity", "gross"):
            add(base, key, row[key])
        add(base, "gross_buy" if row["action"] == "BUY" else "gross_sell", row["gross"])
        if row["net_cash"] is not None:
            add(base, "net_cash", row["net_cash"])
        if row["commission"] is None:
            base["missing_commission_count"] += 1
            base["commission_complete"] = False
        else:
            fee_group = group(row["commission_currency"])
            add(fee_group, "commission", row["commission"])
            fee_group["known_commission_count"] += 1
        if row["realized_pnl"] is None:
            base["missing_pnl_count"] += 1
            base["pnl_complete"] = False
        else:
            pnl_group = group(row["pnl_currency"])
            add(pnl_group, "realized_pnl", row["realized_pnl"])
            pnl_group["known_pnl_count"] += 1
    return groups


def _key(value):
    return re.sub(r"[^a-z0-9]", "", str(value).lower())


def _flex_trade_date(value):
    text = str(value).strip()
    if re.fullmatch(r"[0-9]{8}", text):
        text = f"{text[:4]}-{text[4:6]}-{text[6:]}"
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        return None


def _parse_flex(raw, extension, account, report_timezone):
    if extension == ".xml":
        if "<!DOCTYPE" in raw.upper() or "<!ENTITY" in raw.upper():
            raise HistoryError("不支持含外部实体或文档类型声明的 XML。")
        try:
            root = ET.fromstring(raw)
        except ET.ParseError as exc:
            raise HistoryError("XML 报表格式无效。") from exc
        if root.tag.split("}")[-1] not in ("FlexQueryResponse", "FlexStatements", "FlexStatement"):
            raise HistoryError("请选择 IBKR Flex XML 报表。")
        raw_rows = []
        statements = [node for node in root.iter() if node.tag.split("}")[-1] == "FlexStatement"]
        if not any(node.attrib.get("accountId") == account for node in statements):
            raise HistoryError("Flex 报表不含当前账户，未导入。")
        if not any(node.tag.split("}")[-1] == "Trades" for statement in statements
                   if statement.attrib.get("accountId") == account for node in statement.iter()):
            raise HistoryError("Flex 查询未包含 Trades 成交明细，请添加 Executions 后重新同步。")
        for statement in statements:
            inherited_account = statement.attrib.get("accountId", "")
            for node in statement.iter():
                if node.tag.split("}")[-1] == "Trade":
                    raw_rows.append({"accountId": inherited_account, **node.attrib})
    else:
        reader = csv.DictReader(io.StringIO(raw))
        fields = {_key(name) for name in (reader.fieldnames or [])}
        if not ({"ibexecid", "ibexecutionid"} & fields) or not ({"accountid", "clientaccountid"} & fields):
            raise HistoryError("需要 Flex Trades/Executions CSV，包含 Account ID、IB Execution ID；不支持活动报表汇总 CSV。")
        raw_rows = list(reader)
        if not any(str(next((value for key, value in row.items() if _key(key) in ("accountid", "clientaccountid")), "")) == account for row in raw_rows):
            raise HistoryError("Flex 报表不含当前账户，未导入。")
    records, books = [], []
    skipped = missing_ids = 0
    for raw_row in raw_rows:
        row = {_key(key): value for key, value in raw_row.items()}
        def get(*keys, default=""):
            return next((row[_key(key)] for key in keys if row.get(_key(key)) not in (None, "")), default)
        if str(get("accountId", "clientAccountID")) != account:
            skipped += 1
            continue
        asset = str(get("assetCategory", "assetClass", default="UNKNOWN")).upper()
        level = str(get("levelOfDetail")).upper()
        if level and level not in ("EXECUTION", "EXECUTIONS"):
            skipped += 1
            continue
        transaction = str(get("transactionType")).upper()
        if "CANCEL" in transaction or "CORRECT" in transaction or get("origTradeID", "originalTradeID") not in ("", "0"):
            raise HistoryError("报表含取消或更正交易，请先导出不含取消记录的最终 Executions 明细，避免重复计算。")
        exec_id = str(get("ibExecID", "ibExecutionID")).strip()
        trade_id = str(get("tradeID")).strip()
        if not exec_id and not (transaction == "BOOKTRADE" and re.fullmatch(r"[0-9]+", trade_id) and trade_id.strip("0")):
            missing_ids += 1
            continue
        stamp = get("dateTime")
        if not stamp:
            stamp = str(get("tradeDate")) + ";" + str(get("tradeTime"))
        quantity = _number(get("quantity"))
        action = str(get("buySell")).upper()
        if action not in ("BUY", "SELL") or (quantity < 0 and action != "SELL"):
            raise HistoryError("Flex 数量符号与买卖方向不符。")
        commission = _number(get("ibCommission", default=None), optional=True)
        # Flex carries commissions as signed cash effects (expense negative),
        # while this UI and CommissionReport use positive expense amounts.
        commission = -commission if commission is not None else None
        currency = str(get("currency", "currencyPrimary")).upper()
        detail = dict(
            timestamp=_timestamp(stamp, str(get("timeZone", "timeZoneId", default=report_timezone or "")) or None),
            conid=get("conid"), symbol=get("underlyingSymbol", "symbol"),
            local_symbol=get("symbol", "description"), sec_type=asset, expiry=get("expiry"),
            right=get("putCall"), strike=get("strike", default=None), multiplier=get("multiplier"),
            currency=currency, action=action, quantity=abs(quantity), price=get("tradePrice", "price"),
            reported_gross=get("tradeMoney", "proceeds", default=None),
            commission=commission, commission_currency=get("ibCommissionCurrency"),
            realized_pnl=get("fifoPnlRealized", "realizedPnl", default=None), pnl_currency=currency,
            exchange=get("exchange"), order_type=get("orderType"), tif=get("timeInForce", "tif"),
            source="FLEX", order_id=get("ibOrderID"), open_close=get("openCloseIndicator"),
            flex_trade_id=trade_id, trade_date=_flex_trade_date(get("tradeDate")))
        if exec_id:
            records.append(_normalize(dict(detail, exec_id=exec_id)))
        else:
            # A statement booking has its own identity. Keep it out of execution
            # matching, positions, order groups and realized execution totals.
            books.append(dict(flex_trade_id=trade_id, timestamp=detail["timestamp"].isoformat(), conid=detail["conid"],
                              symbol=detail["local_symbol"] or detail["symbol"], action=action,
                              quantity=abs(quantity), price=_number(detail["price"], optional=True), currency=currency,
                              gross=_number(detail["reported_gross"], optional=True), commission=commission,
                              commission_currency=detail["commission_currency"],
                              realized_pnl=_number(detail["realized_pnl"], optional=True)))
    if not records and not books and not missing_ids and any(str(next((value for key, value in row.items() if _key(key) in ("accountid", "clientaccountid")), "")) == account for row in raw_rows):
        raise HistoryError("报表只含汇总行；请将 Trades 明细层级设为 Executions。")
    return records, books, skipped, missing_ids


def _attach_execution_time_compat(ib):
    """Preserve the explicit UTC wire form before ib_async 2.1 decodes it.

    Version 2.1 parses YYYYmmdd-HH:MM:SS as naive and then assigns the
    host/TWS zone. Its upstream fix is ib_async #230/#232. Normalize only
    that unambiguous form into the already-supported named-zone form.
    This is local to this decoder; other timestamps and installed packages
    are unchanged. The handler table holds bound methods, so both entry
    points must be replaced before connecting.
    """
    decoder = getattr(getattr(ib, "client", None), "decoder", None)
    if decoder is None or getattr(decoder, "_qopt_execution_time_compat", False) is True:
        return False
    handlers = getattr(decoder, "handlers", None)
    original = getattr(decoder, "execDetails", None)
    if not isinstance(handlers, dict) or not callable(original) or 11 not in handlers:
        return False

    def execution_fields(fields):
        # ib_async 2.1's execution frame has timeStr at field 15 (message 11).
        # Never change the caller's list, a zone-bearing timestamp, or an
        # older TWS-local space-separated timestamp.
        if (len(fields) > 15 and str(fields[0]) == "11" and isinstance(fields[15], str)
                and re.fullmatch(r"\d{8}-\d{2}:\d{2}:\d{2}", fields[15])):
            fields = list(fields)
            fields[15] = fields[15].replace("-", " ", 1) + " UTC"
        return original(fields)

    decoder.execDetails = execution_fields
    handlers[11] = execution_fields
    decoder._qopt_execution_time_compat = True
    return True


def attach_history_callbacks(ib, store_provider, context_provider, log=lambda message: None):
    """Capture raw reports before ib_async loses unknown-P&L information.

    Instance-only wrappers preserve the library's normal event/position/order
    updates. They also capture executions without a Trade object. No transport
    connection is opened and no third-party package is modified.
    """
    _attach_execution_time_compat(ib)
    wrapper = ib.wrapper
    if getattr(wrapper, "_qopt_history_attached", False) is True:
        return False
    original_execution, original_commission = wrapper.execDetails, wrapper.commissionReport
    scopes, pending_reports = {}, {}

    def safe(call):
        try:
            call()
        except Exception as exc:
            log(f"成交历史保存失败：{exc}")

    def execution(req_id, contract, value):
        mode, account = context_provider()
        if account and str(getattr(value, "acctNumber", "")) == account:
            scopes[value.execId] = (mode, account)
            fill = SimpleNamespace(contract=contract, execution=value, commissionReport=None)
            safe(lambda: store_provider().record_ib_fill(mode, fill, account))
            pending = pending_reports.pop(value.execId, None)
            if pending and pending[0] == (mode, account):
                safe(lambda: store_provider().record_ib_commission(mode, account, pending[1]))
        return original_execution(req_id, contract, value)

    def commission(value):
        raw = copy(value)
        mode, account = context_provider()
        known = scopes.get(raw.execId)
        if known is None:
            fill = getattr(wrapper, "fills", {}).get(raw.execId)
            if fill and str(getattr(fill.execution, "acctNumber", "")) == account:
                known = (mode, account)
        if account and known == (mode, account):
            safe(lambda: store_provider().record_ib_commission(mode, account, raw))
        elif account and known is None:
            pending_reports[raw.execId] = ((mode, account), raw)
            while len(pending_reports) > 4096:
                pending_reports.pop(next(iter(pending_reports)))
        return original_commission(value)

    wrapper.execDetails, wrapper.commissionReport = execution, commission
    wrapper._qopt_history_attached = True
    return True
