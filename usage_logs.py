"""Local diagnostic journal with daily exports and continuously updated reports.

SQLite is the durable source; human-readable files are atomic, replaceable
exports. All file work runs off the UI/broker threads. No network or trading.
"""
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone, date
import hashlib
import json
import logging
import os
from pathlib import Path
import queue
import re
import sqlite3
import threading
import time
import uuid

BEIJING = timezone(timedelta(hours=8))
SECRET_KEYS = re.compile(r'token|password|passwd|secret|authorization|live_ack|api.?key', re.I)
SECRET_TEXT = re.compile(r'''(?i)((?:[?&]t|token|password|passwd|secret|authorization|live_ack|api_key)["']?\s*[:=]\s*["']?)([^\s,"'};&]+)''')
ACCOUNT = re.compile(r'\b(D?U)\d{5,}\b', re.I)
LABELS = {
    'connect': '连接账户', 'disconnect': '断开连接', 'buy': '开仓请求',
    'preview_add': '持仓加仓预览', 'add_confirm': '确认加仓', 'raise_cost_stop': '上调原成本止损单',
    'set_breakeven_stop': '一键保本损',
    'close': '平仓/条件单请求', 'close_confirm': '确认平仓', 'close_all': '平全部',
    'cancel_own': '撤本工具挂单', 'cancel_order': '撤所选订单', 'modify_order': '改单',
    'auto_mid_start': '开启自动中间价', 'auto_mid_stop': '停止自动中间价',
    'preview_pair': '双向开仓预览', 'preview_close': '平仓预览', 'preview_modify': '改单预览',
    'snapshot': '持仓/订单快照', 'chain': '期权链查询', 'expirations': '到期日查询',
    'trading_classes': '交易类别查询', 'refresh_funds': '刷新资金',
    'refresh_positions': '刷新持仓回报', 'execution_history': '刷新成交历史',
    'import_execution_history': '导入成交报表', 'import_flex_reports': '导入 Flex 报表',
}


def sanitize(value):
    if isinstance(value, dict):
        return {str(k): '[已隐藏]' if SECRET_KEYS.search(str(k)) else sanitize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize(item) for item in value[:100]]
    if isinstance(value, str):
        value = re.sub(r'''(?i)((?:token|password|passwd|secret|authorization|live_ack|api_key)["']?\s*[:=]\s*)(["'])(.*?)\2''',
                       lambda m: m[1] + m[2] + '[已隐藏]' + m[2], value)
        value = re.sub(r'(?i)\bBearer\s+[^\s,"\'}]+', 'Bearer [已隐藏]', value)
        value = SECRET_TEXT.sub(r'\1[已隐藏]', value)
        return ACCOUNT.sub(lambda m: m[1].upper() + '***' + m[0][-4:], value)[:16000]
    if value is None or isinstance(value, (int, float, bool)):
        return value
    return sanitize(str(value))


def message_kind(message):
    text = str(message)
    if text.startswith('成交 '):
        return 'execution_report', 'INFO'
    if text.startswith('订单 '):
        return 'order_update', 'INFO'
    if 'IBKR [' in text:
        match = re.search(r'IBKR \[(\d+)\]', text)
        code = int(match[1]) if match else 0
        return 'broker_message', 'INFO' if code in (2104, 2106, 2107, 2108, 2158) else 'WARNING'
    if any(word in text for word in ('失败', '异常', '错误', 'Traceback')):
        return 'diagnostic', 'ERROR'
    if any(word in text for word in ('暂停', '等待确认', '未确认', '锁定', '无权限')):
        return 'diagnostic', 'WARNING'
    return 'diagnostic', 'INFO'


def now():
    return datetime.now(BEIJING)


def record(event, message='', *, timestamp=None, session='', version='', **fields):
    stamp = timestamp or now()
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=BEIJING)
    stamp = stamp.astimezone(BEIJING)
    return sanitize(dict(id=uuid.uuid4().hex, time=stamp.isoformat(timespec='seconds'),
                         day=stamp.date().isoformat(), session=session, version=version,
                         event=event, message=message, level=fields.pop('level', 'INFO'),
                         count=fields.pop('count', 1), **fields))


