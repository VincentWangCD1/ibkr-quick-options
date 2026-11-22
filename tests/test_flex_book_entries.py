import unittest
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from unittest.mock import Mock

from flex_sync import fetch_reports
from test_app_state import controller, Variable, Widget, Tree
from test_v170_history import flex_xml, HistoryFixture
from test_v180 import ImmediateEvent
from trade_history import HistoryError


def mixed_report(*field_sets):
    root = ET.fromstring(flex_xml())
    trades = root.find(".//Trades")
    trades.clear()
    for fields in field_sets:
        trades.append(ET.fromstring(flex_xml(**fields)).find(".//Trade"))
    return ET.tostring(root, encoding="unicode")


class FlexBookingTests(HistoryFixture):
    book = dict(ibExecID="", tradeID="310", transactionType="BookTrade")

    def load(self, *fields):
        return self.store.import_flex_text(mixed_report(*fields), ".xml", "U123", "LIVE", "America/New_York")

    def test_mixed_book_and_execution_replay_separates_totals(self):
        rows = [self.book, dict(tradeID="311"), dict(ibExecID=" ", tradeID="312", transactionType="ExchTrade")]
        result = self.load(*rows)
        self.assertEqual((result["imported"], result["new_book_entries"], result["missing_execution_ids"]), (1, 1, 1))
        history = self.history()
        self.assertEqual(len(history["rows"]), 1)
        self.assertEqual(len(history["book_entries"]), 1)
        self.assertEqual(history["book_entries"][0]["realized_pnl"], 12.8)
        self.assertEqual(history["totals_by_currency"]["USD"]["realized_pnl"], 12.8)
        self.assertEqual(self.store.history("U123", "PAPER")["book_entries"], [])
        result = self.load(*reversed(rows))
        self.assertEqual((result["imported"], result["new_book_entries"], result["duplicates"]), (0, 0, 1))

    def test_later_identified_booking_and_replay_appear_once(self):
        self.load(self.book)
        self.load(dict(self.book, ibExecID="a.b.01.01"))
        self.load(self.book)
        history = self.history()
        self.assertEqual(len(history["rows"]), 1)
        self.assertEqual(history["book_entries"], [])

    def test_unidentifiable_execution_alone_does_not_record_an_import(self):
        with self.assertRaisesRegex(HistoryError, "IB Execution ID"):
            self.load(dict(ibExecID="", transactionType="BookTrade", tradeID="0"))
        self.assertEqual(self.history()["import_count"], 0)

    def test_book_replay_preserves_reported_pnl_and_conflicts_roll_back(self):
        self.load(self.book)
        self.load(dict(self.book, fifoPnlRealized=""))
        with self.assertRaisesRegex(HistoryError, "Trade ID"):
            self.load(dict(tradeID="313"), dict(self.book, quantity="6"))
        history = self.history()
        self.assertEqual(history["book_entries"][0]["realized_pnl"], 12.8)
        self.assertEqual(history["rows"], [])

    def test_corrected_row_aborts_entire_batch(self):
        with self.assertRaisesRegex(HistoryError, "取消或更正"):
            self.load(self.book, dict(transactionType="TradeCancel"))
        self.assertEqual(self.history()["book_entries"], [])


class WeekendAvailabilityTests(unittest.TestCase):
    unavailable = "<Response><ErrorCode>1003</ErrorCode></Response>"
    ready = "<Response><Status>Success</Status><ReferenceCode>44</ReferenceCode></Response>"

    def fetch(self, get, feedback):
        return fetch_reports("11", "22", "30d", "Asia/Shanghai", "America/New_York", ImmediateEvent(), get,
                             datetime(2026, 11, 21, 18, tzinfo=timezone.utc), on_coverage=feedback.append)

    def test_weekend_cutoff_sent_directly_and_disclosed(self):
        get = Mock(side_effect=[self.ready, flex_xml()])
        feedback = []
        self.assertEqual(len(self.fetch(get, feedback)), 1)
        self.assertEqual(get.call_args_list[0].args[1]["td"], "20261120")
        self.assertEqual(get.call_count, 2)
        self.assertIn("2026-11-20", feedback[0])

    def test_persistent_1003_has_dates_and_no_credentials_hint(self):
        get = Mock(return_value=self.unavailable)
        with self.assertRaisesRegex(HistoryError, "1003") as raised:
            self.fetch(get, [])
        self.assertIn("报表暂不可用", str(raised.exception))
        self.assertNotIn("Token", str(raised.exception))
        self.assertEqual(get.call_count, 1)

    def test_unavailable_download_reuses_reference(self):
        get = Mock(side_effect=[self.ready, self.unavailable, flex_xml()])
        self.assertEqual(len(self.fetch(get, [])), 1)
        self.assertEqual([c.args[0] for c in get.call_args_list], ["SendRequest", "GetStatement", "GetStatement"])
        self.assertEqual(get.call_args_list[1].args, get.call_args_list[2].args)


class FlexHistoryViewTests(unittest.TestCase):
    def test_book_view_and_sync_feedback_survive_refresh(self):
        ui = controller()
        ui.history_tree, ui.history_cards = Tree(), Mock()
        ui.history_tree.master = Widget()
        ui.history_range, ui.history_summary = Variable(""), Variable("")
        ui.history_pnl, ui.history_coverage = Variable(""), Variable("")
        ui.history_pnl_label, ui.history_view = Widget(), Variable("记账明细")
        ui.set_flex_feedback("同步统计")
        book = dict(date="2026-11-19", time="15:30:00", symbol="TEST", action="BUY", quantity=1,
                    currency="USD", price=1.25, gross=125, commission=None, commission_currency="", realized_pnl=None)
        ui.render_history(dict(rows=[], book_entries=[book], totals_by_currency={}))
        self.assertIn("1 行记账记录", ui.history_range.get())
        self.assertIn("同步统计", ui.history_coverage.get())
        self.assertEqual(next(iter(ui.history_tree.rows.values()))["values"][-1], "Flex BookTrade")
        ui.history_view.set("卡片")
        ui.change_history_view()
        self.assertIn("0 笔成交", ui.history_range.get())
        self.assertIn("同步统计", ui.history_coverage.get())
