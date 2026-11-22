"""Read-only IBKR Flex downloads. Credentials never appear in errors/logs."""
import base64
from collections import deque
import ctypes
from ctypes import wintypes
from datetime import date, datetime, time as day_time, timedelta, timezone
import hashlib
from http.client import IncompleteRead
import json
import math
import os
from pathlib import Path
import re
import sqlite3
from contextlib import closing
import ssl
import threading
import time
import urllib.parse
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo

from trade_history import HistoryError, period_dates

BASE = "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService/"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise HistoryError("Flex 服务返回意外重定向，已停止请求。")


def http_get(endpoint, params):
    request = urllib.request.Request(BASE + endpoint + "?" + urllib.parse.urlencode(params),
                                     headers={"User-Agent": "Python/IBKR-QuickOptions-1.8"})
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=12) as response:
            body = response.read(50_000_001)
    except HistoryError:
        raise
    except urllib.error.HTTPError as exc:
        if exc.code in (408, 429, 500, 502, 503, 504):
            retry_header = (exc.headers or {}).get("Retry-After", "")
            seconds = max(60, int(retry_header)) if re.fullmatch(r"[0-9]{1,5}", retry_header) else 60
            raise FlexServiceError(f"Flex 服务暂时不可用（HTTP {exc.code}）。", f"HTTP{exc.code}", seconds, True) from None
        raise HistoryError(f"Flex 网络请求失败（HTTP {exc.code}）。") from None
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, ssl.SSLError):
            raise HistoryError("Flex 安全连接验证失败，请检查网络设置。") from None
        raise FlexServiceError("Flex 网络连接暂时不可用。", "NETWORK", 30, True) from None
    except (TimeoutError, ConnectionError, IncompleteRead):
        raise FlexServiceError("Flex 网络连接暂时不可用。", "NETWORK", 30, True) from None
    except Exception:
        raise HistoryError("Flex 网络请求失败，请检查网络、服务状态或 Token；凭据未记录。") from None
    if len(body) > 50_000_000:
        raise HistoryError("Flex 报表超过 50 MB，请缩小区间。")
    return body.decode("utf-8-sig")


def xml_root(raw):
    if "<!DOCTYPE" in raw.upper() or "<!ENTITY" in raw.upper():
        raise HistoryError("不支持的 Flex XML 声明。")
    try:
        return ET.fromstring(raw)
    except ET.ParseError:
        raise HistoryError("Flex 需使用 XML 输出格式，服务返回的内容不是有效 XML。") from None


class FlexServiceError(HistoryError):
    def __init__(self, message, code, retry_after=0, auto_retry=False):
        super().__init__(message)
        self.code = code
        self.retry_after = retry_after
        self.auto_retry = auto_retry


class FailedAttemptHold:
    """Share the app's retry deadline across restarts and queries using the same token."""
    schema = "CREATE TABLE IF NOT EXISTS holds (key BLOB PRIMARY KEY, failures INTEGER, rejected REAL, resume REAL)"

    def __init__(self, path, key):
        self.path = Path(path)
        self.key = key
        self.started = time.time()

    def reserve_probe(self):
        if not self.path.exists():
            return 0
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(self.schema)
            row = db.execute("SELECT resume FROM holds WHERE key=?", (self.key,)).fetchone()
            if row:
                remaining = math.ceil(row[0] - time.time())
                if remaining > 0:
                    return remaining
                # One instance probes the service after the waiting period.
                db.execute("UPDATE holds SET resume=? WHERE key=?", (time.time() + 300, self.key))
        return 0

    def rejected(self):
        now = time.time()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(self.schema)
            old = db.execute("SELECT failures,rejected FROM holds WHERE key=?", (self.key,)).fetchone()
            count = min(3, old[0] + 1) if old and now - old[1] < 86400 else 1
            # Local backoff policy. IBKR does not publish a 1025 unlock time.
            wait = 900 * 2 ** (count - 1)
            db.execute("INSERT OR REPLACE INTO holds VALUES(?,?,?,?)", (self.key, count, now, now + wait))
        return wait

    def succeeded(self):
        if self.path.exists():
            with closing(sqlite3.connect(self.path)) as db, db:
                db.execute("DELETE FROM holds WHERE key=? AND rejected<=?", (self.key, self.started))


