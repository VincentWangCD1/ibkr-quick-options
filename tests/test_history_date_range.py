"""Local history filters and completed US-equity session P&L."""
from datetime import date, datetime, timezone
import unittest

from test_v170_history import changed, flex_xml
from trade_history import HistoryError, TradeHistoryStore, last_completed_equity_session, period_dates


def instant(stamp):
    return datetime.fromisoformat(stamp).astimezone(timezone.utc)


class HistoryDateRangeTests(unittest.TestCase):
    now = instant("2026-11-21T18:00:00+00:00")

    def test_verified_coverage_survives_restart_and_scope_isolation(self):
        from tempfile import TemporaryDirectory
        from pathlib import Path
        from trade_history import saved_report_windows
        zone = "America/New_York"
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "history.db"
            windows = [(date(2026, 10, 23), date(2026, 11, 20))]
            store = TradeHistoryStore(path)
            try:
                store.import_flex_batch([(flex_xml(), ".xml")], "U123", "LIVE", zone, query_id="55", report_windows=windows)
                with self.assertRaises(HistoryError):
                    store.import_flex_batch([(flex_xml(account="U456"), ".xml")], "U123", "LIVE", zone,
                                            query_id="66", report_windows=windows)
            finally:
                store.close()
            self.assertEqual(saved_report_windows(path, "U123", "LIVE", "55", zone), windows)
            for acct,mode,query,tz in [("U123","LIVE","66",zone), ("U456","LIVE","55",zone),
                                       ("U123","PAPER","55",zone), ("U123","LIVE","55","Asia/Shanghai")]:
                self.assertEqual(saved_report_windows(path, acct, mode, query, tz), [])

    def setUp(self):
        self.store = TradeHistoryStore()
        self.addCleanup(self.store.close)

    def add(self, exec_id, stamp, pnl, account="A", mode="LIVE", **fields):
        self.store.record_execution(account, mode, changed(exec_id, timestamp=stamp, realized_pnl=pnl, **fields))

    def test_custom_dates_validate_against_display_zone(self):
        self.assertEqual(period_dates(("2024-02-29", "2024-02-29"), self.now),
                         (date(2024, 2, 29), date(2024, 2, 29)))
        for dates in [("2026-11-21", "2026-11-20"), ("2025-02-29", "2025-03-01"),
                      ("2026-11-1", "2026-11-02"), ("2025-01-01",), ("2026-11-20", "2026-11-22")]:
            with self.subTest(dates=dates), self.assertRaises(HistoryError):
                period_dates(dates, self.now, "America/New_York")
        self.assertEqual(period_dates(["2026-11-22", "2026-11-22"], self.now, "Asia/Shanghai")[1], date(2026, 11, 22))

    def test_custom_end_and_spring_dst_boundary(self):
        start, end = ("2026-03-08", "2026-03-08")
        self.add("before", "2026-03-08T04:59:59+00:00", 10)
        self.add("start", "2026-03-08T05:00:00+00:00", 1)
        self.add("end", "2026-03-09T03:59:59+00:00", 2)
        self.add("after", "2026-03-09T04:00:00+00:00", 20)
        data = self.store.history("A", "LIVE", (start, end), self.now, "America/New_York")
        self.assertEqual((data["start_date"], data["end_date"]), (start, end))
        self.assertEqual([row["exec_id"] for row in data["rows"]], ["end", "start"])
        self.assertEqual(data["totals_by_currency"]["USD"]["realized_pnl"], 3)

    def test_nyse_core_close_with_holiday_and_half_day(self):
        for now, day in [
            ("2026-11-22T02:00:00+08:00", "2026-11-20"),
            ("2026-11-23T15:59:59-05:00", "2026-11-20"),
            ("2026-11-23T16:00:00-05:00", "2026-11-23"),
            ("2026-11-26T17:00:00-05:00", "2026-11-25"),
            ("2026-11-27T12:59:59-05:00", "2026-11-25"),
            ("2026-11-27T13:00:00-05:00", "2026-11-27"),
            ("2026-04-03T16:05:00-04:00", "2026-04-02"),
            ("2025-01-09T17:00:00-05:00", "2025-01-08"),
        ]:
            with self.subTest(now=now):
                self.assertEqual(last_completed_equity_session(instant(now)), date.fromisoformat(day))
        self.assertIsNone(last_completed_equity_session(instant("2030-12-11T19:00:00-05:00")))

    def test_header_independent_of_filter_currency_account_and_corrections(self):
        friday = "2026-11-20T20:00:00+00:00"
        self.add("legacy", friday, 3)
        self.add("EUR", friday, -2, currency="EUR", pnl_currency="EUR")
        self.add("unknown", friday, None)
        self.add("other-account", friday, 200, account="B")
        self.add("paper", friday, 200, mode="PAPER")
        self.add("move.a.01", friday, 70)
        self.add("move.a.02", "2026-11-19T20:00:00+00:00", 70)
        self.add("bust.a.01", friday, 20)
        self.add("bust.a.02", friday, 20, quantity=0)
        self.store.import_flex_text(flex_xml(account="A", exec_id="flex-night", dateTime="20261119;220000",
                                            tradeDate="20261120", fifoPnlRealized="4"), ".xml", "A", "LIVE", "America/New_York")
        data = self.store.history("A", "LIVE", ("2026-11-01", "2026-11-02"), self.now)
        self.assertEqual(data["rows"], [])
        summary = data["last_session_pnl"]
        self.assertEqual(summary, self.store.last_session_pnl("A", "LIVE", self.now))
        self.assertEqual(summary["date"], "2026-11-20")
        self.assertEqual(summary["by_currency"], {"USD": 7, "EUR": -2})
        self.assertEqual((summary["fills"], summary["missing_pnl_count"]), (4, 1))
        self.assertEqual(summary["last_execution_at"], friday)

    def test_missing_and_known_zero_remain_distinct(self):
        self.assertEqual(self.store.last_session_pnl("A", "LIVE", self.now)["by_currency"], {})
        self.add("unknown", "2026-11-20T21:30:00+00:00", None)
        self.assertEqual(self.store.last_session_pnl("A", "LIVE", self.now)["by_currency"], {"USD": None})
        self.add("zero", "2026-11-20T22:00:00+00:00", 0)
        self.assertEqual(self.store.last_session_pnl("A", "LIVE", self.now)["by_currency"], {"USD": 0})