class ReportStore:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.root / 'usage.sqlite3', timeout=5)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY, day TEXT NOT NULL, time TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS by_day ON events(day, time);
            CREATE TABLE IF NOT EXISTS imported_files(path TEXT PRIMARY KEY, offset INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS day_exports(day TEXT PRIMARY KEY, event_count INTEGER NOT NULL);
        ''')
        self.dirty = set()

    def add(self, item):
        item = sanitize(item)
        result = self.db.execute('INSERT OR IGNORE INTO events VALUES(?,?,?,?)',
                                (item['id'], item['day'], item['time'], json.dumps(item, ensure_ascii=False)))
        if result.rowcount:
            self.dirty.add(item['day'])

    def commit(self):
        self.db.commit()

    def import_legacy(self):
        """Resume at complete-line offsets; don't alter or delete old session files."""
        for path in sorted(self.root.glob('session_*.jsonl')):
            saved = self.db.execute('SELECT offset FROM imported_files WHERE path=?', (path.name,)).fetchone()
            offset = saved[0] if saved and path.stat().st_size >= saved[0] else 0
            with path.open('rb') as handle:
                handle.seek(offset)
                while True:
                    start = handle.tell()
                    raw = handle.readline()
                    if not raw or not raw.endswith(b'\n'):
                        break
                    offset = handle.tell()
                    try:
                        old = json.loads(raw.decode('utf-8-sig'))
                        stamp = datetime.fromisoformat(old['time'])
                        event, level = message_kind(old.get('message', ''))
                        item = record(event, old.get('message', ''), timestamp=stamp, session=path.stem, version='旧版', level=level, source='旧版日志')
                        item['id'] = 'legacy-' + hashlib.sha256(path.name.encode() + str(start).encode() + raw).hexdigest()
                    except (ValueError, KeyError, TypeError, UnicodeError):
                        # Malformed old entries aren't made up as valid events.
                        continue
                    self.add(item)
            self.db.execute('INSERT OR REPLACE INTO imported_files VALUES(?,?)', (path.name, offset))
            self.commit()

    def _write(self, relative, content):
        target = self.root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + '.' + uuid.uuid4().hex + '.tmp')
        try:
            temporary.write_text(content, encoding='utf-8')
            os.replace(temporary, target)
        finally:
            if temporary.exists():
                temporary.unlink()

    def rows(self, start, end):
        return (json.loads(row[0]) for row in self.db.execute('SELECT payload FROM events WHERE day BETWEEN ? AND ? ORDER BY time,id', (str(start), str(end))))

    def report(self, start, end, title, generated):
        events, levels, manual, background, modes, symbols, issues = (Counter() for _ in range(7))
        days = defaultdict(Counter)
        sessions = set()
        elapsed, old_count = 0, 0
        for item in self.rows(start, end):
            count = max(1, int(item.get('count', 1)))
            kind, level = item.get('event', ''), item.get('level', 'INFO')
            events[kind] += count
            levels[level] += count
            days[item['day']]['records'] += 1
            sessions.add(item.get('session', ''))
            if item.get('source') == '旧版日志':
                old_count += count
            if kind == 'heartbeat':
                elapsed += max(0, item.get('seconds', 0))
            if kind == 'request_dispatched':
                action = item.get('action', '未知')
                (background if item.get('background') else manual)[action] += count
                days[item['day']]['background' if item.get('background') else 'manual'] += count
                modes[item.get('mode') or '未确定'] += count
                symbols[item.get('symbol') or '未选择'] += count
            if level in ('ERROR', 'WARNING'):
                # Aggregate request/order number variations without losing the raw detail.
                message = re.sub(r'(请求|订单)\s*\d+', r'\1 #', item.get('message', ''))
                issues[(level, message[:240])] += count
                days[item['day']][level] += count
        def section(counter, labels=None):
            return '\n'.join(f'  {(labels or {}).get(key, key)}：{value}' for key, value in counter.most_common()) or '  暂无记录'
        daily = '\n'.join(f"  {day} | 日志 {c['records']} 条 | 手动请求 {c['manual']} | 后台请求 {c['background']} | 错误 {c['ERROR']} | 提醒 {c['WARNING']}" for day, c in sorted(days.items())) or '  暂无记录'
        problems = '\n'.join(f'  [{level}] {count} 次：{text}' for (level, text), count in issues.most_common(15)) or '  未记录错误/提醒'
        return (f'{title}\n日期范围：{start} 至 {end}（北京时间）\n最后更新：{generated:%Y-%m-%d %H:%M:%S}\n\n'
                f"启动记录：{events['app_start']} 次；正常关闭记录：{events['app_stop']} 次；日志涉及会话：{len(sessions - {''})} 个\n"
                f'记录的运行时间：{elapsed / 3600:.2f} 小时（心跳估计，含空闲；不等于实际操作时长）\n'
                f"错误事件：{levels['ERROR']}；提醒事件：{levels['WARNING']}；旧版日志：{old_count} 条\n\n"
                f'手动操作请求（已交后台处理，不代表券商接受或成交）\n{section(manual, LABELS)}\n\n'
                f'后台自动查询请求\n{section(background, LABELS)}\n\n'
                f'请求使用的模式\n{section(modes)}\n\n请求涉及的标的\n{section(symbols)}\n\n'
                f'每日概览\n{daily}\n\n常见错误 / 提醒\n{problems}\n\n事件分类\n{section(events)}\n\n'
                '说明：这是软件使用与排错记录，不是成交对账或盈亏报表。旧版日志缺少手动/后台分类与心跳，相关统计从升级后开始。\n'
                '日志按北京时间记日，周一至周日为一周；当期每分钟更新，正常关闭时更新。程序未运行期间不采集，下次启动补齐已有记录的汇总。\n')

    def refresh(self, *, generated=None, all_periods=False):
        generated = generated or now()
        self.commit()
        dirty = set(self.dirty)
        counts = dict(self.db.execute('SELECT day,count(*) FROM events GROUP BY day'))
        exported = dict(self.db.execute('SELECT day,event_count FROM day_exports'))
        # Recover committed events after an interrupted export, without rewriting
        # every old day/month each time a long-lived installation starts.
        dirty.update(day for day, count in counts.items() if exported.get(day) != count)
        if all_periods:
            dirty.update(counts)
        periods = dirty | {generated.date().isoformat()}
        weeks, months = {}, {}
        for value in periods:
            day = date.fromisoformat(value)
            week_start = day - timedelta(days=day.weekday())
            iso = day.isocalendar()
            weeks[f'{iso.year}-W{iso.week:02}'] = (week_start, week_start + timedelta(days=6))
            month_start = day.replace(day=1)
            next_month = (month_start.replace(day=28) + timedelta(days=4)).replace(day=1)
            months[day.strftime('%Y-%m')] = (month_start, next_month - timedelta(days=1))
        for day in dirty:
            text = ''.join(json.dumps(item, ensure_ascii=False) + '\n' for item in self.rows(day, day))
            self._write(f'daily/{day[:7]}/{day}.jsonl', text)
        for key, (start, end) in weeks.items():
            self._write(f'weekly/{key}.txt', self.report(start, end, f'使用周报 {key}', generated))
        for key, (start, end) in months.items():
            self._write(f'monthly/{key}.txt', self.report(start, end, f'使用月报 {key}', generated))
        iso = generated.isocalendar()
        self._write('最新汇总.txt', f'更新时间：{generated:%Y-%m-%d %H:%M:%S} 北京时间\n\n本周：weekly/{iso.year}-W{iso.week:02}.txt\n本月：monthly/{generated:%Y-%m}.txt\n每日明细：daily/YYYY-MM/YYYY-MM-DD.jsonl\n\nusage.sqlite3 是日志原始记录库；文本和 JSONL 可重新生成。旧 session_*.jsonl 保留。\n历史日志按日期归档保留；不自动删除。\n')
        self.db.executemany('INSERT OR REPLACE INTO day_exports VALUES(?,?)', ((day, counts.get(day, 0)) for day in dirty))
        self.commit()
        self.dirty.difference_update(dirty)

    def close(self):
        self.db.close()


class UsageLog:
    def __init__(self, root, version='', *, interval=60):
        self.root, self.version, self.interval = Path(root), version, interval
        self.session = now().strftime('%Y%m%d_%H%M%S') + '_' + uuid.uuid4().hex[:8]
        self.queue = queue.Queue(maxsize=20000)
        self.last_error, self.last_refresh = '', ''
        self.closed = False
        self.thread = threading.Thread(target=self._run, name='usage-log', daemon=True)
        self.thread.start()
        self.record('app_start', '程序启动')

    def record(self, event, message='', **fields):
        if self.closed:
            return False
        try:
            self.queue.put_nowait(record(event, message, session=self.session, version=self.version, **fields))
            return True
        except queue.Full:
            self.last_error = '日志队列已满，部分新事件未写入；请查看日志目录磁盘状态。'
            return False

    def request_refresh(self):
        if not self.closed:
            try:
                self.queue.put_nowait('refresh')
            except queue.Full:
                self.last_error = '日志队列已满，汇总将在下一轮更新。'

    def close(self, timeout=3):
        if not self.closed:
            self.record('app_stop', '程序正常关闭')
            self.closed = True
            try:
                self.queue.put(None, timeout=timeout)
            except queue.Full:
                self.last_error = '日志关闭时队列未清空。'
        self.thread.join(timeout=timeout)
        return not self.thread.is_alive()

    def _run(self):
        store, pending = None, {}
        last = time.monotonic()
        try:
            store = ReportStore(self.root)
            store.import_legacy()
            store.refresh()
            self.last_refresh = now().strftime('%Y-%m-%d %H:%M:%S')
            while True:
                try:
                    item = self.queue.get(timeout=max(.05, self.interval - (time.monotonic() - last)))
                except queue.Empty:
                    item = 'refresh'
                if isinstance(item, dict):
                    if item['event'] == 'request_dispatched' and item.get('background'):
                        key = (item['day'], item.get('action'), item.get('mode'), item.get('symbol'))
                        if key in pending:
                            pending[key]['count'] += 1
                            pending[key]['last_time'] = item['time']
                        else:
                            pending[key] = item
                    else:
                        store.add(item)
                        store.commit()
                due = time.monotonic() - last >= self.interval
                if item is None or item == 'refresh' or due:
                    for batch in pending.values():
                        store.add(batch)
                    pending.clear()
                    seconds = time.monotonic() - last
                    store.add(record('heartbeat', '程序运行心跳', session=self.session, version=self.version, seconds=min(seconds, self.interval * 2)))
                    last = time.monotonic()
                    try:
                        store.refresh()
                        self.last_refresh = now().strftime('%Y-%m-%d %H:%M:%S')
                        self.last_error = ''
                    except (OSError, sqlite3.Error) as exc:
                        store.commit()
                        self.last_error = sanitize(f'日志导出失败：{type(exc).__name__}: {exc}')
                    if item is None:
                        break
        except Exception as exc:
            self.last_error = sanitize(f'自动日志停止：{type(exc).__name__}: {exc}')
        finally:
            if store:
                store.close()


class UsageHandler(logging.Handler):
    def __init__(self, journal):
        super().__init__(logging.WARNING)
        self.journal = journal

    def emit(self, log_record):
        try:
            self.journal.record('python_log', self.format(log_record), level=log_record.levelname, logger=log_record.name)
        except Exception:
            pass  # Diagnostic failures cannot trigger recursive logging.