def service_error(code, stage, start, stop, retry_after=0):
    messages = {
        "1003": "该日期范围的报表暂不可用，请稍后重试，或在 IBKR 客户端门户核对可用报表日期。",
        "1012": "Token 已过期，请在门户生成新口令并更新 Flex 在线同步设置。",
        "1013": "当前 IP 受限，请核对 Flex 网络服务的 IP 设置。",
        "1014": "查询无效，请核对 Query ID 和所属账户。",
        "1015": "Token 无效，请更新 Flex 在线同步设置。",
        "1018": "IBKR 请求频率已达上限。",
        "1025": "IBKR 报告 Flex 请求失败尝试过多。等待期间可在客户端门户运行已有活动查询，下载 XML 后点「导入 IBKR Flex 报表…」。",
    }
    detail = messages.get(code, "请稍后重试，并按错误代码核对 IBKR 服务状态。")
    if retry_after and code in ("1003", "1018", "1025"):
        detail += f" 本机暂停请求 {retry_after} 秒，服务恢复时间由 IBKR 决定。"
    transient = code in ("1001", "1004", "1005", "1006", "1007", "1008", "1009", "1019", "1021")
    if transient:
        retry_after = max(60, retry_after)
    safe_code = code if re.fullmatch(r"[0-9]{4}", code) else "未知"
    return FlexServiceError(f"Flex {stage}失败（{start} 至 {stop}，代码 {safe_code}）。{detail}", safe_code, retry_after,
                            transient or code in ("1018", "1025"))


class FlexPacing:
    """Share each token's 1/s and 10/min allowance across requests and GUI jobs."""
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.lock = threading.Lock()
        self.calls = {}
        self.cooldowns = {}
        self.unavailable = {}

    def wait(self, key, cancel, stage, start, stop):
        if cancel.is_set():
            raise HistoryError("Flex 同步已取消。")
        with self.lock:
            now = self.clock()
            until, code = self.cooldowns.get(key, (0, ""))
            if until > now:
                raise service_error(code, stage, start, stop, math.ceil(until - now))
            calls = self.calls.setdefault(key, deque(maxlen=10))
            slot = max(now, calls[-1] + 1.1 if calls else now)
            if len(calls) == calls.maxlen:
                slot = max(slot, calls[0] + 60.1)
            calls.append(slot)
        if slot > now and cancel.wait(slot - now):
            raise HistoryError("Flex 同步已取消。")

    def cooldown(self, key, code, seconds=60):
        if code not in ("1003", "1018", "1025"):
            return 0
        # IBKR publishes the pacing window, but no unlock duration for 1025.
        with self.lock:
            now = self.clock()
            if code == "1003":
                failures = self.unavailable.setdefault(key, deque(maxlen=3))
                failures.append(now)
                if len(failures) < 3 or now - failures[0] > 60:
                    return 0
            self.cooldowns[key] = (now + seconds, code)
        return seconds


_PACING = FlexPacing()
_DAY = timedelta(days=1)


def _latest_report_day(now):
    # Activity statements are issued after the reporting day. The latest
    # weekend is commonly unpublished. Historical weekends inside a window
    # remain included for assets which can trade outside US equity hours.
    latest = now.astimezone(ZoneInfo("America/New_York")).date() - _DAY
    if latest.weekday() > 4:
        latest -= timedelta(days=latest.weekday() - 4)
    return latest


def report_date_bounds(period, display_timezone, report_timezone, now=None):
    now = now or datetime.now(timezone.utc)
    first, last = period_dates(period, now, display_timezone)
    display_zone, report_zone = ZoneInfo(display_timezone), ZoneInfo(report_timezone)
    try:
        start = datetime.combine(first, day_time.min, display_zone).astimezone(report_zone).date()
        end = datetime.combine(last, day_time.max, display_zone).astimezone(report_zone).date()
    except OverflowError:
        raise HistoryError("日期超出时区换算范围。") from None
    return start, end, min(end, _latest_report_day(now))


def report_request_dates(period, display_timezone, report_timezone, now=None):
    start, _, stop = report_date_bounds(period, display_timezone, report_timezone, now)
    return start, stop


def _uncovered(start, stop, covered):
    cursor = start
    for a, b in sorted(covered):
        if b < cursor:
            continue
        if a > stop:
            break
        if cursor < a:
            yield cursor, a - _DAY
        cursor = max(cursor, b + _DAY)
    if cursor <= stop:
        yield cursor, stop


def _newest_blocks(start, stop, days):
    while start <= stop:
        first = max(date(stop.year, 1, 1), stop - timedelta(days=min(days - 1, (stop - start).days)))
        yield first, stop
        if first == start:
            break
        stop = first - _DAY


def _report_statements(report, account):
    return [node for node in report.iter() if node.tag.rsplit("}", 1)[-1] == "FlexStatement"
            and (not account or node.get("accountId") == account)]


def _trade_section(statement):
    return any(section.tag.rsplit("}", 1)[-1] == "Trades" for section in statement)


