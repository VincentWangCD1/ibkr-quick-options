"""Read-only simulated Flex date, coverage and pacing regressions."""
from datetime import date, datetime, timezone
from unittest.mock import Mock, patch
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from flex_sync import FlexPacing, FlexServiceError, fetch_reports, http_get, report_request_dates
from test_v180 import ImmediateEvent
from trade_history import HistoryError


WEEKEND = datetime(2026, 11, 21, 18, tzinfo=timezone.utc)
READY = "<Response><Status>Success</Status><ReferenceCode>99</ReferenceCode></Response>"
REPORT = """<FlexQueryResponse><FlexStatements>
<FlexStatement accountId="TEST" fromDate="20261001" toDate="20261010"><Trades/></FlexStatement>
<FlexStatement accountId="TEST" fromDate="2026-10-12" toDate="2026-10-15"><Trades/></FlexStatement>
<FlexStatement accountId="TEST" fromDate="20261011" toDate="20261011"/>
<FlexStatement accountId="OTHER" fromDate="20260901" toDate="20261120"><Trades/></FlexStatement>
</FlexStatements></FlexQueryResponse>"""


class FlexRangeDownloadTests(unittest.TestCase):
    def fetch(self, get, period, **options):
        return fetch_reports("111", "22", period, "Asia/Shanghai", "America/New_York",
                             cancel=ImmediateEvent(), get=get, now=WEEKEND, **options)

    def test_past_custom_end_and_two_timezone_edges(self):
        start, stop = report_request_dates(("2026-10-04", "2026-10-12"), "Asia/Shanghai", "America/New_York", WEEKEND)
        self.assertEqual((start, stop), (date(2026, 10, 3), date(2026, 10, 12)))
        start, stop = report_request_dates(("2026-10-04", "2026-10-12"), "America/New_York", "Asia/Shanghai", WEEKEND)
        self.assertEqual((start, stop), (date(2026, 10, 4), date(2026, 10, 13)))

    def test_weekend_only_uses_local_history_without_a_failed_request(self):
        get = Mock()
        with self.assertRaisesRegex(HistoryError, "尚未出具"):
            self.fetch(get, "today")
        get.assert_not_called()

    def test_caches_only_dated_windows_for_selected_account(self):
        cache, notes = [], []
        self.fetch(Mock(side_effect=[READY, REPORT]), ("2026-10-04", "2026-10-14"),
                   on_windows=lambda a, b: cache.append((a, b)), account="TEST", on_coverage=notes.append)
        self.assertEqual(cache, [(date(2026, 10, 3), date(2026, 10, 10)), (date(2026, 10, 12), date(2026, 10, 14))])
        self.assertIn("2026-10-11", " ".join(notes))

    def test_available_year_retained_with_previous_year_declared_missing(self):
        get = Mock(side_effect=[READY, "<FlexStatement/>", "<Response><ErrorCode>1003</ErrorCode></Response>"])
        notes = []
        self.assertEqual(len(self.fetch(get, "ytd", on_coverage=notes.append)), 1)
        self.assertIn("2025-12-31", " ".join(notes))

    def test_long_1003_fallback_imports_contiguous_smaller_windows(self):
        unavailable = "<Response><ErrorCode>1003</ErrorCode></Response>"
        windows = []
        def get(endpoint, params):
            if endpoint == "GetStatement":
                return "<FlexStatement/>"
            windows.append((params["fd"], params["td"]))
            return unavailable if len(windows) == 1 else READY
        self.assertEqual(len(self.fetch(get, "90d", on_coverage=lambda note: None, pacing=FlexPacing())), 3)
        self.assertEqual(windows[1:], [("20261022", "20261120"), ("20260922", "20261021"), ("20260824", "20260921")])

    def test_repeated_unavailable_has_bounded_requests_and_disclosed_gap(self):
        unavailable = "<Response><ErrorCode>1003</ErrorCode></Response>"
        get = Mock(side_effect=[unavailable, READY, "<FlexStatement/>", unavailable, unavailable])
        notes = []
        reports = self.fetch(get, "90d", on_coverage=notes.append, pacing=FlexPacing())
        self.assertEqual(len(reports), 1)
        self.assertEqual(get.call_count, 5)
        self.assertIn("2026-08-24 至 2026-10-21 未取得", " ".join(notes))

    def test_late_lockout_returns_completed_window_and_retry_skips_cached_dates(self):
        ready_report = '<FlexStatement accountId="TEST" fromDate="20260101" toDate="20261120"><Trades/></FlexStatement>'
        get = Mock(side_effect=[READY, ready_report, "<Response><ErrorCode>1025</ErrorCode></Response>"])
        cache, notes, deferred = [], [], []
        opts = dict(account="TEST", on_windows=lambda a, b: cache.append((a, b)), on_coverage=notes.append,
                    pacing=FlexPacing(), on_deferred=deferred.append)
        reports = self.fetch(get, "ytd", **opts)
        self.assertEqual(reports, [ready_report])
        self.assertEqual(deferred[0].retry_after, 900)
        self.assertIn("2025-12-31 至 2025-12-31 未取得", " ".join(notes))
        get = Mock(side_effect=[READY, '<FlexStatement accountId="TEST" fromDate="20251231" toDate="20251231"><Trades/></FlexStatement>'])
        opts["pacing"] = FlexPacing()
        self.assertEqual(len(self.fetch(get, "ytd", known_windows=cache, **opts)), 1)
        self.assertEqual(get.call_args_list[0].args[1]["td"], "20251231")
        get.reset_mock()
        self.assertEqual(self.fetch(get, "ytd", known_windows=cache, **opts), [])
        get.assert_not_called()

    def test_missing_trades_section_never_cache_dates_as_empty_execution_history(self):
        cache, notes = [], []
        missing = '<FlexStatement accountId="TEST" fromDate="20261002" toDate="20261015"/>'
        get = Mock(side_effect=[READY, missing])
        with self.assertRaisesRegex(HistoryError, "Trades"):
            self.fetch(get, ("2026-10-04", "2026-10-14"), account="TEST",
                       on_coverage=notes.append, on_windows=lambda a, b: cache.append((a, b)))
        self.assertEqual(cache, [])

    def test_newly_eligible_day_can_retry_after_prior_days_are_imported(self):
        unavailable = "<Response><ErrorCode>1003</ErrorCode></Response>"
        earlier = '<FlexStatement accountId="TEST" fromDate="20261115" toDate="20261119"><Trades/></FlexStatement>'
        get = Mock(side_effect=[unavailable, READY, earlier])
        cache, notes, deferred = [], [], []
        result = self.fetch(get, "7d", account="TEST", on_coverage=notes.append, on_deferred=deferred.append,
                            on_windows=lambda a, b: cache.append((a, b)), pacing=FlexPacing())
        self.assertEqual(result, [earlier])
        self.assertTrue(deferred[0].auto_retry)
        self.assertEqual(cache, [(date(2026, 11, 15), date(2026, 11, 19))])
        self.assertIn("2026-11-20 至 2026-11-20 未取得", " ".join(notes))
        with self.assertRaises(FlexServiceError) as raised:
            self.fetch(Mock(return_value=unavailable), ("2026-10-01", "2026-10-02"), pacing=FlexPacing())
        self.assertFalse(raised.exception.auto_retry)

    def test_pending_statement_and_transient_network_errors_advertise_retry(self):
        progress = "<Response><ErrorCode>1019</ErrorCode></Response>"
        with self.assertRaises(FlexServiceError) as raised:
            self.fetch(Mock(side_effect=[READY] + [progress] * 40), "7d", pacing=FlexPacing())
        self.assertTrue(raised.exception.auto_retry)
        self.assertGreaterEqual(raised.exception.retry_after, 60)
        with patch("flex_sync.urllib.request.build_opener", side_effect=TimeoutError("confidential")):
            with self.assertRaises(FlexServiceError) as raised:
                http_get("SendRequest", {"t": "private"})
        self.assertTrue(raised.exception.auto_retry)
        self.assertNotIn("confidential", str(raised.exception))

    def test_dated_partial_report_triggers_one_bounded_gap_request(self):
        def report(a, b):
            return f'<FlexStatement accountId="TEST" fromDate="{a}" toDate="{b}"><Trades/></FlexStatement>'
        initial = report("20261012", "20261016")
        for tail in (report("20261006", "20261009"), initial):
            notes = []
            get = Mock(side_effect=[READY, initial, READY, tail])
            result = self.fetch(get, ("2026-10-05", "2026-10-16"), account="TEST", on_coverage=notes.append,
                                pacing=FlexPacing())
            self.assertEqual(get.call_count, 4)
            self.assertEqual(get.call_args_list[2].args[1]["td"], "20261011")
            self.assertIn(initial, result)
            self.assertTrue(notes)

    def test_1003_in_newer_historical_chunk_does_not_discard_available_older_chunk(self):
        unavailable = "<Response><ErrorCode>1003</ErrorCode></Response>"
        get = Mock(side_effect=[unavailable, unavailable, READY, "<FlexStatement/>"])
        notes = []
        self.fetch(get, ("2026-07-01", "2026-08-15"), on_coverage=notes.append, pacing=FlexPacing())
        self.assertEqual(get.call_count, 4)
        self.assertIn("2026-07-17 至 2026-08-15 未取得", " ".join(notes))

    def test_broker_rejection_blocks_same_token_across_ranges_but_allows_later_retry(self):
        for code in ("1018", "1025"):
            clock = Mock(return_value=100.0)
            pacing = FlexPacing(clock)
            get = Mock(side_effect=[f"<Response><ErrorCode>{code}</ErrorCode></Response>", READY, REPORT])
            for period in ("60d", "90d", "ytd"):
                with self.assertRaises(FlexServiceError) as raised:
                    self.fetch(get, period, pacing=pacing)
                self.assertEqual((raised.exception.code, raised.exception.retry_after), (code, 900 if code == "1025" else 60))
            self.assertEqual(get.call_count, 1)
            clock.return_value = 1001.0
            self.assertEqual(len(self.fetch(get, "90d", pacing=pacing)), 1)

    def test_1025_survives_restart_and_different_range_with_one_recovery_probe(self):
        reject = "<Response><ErrorCode>1025</ErrorCode></Response>"
        with TemporaryDirectory() as tmp, patch("flex_sync.time.time", return_value=5000) as clock:
            path = Path(tmp) / "hold.sqlite3"
            get = Mock(side_effect=[reject, reject, READY, REPORT])
            def run(period):
                return self.fetch(get, period, pacing=FlexPacing(), hold_path=path)
            for when, period, seconds in ((5000, "30d", 900), (5060, "7d", 840), (5901, "60d", 1800)):
                clock.return_value = when
                with self.assertRaises(FlexServiceError) as failure:
                    run(period)
                self.assertEqual(failure.exception.retry_after, seconds)
            self.assertEqual(get.call_count, 2)
            clock.return_value = 7702
            self.assertEqual(len(run("90d")), 1)
            self.assertEqual(get.call_count, 4)

    def test_limit_applies_to_every_request_and_is_scoped_to_token(self):
        pacing = FlexPacing(lambda: 100.0)
        event = Mock()
        event.is_set.return_value = event.wait.return_value = False
        for _ in range(11):
            pacing.wait("scope-a", event, "报表请求", date.today(), date.today())
        delays = [call.args[0] for call in event.wait.call_args_list]
        self.assertGreaterEqual(delays[0], 1.0)
        self.assertGreaterEqual(delays[-1], 60.0)
        event.wait.reset_mock()
        pacing.wait("scope-b", event, "报表下载", date.today(), date.today())
        event.wait.assert_not_called()
