"""Offline execution-history persistence, accounting and raw decoder regressions."""
import asyncio
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace as NS
import unittest
from zoneinfo import ZoneInfo

from trade_history import TradeHistoryStore, HistoryError, attach_history_callbacks


UTC = timezone.utc
NOW = datetime(2026, 11, 19, 12, tzinfo=UTC)
UNSET = 1.7976931348623157e308


def record(exec_id="00000001.00000002.01.01", **updates):
    return dict(exec_id=exec_id, timestamp="2026-11-19T03:01:02+00:00", conid=123,
                symbol="SPY", local_symbol="SPY 261119C00730000", sec_type="OPT",
                expiry="20261119", right="C", strike=730, multiplier=100,
                currency="USD", action="BUY", quantity=2, price=1.25,
                commission=None, commission_currency="USD", realized_pnl=None,
                pnl_currency="USD", source="API", **updates)


def changed(exec_id="00000001.00000002.01.01", **updates):
    value = record(exec_id)
    value.update(updates)
    return value


def report(exec_id="00000001.00000002.01.01", commission=1., pnl=UNSET, currency="USD"):
    return NS(execId=exec_id, commission=commission, realizedPNL=pnl, currency=currency)


def flex_xml(account="U123", exec_id="00000001.00000002.01.01", **overrides):
    attrs = dict(accountId=account, levelOfDetail="EXECUTION", assetCategory="OPT", currency="USD",
                 symbol="SPY 261119C00730000", underlyingSymbol="SPY", conid="123", multiplier="100",
                 expiry="20261119", putCall="C", strike="730", ibExecID=exec_id,
                 dateTime="20261118;220102", buySell="BUY", quantity="2", tradePrice="1.25",
                 ibCommission="-1.2", ibCommissionCurrency="USD", fifoPnlRealized="12.8")
    attrs.update(overrides)
    import xml.etree.ElementTree as ET
    root = ET.Element("FlexQueryResponse")
    statement = ET.SubElement(ET.SubElement(root, "FlexStatements"), "FlexStatement", accountId=account)
    ET.SubElement(ET.SubElement(statement, "Trades"), "Trade", attrs)
    return ET.tostring(root, encoding="unicode")


class HistoryFixture(unittest.TestCase):
    def setUp(self):
        self.store = TradeHistoryStore()

    def tearDown(self):
        self.store.close()

    def history(self, period="today", **kwargs):
        return self.store.history("U123", "LIVE", period, now=NOW, **kwargs)