def _reported_windows(statements):
    windows = set()
    for statement in statements:
        try:
            first = date.fromisoformat(statement.get("fromDate", ""))
            last = date.fromisoformat(statement.get("toDate", ""))
        except ValueError:
            continue
        if first <= last:
            windows.add((first, last))
    return sorted(windows)


def fetch_reports(token, query_id, period, display_timezone, report_timezone, cancel=None, get=http_get, now=None,
                  on_coverage=None, on_windows=None, account=None, pacing=None, known_windows=(), on_deferred=None,
                  hold_path=None):
    if not re.fullmatch(r"[0-9]+", str(token).strip()) or not re.fullmatch(r"[0-9]+", str(query_id).strip()):
        raise HistoryError("请填写有效的 Flex Token 和 Query ID（数字）。")
    cancel = cancel or threading.Event()
    now = now or datetime.now(timezone.utc)
    start, requested_end, end = report_date_bounds(period, display_timezone, report_timezone, now)
    if start > end:
        raise HistoryError(f"所选日期的 Flex 日报尚未出具，当前可请求至 {end}。可先查看本地或 TWS 成交。")
    token = str(token).strip()
    key = hashlib.sha256(token.encode("ascii")).digest()
    pacing = pacing or _PACING
    hold = FailedAttemptHold(hold_path, key) if hold_path is not None else None

    def request(endpoint, params, stage, first, last):
        pacing.wait(key, cancel, stage, first, last)
        try:
            raw = get(endpoint, params)
        except FlexServiceError as error:
            if error.code == "HTTP429":
                pacing.cooldown(key, "1018", error.retry_after)
            raise
        return raw, xml_root(raw)

    def fail(node, stage, first, last):
        code = node.findtext("ErrorCode", "").strip()
        if code == "1025":
            try:
                wait = hold.rejected() if hold else 900
            except (OSError, sqlite3.Error):
                # Enforce the wait in this process even if local storage is unavailable.
                wait = 900
            return service_error(code, stage, first, last, pacing.cooldown(key, code, wait))
        return service_error(code, stage, first, last, pacing.cooldown(key, code))

    def download(first, last):
        params = {"t": token, "q": str(query_id).strip(), "v": "3", "fd": first.strftime("%Y%m%d"), "td": last.strftime("%Y%m%d")}
        _, root = request("SendRequest", params, "报表请求", first, last)
        reference = root.findtext("ReferenceCode", "")
        if root.findtext("Status") != "Success" or not reference.isdigit():
            raise fail(root, "报表请求", first, last)
        # Ignore response <url>: only the fixed official endpoint receives token.
        deadline = time.monotonic() + 300
        for attempt in range(40):
            if cancel.wait(7):
                raise HistoryError("Flex 同步已取消。")
            raw, report = request("GetStatement", {"t": token, "q": reference, "v": "3"}, "报表下载", first, last)
            if report.tag.rsplit("}", 1)[-1] in ("FlexQueryResponse", "FlexStatements", "FlexStatement"):
                return raw, report
            code = report.findtext("ErrorCode", "")
            if code == "1003" and attempt == 0:
                continue
            if code not in ("1001", "1004", "1005", "1006", "1007", "1008", "1019", "1021"):
                raise fail(report, "报表下载", first, last)
            if time.monotonic() >= deadline:
                break
        raise FlexServiceError("Flex 报表仍在生成，请稍后重新同步；原有记录保留。", "1019", 60, True)

    reports = []
    # An Asian YTD view can include the last report day of the previous NY
    # year. Fetch newer years first, preserving gaps in cached date coverage.
    pending = deque(block for a, b in reversed(list(_uncovered(start, end, known_windows)))
                    for block in _newest_blocks(a, b, 365))
    if pending and hold:
        try:
            remaining = hold.reserve_probe()
        except (OSError, sqlite3.Error):
            raise FlexServiceError("暂时无法读取本机 Flex 重试时间。", "LOCAL", 60, True) from None
        if remaining:
            raise FlexServiceError("此前 IBKR 返回 1025，本机正在等待下次重试。当前显示本地成交。", "1025", remaining, True)
    missing = []
    gap_probes = set()
    consecutive_failures = 0
    deferred = None
    while pending:
        first, stop = pending.popleft()
        try:
            raw, report = download(first, stop)
        except FlexServiceError as error:
            if error.auto_retry and reports and on_deferred and on_coverage:
                deferred = error
                missing.extend([(first, stop), *pending])
                break
            if error.code != "1003":
                raise
            consecutive_failures += 1
            if (stop - first).days >= 30 and consecutive_failures == 1 and not error.retry_after and (first, stop) not in gap_probes:
                pending.extendleft(reversed(list(_newest_blocks(first, stop, 30))))
                continue
            if stop == _latest_report_day(now) and not error.retry_after:
                # Latest eligible statement can still be in preparation. Keep
                # this day pending and recover earlier days before the retry.
                latest_error = FlexServiceError(str(error), "1003", 60, True)
                if not on_deferred or not on_coverage or (first == stop and not pending and not reports):
                    raise latest_error from None
                deferred = latest_error
                missing.append((stop, stop))
                if first < stop:
                    pending.appendleft((first, stop - _DAY))
                continue
            missing.append((first, stop))
            if consecutive_failures >= 3 or error.retry_after or not pending:
                if not reports or not on_coverage:
                    if deferred:
                        raise deferred from None
                    raise
                missing.extend(pending)
                break
            if not on_coverage:
                raise
            continue
        consecutive_failures = 0
        statements = _report_statements(report, account)
        trade_statements = [s for s in statements if _trade_section(s)]
        if account and not statements:
            raise HistoryError("Flex 报表不含当前账户，未导入。")
        if account and not trade_statements:
            detail = f"{first} 至 {stop} 的 Flex 报表未返回 Trades 区段，无法确认成交覆盖。"
            if not on_coverage or (not pending and not reports):
                raise HistoryError(detail + "请核对活动查询选择了 Trades / Executions。")
            on_coverage(detail)
            missing.append((first, stop))
            continue
        returned = _reported_windows(trade_statements)
        windows = [(max(a, first), min(b, stop)) for a, b in returned if b >= first and a <= stop]
        if returned and not windows:
            detail = f"IBKR 返回的报表日期在请求范围 {first} 至 {stop} 之外。"
            if not on_coverage or (not pending and not reports):
                raise HistoryError(detail)
            on_coverage(detail)
            missing.append((first, stop))
            continue
        reports.append(raw)
        if on_windows:
            for a, b in windows:
                on_windows(a, b)
        if on_coverage and windows:
            for a, b in _uncovered(first, stop, windows):
                first_weekday = a if a.weekday() < 5 else a + timedelta(days=7 - a.weekday())
                if first_weekday <= b and (first, stop) not in gap_probes:
                    # A dated partial report made progress. Probe the uncovered
                    # dates once; a server returning its template period cannot
                    # cause an infinite chain of narrowing requests.
                    gap_probes.add((a, b))
                    pending.appendleft((a, b))
                else:
                    on_coverage(f"报表未确认覆盖 {a} 至 {b}。")
        elif on_coverage and account:
            on_coverage(f"{first} 至 {stop} 的报表未标明可核实的起止日期。")
    if on_coverage and missing:
        gaps = []
        for a, b in sorted(missing):
            if gaps and gaps[-1][1] + _DAY == a:
                gaps[-1] = (gaps[-1][0], b)
            else:
                gaps.append((a, b))
        for a, b in gaps:
            on_coverage(f"历史报表部分可用。{a} 至 {b} 未取得。")
    if on_coverage and end < requested_end:
        on_coverage(f"Flex 日报请求截至 {end}，更新的成交可由 TWS 补充。")
    if on_deferred and deferred:
        on_deferred(deferred)
    if hold and reports and not deferred:
        try:
            hold.succeeded()
        except (OSError, sqlite3.Error):
            if on_coverage:
                on_coverage("报表已收到，暂时无法更新本机 Flex 重试时间。")
    return reports


def _protect(raw, decrypt=False):
    if os.name != "nt":
        raise HistoryError("凭据本机保存仅支持 Windows 加密。")
    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_byte))]
    buffer = ctypes.create_string_buffer(raw)
    source = Blob(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
    target = Blob()
    api = ctypes.windll.crypt32.CryptUnprotectData if decrypt else ctypes.windll.crypt32.CryptProtectData
    if not api(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise HistoryError("无法用当前 Windows 用户加密/读取 Flex 凭据。")
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        ctypes.windll.kernel32.LocalFree(ctypes.cast(target.data, ctypes.c_void_p))


def save_credentials(path, account, mode, token, query_id, timezone):
    path = Path(path)
    values = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    values[f"{mode}:{account}"] = dict(token=base64.b64encode(_protect(token.encode())).decode(), query_id=query_id, timezone=timezone)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(values), encoding="utf-8")
    temporary.replace(path)


def load_credentials(path, account, mode):
    path = Path(path)
    if not path.exists():
        return {}
    values = json.loads(path.read_text(encoding="utf-8")).get(f"{mode}:{account}")
    return {**values, "token": _protect(base64.b64decode(values["token"]), decrypt=True).decode()} if values else {}
