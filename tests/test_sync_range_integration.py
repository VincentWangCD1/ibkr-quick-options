"""Exercise the Flex downloader and ledger together, using a synthetic service."""
from datetime import datetime, timezone
from unittest import TestCase
from xml.etree import ElementTree as ET

from flex_sync import fetch_reports
from test_v170_history import flex_xml
from test_v180 import ImmediateEvent
from trade_history import TradeHistoryStore


class SelectedRangeIntegrationTests(TestCase):
    now = datetime(2026, 11, 21, 19, tzinfo=timezone.utc)
    # Each row has $1 realized P&L. The NY late fill on Oct 23 is already
    # Oct 24 in Shanghai; the Jan 1 Beijing boundary falls in December in NY.
    dates = ("20251231;230000", "20260115;120000", "20260824;230000", "20260923;230000",
             "20261023;230000", "20261113;140000", "20261120;150000")

    def setUp(self):
        self.store = TradeHistoryStore()
        self.addCleanup(self.store.close)
        self.requests = []

    def service(self, endpoint, params):
        if endpoint == "SendRequest":
            self.requests.append((params["fd"], params["td"]))
            return f"<Response><Status>Success</Status><ReferenceCode>{len(self.requests)}</ReferenceCode></Response>"
        start, end = self.requests[int(params["q"]) - 1]
        root = ET.fromstring(flex_xml())
        statement = root.find(".//FlexStatement")
        statement.attrib.update(fromDate=start, toDate=end)
        trades = statement.find("Trades")
        trades.clear()
        for i, stamp in enumerate(self.dates):
            if start <= stamp[:8] <= end:
                trade = ET.fromstring(flex_xml(exec_id=f"execution-{i}", dateTime=stamp, fifoPnlRealized="1"))
                trades.append(trade.find(".//Trade"))
        return ET.tostring(root, encoding="unicode")

    def test_presets_and_custom_select_correct_fills_after_sync_and_replay(self):
        for period, expected in (("30d", 3), ("60d", 4), ("90d", 5), ("ytd", 7),
                                 (("2025-12-31", "2026-01-01"), 1),
                                 (("2026-10-24", "2026-10-24"), 1), (("2026-03-03", "2026-03-03"), 0)):
            with self.subTest(period=period):
                reports = fetch_reports("123", "456", period, "Asia/Shanghai", "America/New_York",
                                        now=self.now, cancel=ImmediateEvent(), get=self.service,
                                        on_coverage=lambda message: None, account="U123")
                result = self.store.import_flex_batch([(raw, ".xml") for raw in reports], "U123", "LIVE", "America/New_York")
                self.assertEqual(result["skipped"], 0)
                data = self.store.history("U123", "LIVE", period, self.now, "Asia/Shanghai")
                self.assertEqual(len(data["rows"]), expected)
                self.assertEqual(data["totals_by_currency"].get("USD", {}).get("realized_pnl"), expected or None)
                self.assertEqual(data["last_session_pnl"]["by_currency"], {"USD": 1.0})
