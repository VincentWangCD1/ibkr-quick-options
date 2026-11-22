"""Controller coverage for date selection and read-only automatic Flex sync."""
from datetime import date, datetime, timezone
import queue
import unittest
from unittest.mock import Mock, patch

from flex_sync import report_date_bounds
from history_ui import CUSTOM_PERIOD
from test_app_state import Tree, Variable, controller


class Timers:
    def __init__(self):
        self.calls = {}
        self.seq = 0

    def after(self, delay, callback):
        self.seq += 1
        self.calls[self.seq] = (delay, callback)
        return self.seq

    def after_idle(self, callback):
        return self.after(0, callback)

    def after_cancel(self, handle):
        self.calls.pop(handle, None)

    def fire(self):
        self.calls.pop(next(iter(self.calls)))[1]()


class ImmediateThread:
    def __init__(self, target, daemon):
        self.start = target


class HistoryAutoRangeTests(unittest.TestCase):
    def setUp(self):
        self.ui = ui = controller()
        ui.session_mode, ui.snapshot_data = "LIVE", {"account": "U123"}
        ui.flex_config_scope, ui.flex_config = ("LIVE", "U123"), dict(token="101", query_id="20", timezone="America/New_York")
        ui.root, ui.outbox = Timers(), queue.Queue()
        ui.history_period, ui.history_timezone = Variable("最近 30 日"), Variable("北京时间")
        ui.history_summary, ui.history_coverage, ui.history_tree = Variable(""), Variable(""), Tree()
        ui.history_needs_sync = False
        ui.submit = Mock(return_value=True)
        self.threads = patch("history_ui.threading.Thread", ImmediateThread)
        self.threads.start()
        self.addCleanup(self.threads.stop)
        bounds = patch("flex_sync.report_date_bounds", side_effect=lambda *args: report_date_bounds(
            *args, now=datetime(2026, 11, 21, 19, tzinfo=timezone.utc)))
        bounds.start()
        self.addCleanup(bounds.stop)

    def change(self, name):
        self.ui.history_period.set(name)
        self.ui.choose_history_period()

    def deliver(self):
        while not self.ui.outbox.empty():
            self.ui.handle_result(*self.ui.outbox.get_nowait())

    def report(self, token, query_id, period, display_zone, report_zone, cancel, **callbacks):
        self.assertEqual((display_zone, report_zone), ("Asia/Shanghai", "America/New_York"))
        callbacks["on_windows"](date(2026, 1, 1), date(2026, 11, 20))
        return ["<report/>"]

    def test_debounce_latest_selection_then_fetch_and_import_once(self):
        ui = self.ui
        with patch("flex_sync.fetch_reports", side_effect=self.report) as fetch:
            self.change("最近 60 日")
            self.change("最近 90 日")
            self.assertEqual(len(ui.root.calls), 1)
            ui.root.fire()
            self.assertEqual(fetch.call_args.args[2], "90d")
            self.assertEqual(fetch.call_count, 1)
            self.assertTrue(any(call.kwargs.get("refresh") for call in ui.submit.call_args_list))
            self.deliver()
            ui.submit.assert_called_with("import_flex_reports", reports=["<report/>"], tz_name="America/New_York",
                                         query_id="20", report_windows=[(date(2026, 1, 1), date(2026, 11, 20))])
            self.assertEqual(ui.flex_windows, [])
            ui.handle_result("import_flex_reports", dict(imported=1, duplicates=0), None, ui.context())
            self.assertEqual(ui.flex_windows, [(date(2026, 1, 1), date(2026, 11, 20))])

    def test_custom_bounds_pass_to_broker_and_fetch_separately_from_display_zone(self):
        ui = self.ui
        ui.history_period.set(CUSTOM_PERIOD)
        ui.history_custom_dates = ("2025-12-28", "2026-01-08")
        with patch("flex_sync.fetch_reports", side_effect=self.report) as fetch:
            ui.history_filter_changed()
            self.assertEqual(ui.submit.call_args.kwargs["period"], ui.history_custom_dates)
            ui.root.fire()
            self.assertEqual(fetch.call_args.args[2], ui.history_custom_dates)

    def test_finishing_prior_range_saves_coverage_and_runs_latest(self):
        ui = self.ui
        with patch("flex_sync.fetch_reports", side_effect=self.report) as fetch:
            self.change("最近 90 日")
            ui.root.fire()
            self.change("最近 7 日")
            ui.root.fire()
            self.assertEqual(fetch.call_count, 1)
            self.deliver()
            ui.handle_result("import_flex_reports", dict(imported=1, duplicates=0), None, ui.context())
            self.assertNotIn("新增 1", ui.flex_feedback)
            with patch("flex_sync.report_date_bounds", return_value=(date(2026,11,14),date(2026,11,21),date(2026,11,20))):
                ui.root.fire()
            self.assertEqual(fetch.call_count, 1)
            self.assertIn("已同步", ui.flex_feedback)
            self.assertIn("11-21", ui.flex_feedback)
            ui.sync_flex()
            self.assertEqual(fetch.call_count, 2)

    def test_cooldown_retries_latest_transient_failure_and_stop_cancels_timer(self):
        ui = self.ui
        def fail(*args, **kwargs):
            error = ValueError("1025")
            error.code = "1025"
            error.retry_after = 65
            raise error
        with patch("flex_sync.fetch_reports", side_effect=fail) as fetch:
            self.change("年初至今")
            ui.root.fire()
            self.deliver()
            self.assertEqual(fetch.call_count, 1)
            self.assertGreaterEqual(next(iter(ui.root.calls.values()))[0], 65000)
            self.change("最近 60 日")
            ui.root.fire()
            self.assertEqual(fetch.call_count, 1)
            self.assertEqual(len(ui.root.calls), 1)
            ui.cancel_flex()
            self.assertFalse(ui.root.calls)

    def test_deferred_chunks_commit_before_retrying_uncovered_dates(self):
        ui = self.ui
        def partial(*args, on_windows, on_deferred, **kwargs):
            error = ValueError("1025")
            error.code = "1025"
            error.retry_after = 70
            on_windows(date(2026, 9, 1), date(2026, 9, 10))
            on_deferred(error)
            return ["chunk"]
        with patch("flex_sync.fetch_reports", side_effect=partial) as fetch:
            self.change("年初至今")
            ui.root.fire()
            self.deliver()
            self.assertEqual(ui.flex_windows, [])
            ui.handle_result("import_flex_reports", dict(imported=2, duplicates=0), None, ui.context())
            self.assertIn("自动重试", ui.flex_feedback)
            self.assertIn("新增 2", ui.flex_feedback)
            ui.flex_retry_at = 0
            ui.root.fire()
            self.assertEqual(fetch.call_args.kwargs["known_windows"], ((date(2026,9,1),date(2026,9,10)),))

    def test_recent_unpublished_or_transient_error_retries_only_affected_range(self):
        ui = self.ui
        recent = ValueError("today's statement is being prepared")
        recent.code, recent.retry_after, recent.auto_retry = "1003", 90, True
        with patch("flex_sync.fetch_reports", side_effect=[recent, ["another range"]]) as fetch:
            self.change("最近 90 日")
            ui.root.fire()
            self.deliver()
            self.assertGreaterEqual(next(iter(ui.root.calls.values()))[0], 90000)
            self.change("最近 7 日")
            ui.root.fire()
            self.assertEqual(fetch.call_count, 2)

    def test_historical_unavailable_error_does_not_loop(self):
        ui = self.ui
        historical = ValueError("unavailable historical statement")
        historical.code, historical.retry_after, historical.auto_retry = "1003", 90, False
        with patch("flex_sync.fetch_reports", side_effect=historical):
            self.change("最近 90 日")
            ui.root.fire()
            self.deliver()
            self.assertFalse(ui.root.calls)

    def test_autosync_without_credentials_leaves_local_history_usable(self):
        ui = self.ui
        ui.flex_config = {}
        ui.configure_flex = Mock()
        with patch("flex_sync.load_credentials", return_value={}), patch("flex_sync.fetch_reports") as fetch:
            self.change("年初至今")
            ui.root.fire()
            fetch.assert_not_called()
        ui.configure_flex.assert_not_called()
        self.assertIn("本地记录", ui.flex_feedback)

    def test_same_day_restart_uses_committed_windows_before_contacting_broker(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from trade_history import TradeHistoryStore
        from test_v170_history import flex_xml
        ui = self.ui
        config = dict(ui.flex_config)
        ui.flex_config, ui.flex_config_scope = {}, None
        with TemporaryDirectory() as tmp, patch("app.BASE", Path(tmp)):
            store = TradeHistoryStore(Path(tmp, "data", "trade_history.sqlite3"))
            try:
                store.import_flex_batch([(flex_xml(), ".xml")], "U123", "LIVE", config["timezone"], query_id="20",
                                        report_windows=[(date(2026, 10, 23), date(2026, 11, 20))])
            finally:
                store.close()
            with patch("flex_sync.load_credentials", return_value=config), patch("flex_sync.fetch_reports") as fetch:
                self.change("最近 7 日")
                ui.root.fire()
                fetch.assert_not_called()
                self.assertIn("已同步", ui.flex_feedback)
                self.change("最近 90 日")
                ui.root.fire()
                self.assertEqual(fetch.call_count, 1)
                self.assertEqual(fetch.call_args.kwargs["known_windows"], ((date(2026, 10, 23), date(2026, 11, 20)),))

    def test_delayed_response_after_reconnect_is_discarded(self):
        ui = self.ui
        with patch("flex_sync.fetch_reports", side_effect=self.report):
            self.change("最近 7 日")
            ui.root.fire()
        ui.session_epoch += 1
        ui.clear_history_display()
        ui.submit.reset_mock()
        self.deliver()
        ui.submit.assert_not_called()
        self.assertFalse(ui.flex_windows)