class HistoryStoreTests(HistoryFixture):
    def test_partial_fills_dedup_and_commission_late_arrival(self):
        first = record()
        self.assertTrue(self.store.record_execution("U123", "LIVE", first))
        self.assertFalse(self.store.record_execution("U123", "LIVE", first))
        self.store.record_execution("U123", "LIVE", changed("00000001.00000002.02.01", quantity=1))
        result = self.history()
        self.assertEqual(len(result["rows"]), 2)
        total = result["totals_by_currency"]["USD"]
        self.assertEqual((total["quantity"], total["gross_buy"]), (3, 375))
        self.assertIsNone(total["realized_pnl"])
        self.assertEqual(result["missing_pnl_count"], 2)
        self.store.record_ib_commission("LIVE", "U123", report(pnl=8.5))
        total = self.history()["totals_by_currency"]["USD"]
        self.assertEqual((total["commission"], total["realized_pnl"]), (1, 8.5))
        self.assertFalse(total["pnl_complete"])
        self.assertEqual(total["missing_pnl_count"], 1)

    def test_correction_supersedes_original_even_if_original_replayed_later(self):
        self.store.record_execution("U123", "LIVE", record())
        self.store.record_ib_commission("LIVE", "U123", report(pnl=50))
        corrected = changed("00000001.00000002.01.02", quantity=1, price=1.30)
        self.store.record_execution("U123", "LIVE", corrected)
        self.store.record_execution("U123", "LIVE", record())
        result = self.history()
        self.assertEqual(len(result["rows"]), 1)
        self.assertEqual(result["rows"][0]["gross"], 130)
        self.assertIsNone(result["rows"][0]["realized_pnl"])
        self.store.record_ib_commission("LIVE", "U123", report(corrected["exec_id"], pnl=-3.2))
        self.assertEqual(self.history()["totals_by_currency"]["USD"]["realized_pnl"], -3.2)

    def test_corrected_fill_moved_outside_period_does_not_resurrect_original(self):
        self.store.record_execution("U123", "LIVE", record())
        self.store.record_execution("U123", "LIVE", changed("00000001.00000002.01.02", timestamp="2026-11-17T03:01:02+00:00"))
        self.assertEqual(self.history()["rows"], [])
        self.assertEqual(len(self.history("7d")["rows"]), 1)

    def test_zero_quantity_correction_busts_original(self):
        self.store.record_execution("U123", "LIVE", record())
        self.store.record_execution("U123", "LIVE", changed("00000001.00000002.01.02", quantity=0))
        self.assertEqual(self.history("30d")["rows"], [])

    def test_account_and_mode_scopes_are_strict(self):
        for account, mode in (("U123", "LIVE"), ("U456", "LIVE"), ("U123", "PAPER")):
            self.store.record_execution(account, mode, record())
        self.assertEqual(len(self.history()["rows"]), 1)
        self.assertEqual(self.store.history("U999", "LIVE", now=NOW)["rows"], [])

    def test_account_history_includes_stocks_and_options(self):
        self.store.record_execution("U123", "LIVE", record())
        self.store.record_execution("U123", "LIVE", changed("stock.abc.01.01", sec_type="STK",
            conid=999, quantity=100, price=600, multiplier=1, commission=2, realized_pnl=900))
        result = self.history()
        self.assertEqual(len(result["rows"]), 2)
        self.assertEqual(result["totals_by_currency"]["USD"]["gross"], 60250)
        self.assertEqual(result["totals_by_currency"]["USD"]["realized_pnl"], 900)

    def test_realized_pnl_already_net_is_not_reduced_twice(self):
        self.store.record_execution("U123", "LIVE", changed(action="SELL", commission=1.2, realized_pnl=12.8))
        result = self.history()
        self.assertEqual(result["totals_by_currency"]["USD"]["realized_pnl"], 12.8)
        self.assertEqual(result["rows"][0]["net_cash"], 248.8)

    def test_unknown_sentinels_and_unpopulated_commission_report_do_not_become_zero(self):
        self.store.record_execution("U123", "LIVE", changed(commission=UNSET, realized_pnl=float("nan")))
        self.store.record_ib_commission("LIVE", "U123", report(commission=UNSET))
        row = self.history()["rows"][0]
        self.assertIsNone(row["commission"])
        self.assertIsNone(row["realized_pnl"])
        self.assertIsNone(row["net_cash"])

    def test_commission_before_execution_is_merged_and_explicit_zero_pnl_preserved(self):
        self.store.record_ib_commission("LIVE", "U123", report(commission=-.10, pnl=0))
        self.store.record_execution("U123", "LIVE", record())
        row = self.history()["rows"][0]
        self.assertEqual(row["commission"], -.10)
        self.assertEqual(row["realized_pnl"], 0)
        self.assertEqual(row["net_cash"], -249.9)

    def test_currency_totals_never_mix_and_api_refresh_does_not_replace_fee_currency(self):
        self.store.record_execution("U123", "LIVE", record())
        self.store.record_ib_commission("LIVE", "U123", report(commission=1.2, pnl=9, currency="EUR"))
        self.store.record_execution("U123", "LIVE", record())
        result = self.history()
        self.assertEqual(result["totals_by_currency"]["EUR"]["realized_pnl"], 9)
        self.assertIsNone(result["totals_by_currency"]["USD"]["realized_pnl"])
        self.assertIsNone(result["rows"][0]["net_cash"])

    def test_today_and_period_windows_follow_selected_display_timezone(self):
        self.store.record_execution("U123", "LIVE", changed(timestamp="2026-11-18T17:01:02+00:00"))
        self.assertEqual(len(self.history(tz_name="Asia/Shanghai")["rows"]), 1)
        self.assertEqual(self.history(tz_name="America/New_York")["rows"], [])
        self.assertEqual(len(self.history("7d", tz_name="America/New_York")["rows"]), 1)
        self.assertEqual(self.history("7d")["start_date"], "2026-11-13")

    def test_persistence_survives_store_reopen(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "history.sqlite3"
            store = TradeHistoryStore(path)
            store.record_execution("U123", "LIVE", record())
            store.close()
            store = TradeHistoryStore(path)
            try:
                self.assertEqual(len(store.history("U123", "LIVE", now=NOW)["rows"]), 1)
            finally:
                store.close()

    def test_naive_or_date_only_execution_time_is_rejected(self):
        for stamp in ("20261119", "2026-11-19T12:00:00", "2026-11-19", datetime(2026, 11, 19)):
            with self.subTest(stamp=stamp), self.assertRaises(HistoryError):
                self.store.record_execution("U123", "LIVE", changed(timestamp=stamp))


class FlexImportTests(HistoryFixture):
    def import_text(self, text, suffix=".xml", tz_name="America/New_York"):
        with TemporaryDirectory() as directory:
            path = Path(directory) / ("statement" + suffix)
            path.write_text(text, encoding="utf-8-sig")
            return self.store.import_flex(path, "U123", "LIVE", tz_name)

    def test_flex_xml_merges_api_execution_and_final_fees_pnl_take_priority(self):
        self.store.record_execution("U123", "LIVE", record())
        result = self.import_text(flex_xml())
        self.assertEqual((result["imported"], result["duplicates"]), (0, 1))
        self.store.record_ib_commission("LIVE", "U123", report(pnl=10))
        row = self.history()["rows"][0]
        self.assertEqual((row["commission"], row["realized_pnl"], row["source"]), (1.2, 12.8, "FLEX"))
        self.assertEqual(self.import_text(flex_xml())["duplicates"], 1)
        self.assertEqual(len(self.history()["rows"]), 1)

    def test_flex_csv_detailed_trade_import(self):
        text = ('ClientAccountID,AssetClass,CurrencyPrimary,Symbol,UnderlyingSymbol,Conid,Multiplier,IBExecID,DateTime,Buy/Sell,Quantity,TradePrice,IBCommission,IBCommissionCurrency,FifoPnlRealized\n'
                'U123,OPT,USD,SPY option,SPY,123,100,x.y.01.01,20261118;220102,SELL,-2,1.25,-1,USD,8\n')
        result = self.import_text(text, ".csv")
        self.assertEqual(result["imported"], 1)
        self.assertEqual(self.history()["rows"][0]["action"], "SELL")

    def test_flex_missing_fields_are_filled_by_late_raw_report(self):
        self.import_text(flex_xml(ibCommission="", fifoPnlRealized=""))
        self.store.record_ib_commission("LIVE", "U123", report(commission=1.1, pnl=7.2))
        row = self.history()["rows"][0]
        self.assertEqual((row["commission"], row["realized_pnl"]), (1.1, 7.2))
        self.store.record_ib_commission("LIVE", "U123", report(commission=1.2, pnl=7.1))
        row = self.history()["rows"][0]
        self.assertEqual((row["commission"], row["realized_pnl"]), (1.2, 7.1))

    def test_import_is_atomic_if_later_row_conflicts_with_saved_execution(self):
        import xml.etree.ElementTree as ET
        self.store.record_execution("U123", "LIVE", record())
        root = ET.fromstring(flex_xml(exec_id="a.b.02.01"))
        trades = root.find("./FlexStatements/FlexStatement/Trades")
        conflict = ET.fromstring(flex_xml(tradePrice="1.4")).find("./FlexStatements/FlexStatement/Trades/Trade")
        trades.append(conflict)
        with self.assertRaisesRegex(HistoryError, "冲突"):
            self.import_text(ET.tostring(root, encoding="unicode"))
        self.assertEqual(len(self.history()["rows"]), 1)
        self.assertEqual(self.history()["import_count"], 0)

    def test_missing_report_timezone_is_explicit_error_not_display_timezone_guess(self):
        with self.assertRaisesRegex(HistoryError, "时区"):
            self.import_text(flex_xml(), tz_name=None)
        self.assertEqual(self.history()["rows"], [])

    def test_file_timezone_offset_is_accepted_without_timezone_parameter(self):
        self.import_text(flex_xml(dateTime="2026-11-19T03:01:02Z"), tz_name=None)
        self.assertEqual(self.history()["rows"][0]["timestamp"], "2026-11-19T03:01:02+00:00")

    def test_same_execution_wrong_import_timezone_rolls_back(self):
        self.store.record_execution("U123", "LIVE", record())
        with self.assertRaisesRegex(HistoryError, "时间冲突"):
            self.import_text(flex_xml(), tz_name="Asia/Shanghai")
        self.assertIsNone(self.history()["rows"][0]["realized_pnl"])

    def test_daylight_savings_ambiguous_import_time_rejected(self):
        with self.assertRaisesRegex(HistoryError, "夏令时"):
            self.import_text(flex_xml(dateTime="20261101;013000"))

    def test_date_only_report_is_not_silently_midnight(self):
        with self.assertRaisesRegex(HistoryError, "时分秒"):
            self.import_text(flex_xml(dateTime="20261119"))

    def test_no_stable_exec_id_no_history_fabrication(self):
        with self.assertRaisesRegex(HistoryError, "Execution ID"):
            self.import_text(flex_xml(ibExecID=""))
        self.assertEqual(self.history()["rows"], [])

    def test_unsupported_cancel_records_rejected_instead_of_double_counting(self):
        with self.assertRaisesRegex(HistoryError, "取消或更正"):
            self.import_text(flex_xml(transactionType="TradeCancel", origTradeID="1234"))

    def test_activities_csv_and_xml_entities_rejected(self):
        with self.assertRaises(HistoryError):
            self.import_text('Trades,Header,Asset Category,Symbol\nTrades,Data,Equity,SPY', ".csv")
        with self.assertRaises(HistoryError):
            self.import_text('<!DOCTYPE foo [<!ENTITY x "bad">]><FlexQueryResponse/>')


class RawWrapperHistoryTests(unittest.TestCase):
    def setUp(self):
        from ib_async import IB, Option, Execution
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.ib = IB()
        self.store = TradeHistoryStore()
        self.logs = []
        attach_history_callbacks(self.ib, lambda: self.store, lambda: ("LIVE", "U123"), self.logs.append)
        self.contract = Option("SPY", "20261119", 730, "C", "SMART", conId=123, multiplier="100", currency="USD")
        self.contract.localSymbol = "SPY option"
        self.execution = Execution(execId="a.b.01.01", time=NOW, acctNumber="U123", side="BOT", shares=1, price=1.0)

    def tearDown(self):
        self.assertFalse(self.ib.isConnected())
        self.store.close()
        self.loop.close()
        asyncio.set_event_loop(None)

    def result(self):
        return self.store.history("U123", "LIVE", now=NOW)

    def wire_execution(self, stamp, exec_id="wire.utc.01.01", direct=False):
        # Real message 11 fields for the installed ib_async 2.1 decoder.
        fields = ["11", "0", "70", "123", "SPY", "OPT", "20261119", "730", "C", "100",
                  "SMART", "USD", "SPY option", "SPY", exec_id, stamp, "U123", "CBOE",
                  "BOT", "2", "1.25", "700", "31", "0", "2", "1.25", "QOPT1:test", "", "0", "", "1", "0"]
        decoder = self.ib.client.decoder
        decoder.serverVersion = 178
        (decoder.execDetails if direct else decoder.interpret)(fields)
        self.assertEqual(fields[15], stamp)  # Compatibility adapter must not mutate input.
        return self.ib.wrapper.fills[exec_id]

    def test_utc_wire_time_crosses_beijing_day_boundary_without_host_offset(self):
        self.ib.TimezoneTWS = "Asia/Shanghai"  # Old decoder incorrectly adds this zone.
        self.ib.wrapper.defaultTimezone = ZoneInfo("Asia/Shanghai")
        before = self.wire_execution("20261118-15:59:59", "wire.before.01.01")
        after = self.wire_execution("20261118-16:01:02", "wire.after.01.01")
        self.assertEqual(before.execution.time.astimezone(UTC).isoformat(), "2026-11-18T15:59:59+00:00")
        self.assertEqual(after.execution.time.astimezone(UTC).isoformat(), "2026-11-18T16:01:02+00:00")
        today = self.result()["rows"]
        self.assertEqual([(r["exec_id"], r["date"], r["time"]) for r in today],
                         [("wire.after.01.01", "2026-11-19", "00:01:02")])
        self.assertEqual(len(self.store.history("U123", "LIVE", "7d", now=NOW)["rows"]), 2)
        self.assertEqual(self.logs, [])

    def test_utc_wire_time_is_same_with_tws_new_york_or_utc(self):
        for index, zone in enumerate(("America/New_York", "UTC", "")):
            with self.subTest(zone=zone):
                self.ib.TimezoneTWS = zone
                fill = self.wire_execution("20261119-00:01:02", f"wire.zone{index}.01.01", direct=True)
                self.assertEqual(fill.execution.time.astimezone(UTC).isoformat(), "2026-11-19T00:01:02+00:00")

    def test_zone_bearing_and_tws_local_wire_times_preserve_normal_decoder_behavior(self):
        self.ib.TimezoneTWS = "America/New_York"
        for index, stamp in enumerate(("20261118 16:01:02 UTC", "20261118 11:01:02 America/New_York",
                                       "20261118  11:01:02")):
            with self.subTest(stamp=stamp):
                fill = self.wire_execution(stamp, f"wire.explicit{index}.01.01")
                self.assertEqual(fill.execution.time.astimezone(UTC).isoformat(), "2026-11-18T16:01:02+00:00")
                self.assertEqual((fill.execution.side, fill.execution.shares, fill.execution.price), ("BOT", 2, 1.25))
                self.assertEqual((fill.contract.conId, fill.contract.right, fill.contract.multiplier), (123, "C", "100"))

    def test_wire_dispatch_keeps_raw_unknown_pnl_and_does_not_wrap_twice(self):
        decoder = self.ib.client.decoder
        installed = decoder.handlers[11]
        self.assertFalse(attach_history_callbacks(self.ib, lambda: self.store, lambda: ("LIVE", "U123")))
        self.assertIs(decoder.handlers[11], installed)
        self.assertIs(decoder.execDetails, installed)
        fill = self.wire_execution("20261119-03:01:02")
        decoder.interpret(["59", "1", fill.execution.execId, "1.2", "USD", str(UNSET), "0", "0"])
        row = self.result()["rows"][0]
        self.assertEqual(row["timestamp"], "2026-11-19T03:01:02+00:00")
        self.assertEqual(row["commission"], 1.2)
        self.assertIsNone(row["realized_pnl"])
        self.assertEqual(fill.commissionReport.realizedPNL, 0)  # Original library still processes report.

    def test_decoder_compatibility_is_instance_only_and_survives_wrapper_reset(self):
        from ib_async import IB
        other = IB()
        self.assertFalse(getattr(other.client.decoder, "_qopt_execution_time_compat", False))
        self.ib.wrapper.reset()
        self.ib.TimezoneTWS = "Asia/Shanghai"
        fill = self.wire_execution("20261119-03:01:02")
        self.assertEqual(fill.execution.time.astimezone(UTC).isoformat(), "2026-11-19T03:01:02+00:00")
        self.assertEqual(len(self.result()["rows"]), 1)

    def test_raw_callback_captures_trade_without_order_and_preserves_unknown_pnl(self):
        from ib_async import CommissionReport
        self.ib.wrapper.execDetails(0, self.contract, self.execution)
        value = CommissionReport(execId=self.execution.execId, commission=1, currency="USD", realizedPNL=UNSET)
        self.ib.wrapper.commissionReport(value)
        self.assertEqual(value.realizedPNL, 0)  # Library normalizes it, collector did not.
        self.assertIsNone(self.result()["rows"][0]["realized_pnl"])
        self.assertEqual(self.result()["rows"][0]["commission"], 1)
        self.assertEqual(self.logs, [])
        self.store.record_ib_fill("LIVE", self.ib.wrapper.fills[self.execution.execId], "U123")
        self.assertIsNone(self.result()["rows"][0]["realized_pnl"])

    def test_commission_arriving_before_execution_waits_for_account_proof(self):
        from ib_async import CommissionReport
        self.ib.wrapper.commissionReport(CommissionReport(execId=self.execution.execId, commission=1, currency="USD", realizedPNL=9))
        self.assertEqual(self.result()["rows"], [])
        self.ib.wrapper.execDetails(0, self.contract, self.execution)
        self.assertEqual(self.result()["rows"][0]["realized_pnl"], 9)

    def test_other_account_is_not_saved_and_hook_is_installed_once(self):
        self.execution.acctNumber = "U456"
        self.ib.wrapper.execDetails(0, self.contract, self.execution)
        self.assertEqual(self.result()["rows"], [])
        self.assertFalse(attach_history_callbacks(self.ib, lambda: self.store, lambda: ("LIVE", "U123")))


if __name__ == "__main__":
    unittest.main()
