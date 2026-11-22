import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from app import App
from usage_logs import BEIJING, ReportStore, UsageLog, message_kind, record, sanitize
from tests.test_app_state import controller


class UsageLogTests(unittest.TestCase):
    def test_credentials_masked_in_nested_data_and_messages(self):
        value = dict(token='preview-secret', other=[dict(password='pw-secret')],
                     message='"authorization": "Bearer oauth-secret" token=api-secret https://example.test?t=flex-secret&q=1 U12345678 DU98765432')
        before = copy.deepcopy(value)
        encoded = json.dumps(sanitize(value), ensure_ascii=False)
        for secret in ('preview-secret', 'pw-secret', 'oauth-secret', 'api-secret', 'flex-secret', 'U12345678', 'DU98765432'):
            self.assertNotIn(secret, encoded)
        self.assertIn('U***5678', encoded)
        self.assertEqual(value, before)

    def test_utc_timestamp_uses_beijing_day_and_iso_week_year(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ReportStore(directory)
            try:
                item = record('request_dispatched', timestamp=datetime(2026, 12, 31, 17, tzinfo=timezone.utc), action='buy', mode='PAPER', symbol='SPY')
                self.assertEqual(item['day'], '2027-01-01')
                store.add(item)
                store.refresh(generated=datetime(2027, 1, 1, tzinfo=BEIJING))
                weekly = Path(directory, 'weekly', '2026-W53.txt').read_text(encoding='utf-8')
                monthly = Path(directory, 'monthly', '2027-01.txt').read_text(encoding='utf-8')
                self.assertIn('2026-12-28 至 2027-01-03', weekly)
                self.assertIn('2027-01-01 至 2027-01-31', monthly)
                self.assertIn('开仓请求：1', monthly)
                self.assertNotIn('已成交：1', monthly)
            finally:
                store.close()

    def test_legacy_import_resumes_and_waits_for_complete_lines(self):
        with tempfile.TemporaryDirectory() as directory:
            old = Path(directory, 'session_20261119_120000.jsonl')
            first = json.dumps(dict(time='2026-11-19 12:00:00', message='连接 U12345678')).encode() + b'\n'
            second = json.dumps(dict(time='2026-11-19 13:00:00', message='Error test')).encode()
            old.write_bytes(first + second[:20])
            store = ReportStore(directory)
            try:
                store.import_legacy()
                self.assertEqual(store.db.execute('SELECT count(*) FROM events').fetchone()[0], 1)
                old.write_bytes(first + second + b'\n')
                before = old.read_bytes()
                store.import_legacy()
                store.import_legacy()
                self.assertEqual(store.db.execute('SELECT count(*) FROM events').fetchone()[0], 2)
                self.assertEqual(old.read_bytes(), before)
                self.assertNotIn('U12345678', str(list(store.rows('2026-11-19', '2026-11-19'))))
            finally:
                store.close()

    def test_period_reports_do_not_mix_previous_month(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ReportStore(directory)
            try:
                for month, amount in ((10, 3), (11, 7)):
                    store.add(record('request_dispatched', timestamp=datetime(2026, month, 20, tzinfo=BEIJING), action='chain', background=True, count=amount))
                store.refresh(generated=datetime(2026, 11, 20, tzinfo=BEIJING))
                october = Path(directory, 'monthly', '2026-10.txt').read_text(encoding='utf-8')
                november = Path(directory, 'monthly', '2026-11.txt').read_text(encoding='utf-8')
                self.assertIn('期权链查询：3', october)
                self.assertIn('期权链查询：7', november)
                self.assertNotIn('期权链查询：10', november)
            finally:
                store.close()

    def test_failed_text_export_preserves_durable_events_for_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ReportStore(directory)
            try:
                store.add(record('request_error', 'failure', timestamp=datetime(2026, 11, 19, tzinfo=BEIJING), level='ERROR'))
                with patch.object(store, '_write', side_effect=OSError('read-only')):
                    with self.assertRaises(OSError):
                        store.refresh()
                self.assertIn('2026-11-19', store.dirty)
                self.assertEqual(store.db.execute('SELECT count(*) FROM events').fetchone()[0], 1)
                store.refresh()
                self.assertFalse(store.dirty)
                self.assertTrue(Path(directory, 'daily', '2026-11', '2026-11-19.jsonl').exists())
            finally:
                store.close()

    def test_thread_flushes_background_counts_and_keeps_manual_events(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = UsageLog(directory, 'test', interval=60)
            for _ in range(5):
                journal.record('request_dispatched', action='chain', background=True, mode='PAPER', symbol='SPY')
            journal.record('request_dispatched', action='buy', background=False, mode='PAPER', symbol='SPY')
            self.assertTrue(journal.close())
            self.assertEqual(journal.last_error, '')
            store = ReportStore(directory)
            try:
                rows = list(store.rows('2000-01-01', '2100-01-01'))
                requests = [row for row in rows if row['event'] == 'request_dispatched']
                self.assertEqual(len(requests), 2)
                self.assertEqual(sum(row['count'] for row in requests), 6)
                self.assertEqual(sum(row['event'] == 'app_start' for row in rows), 1)
                self.assertEqual(sum(row['event'] == 'app_stop' for row in rows), 1)
                self.assertFalse(journal.record('after_close'))
            finally:
                store.close()

    def test_reopen_recovers_committed_events_without_export(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ReportStore(directory)
            store.add(record('diagnostic', 'persisted', timestamp=datetime(2026, 10, 20, tzinfo=BEIJING)))
            store.commit()
            store.close()
            recovered = ReportStore(directory)
            try:
                self.assertFalse(recovered.dirty)
                recovered.refresh(generated=datetime(2026, 11, 20, tzinfo=BEIJING))
                self.assertIn('persisted', Path(directory, 'daily', '2026-10', '2026-10-20.jsonl').read_text(encoding='utf-8'))
                saved = Path(directory, 'daily', '2026-10', '2026-10-20.jsonl').stat().st_mtime_ns
                recovered.refresh(generated=datetime(2026, 11, 20, tzinfo=BEIJING))
                self.assertEqual(saved, Path(directory, 'daily', '2026-10', '2026-10-20.jsonl').stat().st_mtime_ns)
            finally:
                recovered.close()

    def test_store_failure_reported_without_throwing_into_application(self):
        with tempfile.TemporaryDirectory() as directory:
            bad = Path(directory, 'not-a-directory')
            bad.write_text('x')
            journal = UsageLog(bad, 'test')
            journal.thread.join(timeout=2)
            self.assertIn('自动日志停止', journal.last_error)
            journal.record('request_dispatched', action='buy')
            journal.close()

    def test_broker_connection_notices_are_not_counted_as_errors(self):
        self.assertEqual(message_kind('IBKR [2104] farm is OK'), ('broker_message', 'INFO'))
        self.assertEqual(message_kind('IBKR [354] not subscribed'), ('broker_message', 'WARNING'))

    def test_request_logging_does_not_copy_tokens_or_change_order_arguments(self):
        ui = controller()
        ui.busy = False
        ui.usage_log = Mock()
        ui.submit('buy', token='PRIVATE_PREVIEW_TOKEN')
        calls = str(ui.usage_log.record.call_args_list)
        self.assertNotIn('PRIVATE_PREVIEW_TOKEN', calls)
        self.assertIn('request_dispatched', calls)
        self.assertEqual(ui.inbox.get()[1], {'token': 'PRIVATE_PREVIEW_TOKEN'})

    def test_ui_log_is_masked_and_memory_is_bounded(self):
        ui = controller()
        ui.usage_log, ui.log_box = Mock(), Mock()
        ui.log_box.index.return_value = '1.0'
        ui.logs = ['old'] * 1000
        App.log(ui, 'token=secret U12345678')
        self.assertEqual(len(ui.logs), 1000)
        self.assertNotIn('secret', ui.logs[-1])
        self.assertNotIn('U12345678', ui.logs[-1])


if __name__ == '__main__':
    unittest.main()
