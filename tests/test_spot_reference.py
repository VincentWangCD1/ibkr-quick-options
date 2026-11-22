"""Offline SPX/ES reference tests: no sockets, quotes, accounts or orders."""
from datetime import date, datetime, timedelta, timezone
from importlib.util import find_spec
from types import SimpleNamespace as NS
import asyncio
import time
import unittest
from unittest.mock import Mock, AsyncMock

from spot_reference import (NY, ReferenceError, SpotReference, previous_spx_close,
                            closing_spx_minute, matching_es_close, scheduled_close, paired_spy_close)


def bar(stamp, close):
    return NS(date=stamp, close=close)


def future(conid=10, expiry="20261218", **overrides):
    values = dict(symbol="ES", secType="FUT", exchange="CME", currency="USD",
                  multiplier="50", conId=conid, localSymbol="ESZ6",
                  lastTradeDateOrContractMonth=expiry)
    values.update(overrides)
    return NS(**values)


def detail(contract):
    return NS(contract=contract, realExpirationDate=contract.lastTradeDateOrContractMonth,
              validExchanges="CME")


def schedule(day, hour=16):
    text = day.strftime("%Y%m%d")
    return NS(timeZone="US/Eastern", sessions=[NS(refDate=text,
              startDateTime=text + "-09:30:00", endDateTime=text + f"-{hour:02}:00:00")])


class ClosingSampleTests(unittest.TestCase):
    def test_previous_actual_bar_handles_weekends_holidays_and_unfinished_today(self):
        # Monday after Thanksgiving: Friday's half session is the last trade day.
        bars = [bar("20261125", 5900), bar(date(2026, 11, 27), 6000),
                bar("20261130", 9999)]
        result = previous_spx_close(bars, date(2026, 11, 30))
        self.assertEqual(result["basis_date"], "2026-11-27")
        self.assertEqual(result["daily_close"], 6000)
        # Thanksgiving itself has no row; use the actual Wednesday row.
        result = previous_spx_close(bars[:1], date(2026, 11, 27))
        self.assertEqual(result["basis_date"], "2026-11-25")

    def test_early_close_uses_last_returned_minute_not_assumed_1600(self):
        stamp = datetime(2026, 11, 27, 12, 59, tzinfo=NY)
        result, close = closing_spx_minute(
            [bar(stamp - timedelta(minutes=1), 5999), bar(stamp, 6000)],
            date(2026, 11, 27), 6000)
        self.assertEqual(result, stamp.astimezone(timezone.utc))
        self.assertEqual(close, 6000)
        self.assertEqual(matching_es_close([bar(result, 6020)], result), 6020)

    def test_missing_or_wrong_time_cannot_be_approximated(self):
        stamp = datetime(2026, 11, 10, 15, 59, tzinfo=NY)
        # Official daily close and synchronized minute close need not agree.
        self.assertEqual(closing_spx_minute([bar(stamp, 5990)], stamp.date(), 6000)[1], 5990)
        with self.assertRaisesRegex(ReferenceError, "完全同时间戳"):
            matching_es_close([bar(stamp - timedelta(minutes=1), 6020)], stamp)
        with self.assertRaisesRegex(ReferenceError, "时区"):
            matching_es_close([bar(stamp.replace(tzinfo=None), 6020)], stamp)

    def test_conflicting_or_stale_history_is_rejected(self):
        with self.assertRaisesRegex(ReferenceError, "冲突"):
            previous_spx_close([bar("20261110", 6000), bar("20261110", 6001)], date(2026, 11, 11))
        with self.assertRaisesRegex(ReferenceError, "超过 10"):
            previous_spx_close([bar("20261001", 6000)], date(2026, 11, 11))
        stamp = datetime(2026, 11, 10, 20, 59, tzinfo=timezone.utc)
        with self.assertRaisesRegex(ReferenceError, "冲突"):
            matching_es_close([bar(stamp, 6020), bar(stamp, 6021)], stamp)


@unittest.skipUnless(find_spec("ib_async"), "Install requirements.txt for mock ES contract tests")
class SpotReferenceTests(unittest.TestCase):
    def setUp(self):
        self.loop = asyncio.new_event_loop()
        self.today = datetime(2026, 11, 11, 8, tzinfo=NY)
        self.stamp = datetime(2026, 11, 10, 15, 59, tzinfo=NY)
        self.underlying = NS(symbol="SPX", secType="IND", conId=416904, localSymbol="SPX")
        self.spy = NS(symbol="SPY", secType="STK", conId=756733, localSymbol="SPY",
                      currency="USD", exchange="SMART", primaryExchange="ARCA")
        self.contract = future()
        self.ib = Mock()
        self.ib._run.side_effect = self.loop.run_until_complete
        self.ib.reqHistoricalScheduleAsync = AsyncMock(side_effect=lambda *a, **k: schedule(self.stamp.date()))
        self.ib.reqContractDetails.return_value = [detail(self.contract)]
        self.daily = [bar("20261109", 5990), bar("20261110", 6000)]
        self.spx_minutes = [bar(self.stamp, 6000)]
        self.es_minutes = [bar(self.stamp, 6020)]
        self.ib.reqHistoricalData.side_effect = self.history
        self.ticker = NS(contract=self.contract, marketDataType=1, halted=0,
                         last=6030, bid=6029.75, ask=6030.25)
        self.subscribe = Mock(return_value=self.ticker)
        self.times = {10: {1: time.monotonic(), 2: time.monotonic(), 4: time.monotonic()}}
        self.log = Mock()
        self.reference = SpotReference(self.ib, self.subscribe, self.log, now=lambda: self.today,
                                       resolve_underlying=lambda symbol: self.spy if symbol == "SPY" else self.underlying)

    def tearDown(self):
        self.loop.close()

    def history(self, contract, **kwargs):
        if kwargs["barSizeSetting"] == "1 day":
            return self.daily
        return self.spx_minutes if contract.secType == "IND" else self.es_minutes

    def test_same_minute_basis_and_explicit_live_contract_produce_labelled_estimate(self):
        result = self.reference.get(self.underlying, self.times)
        self.assertEqual(result["spot"], 6010)
        self.assertEqual(result["offset"], -20)
        self.assertEqual(result["source"], "ES_ADJUSTED_ESTIMATE")
        self.assertTrue(result["estimated"])
        self.assertFalse(result["usable_for_order"])
        self.assertEqual(result["basis_time"], "2026-11-10T21:00:00+00:00")
        self.assertIn("同一分钟", result["basis_precision"])
        calls = self.ib.reqHistoricalData.call_args_list
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[2].args[0].conId, self.subscribe.call_args.args[0].conId)
        self.assertFalse(calls[2].kwargs["useRTH"])
        for call in calls:
            self.assertEqual(call.kwargs["formatDate"], 2)
            self.assertFalse(call.kwargs["keepUpToDate"])

    def test_daily_basis_is_cached_while_current_es_reprices(self):
        first = self.reference.get(self.underlying, self.times)
        self.ticker.last = 6040
        second = self.reference.get(self.underlying, self.times)
        self.assertEqual((first["spot"], second["spot"]), (6010, 6020))
        self.assertEqual(self.ib.reqHistoricalData.call_count, 3)
        self.assertEqual(self.ib.reqContractDetails.call_count, 1)
        self.assertEqual(first["offset"], second["offset"])

    def test_new_day_and_roll_recalibrate_against_same_new_contract(self):
        self.reference.get(self.underlying, self.times)
        self.today += timedelta(days=1)
        self.stamp += timedelta(days=1)
        self.daily = [bar("20261111", 6010)]
        self.spx_minutes = [bar(self.stamp, 6010)]
        self.es_minutes = [bar(self.stamp, 6040)]
        new_contract = future(11, "20270319", localSymbol="ESH7")
        self.ib.reqContractDetails.return_value = [detail(new_contract)]
        self.ticker.contract = new_contract
        self.ticker.last = 6050
        self.times[11] = {4: time.monotonic()}
        result = self.reference.get(self.underlying, self.times)
        self.assertEqual(result["basis_date"], "2026-11-11")
        self.assertEqual((result["es_conid"], result["offset"], result["spot"]), (11, -30, 6020))
        self.assertEqual(self.ib.reqHistoricalData.call_args.args[0].conId, 11)
        self.assertEqual(self.ib.reqHistoricalData.call_count, 6)

    def test_contract_filter_excludes_expiry_day_mes_and_continuous(self):
        self.today = datetime(2026, 12, 18, 8, tzinfo=NY)
        current = future(12, "20270319", localSymbol="ESH7")
        contracts = [future(10, "20261218"), future(11, "20270319", symbol="MES", multiplier="5"),
                     future(13, "20270319", secType="CONTFUT"), current,
                     future(14, "20270618", localSymbol="ESM7")]
        self.ib.reqContractDetails.return_value = [detail(c) for c in contracts]
        selected = self.reference._es(self.today.date())
        self.assertEqual(selected.conId, 12)
        self.assertEqual(selected.exchange, "CME")

    def test_ambiguous_explicit_contracts_are_not_guessed(self):
        self.ib.reqContractDetails.return_value = [detail(future(10)), detail(future(11))]
        result = self.reference.get(self.underlying, self.times)
        self.assertEqual(result["source"], "SPX_PREVIOUS_CLOSE")
        self.assertIn("不唯一", result["error"])
        self.assertTrue(all(call.args[0].symbol == "SPY" for call in self.subscribe.call_args_list))

    def test_delayed_frozen_stale_or_missing_es_falls_back_without_minute_history(self):
        cases = ((2, 0, 0), (3, 0, 0), (1, 1, 0), (1, 0, 60))
        for kind, halted, age in cases:
            with self.subTest(kind=kind, halted=halted, age=age):
                self.reference.clear()
                self.ib.reqHistoricalData.reset_mock()
                self.ticker.marketDataType, self.ticker.halted = kind, halted
                self.times[10] = {t: time.monotonic() - age for t in (1, 2, 4)}
                result = self.reference.get(self.underlying, self.times)
                self.assertEqual(result["spot"], 6000)
                self.assertEqual(result["source"], "SPX_PREVIOUS_CLOSE")
                self.assertFalse(result["estimated"])
                self.assertIn("非实时", result["kind"])
                self.assertTrue(result["error"])
                self.assertEqual(self.ib.reqHistoricalData.call_count, 1)

    def test_fresh_bid_ask_is_allowed_but_crossed_or_future_time_is_not(self):
        self.ticker.last = float("nan")
        result = self.reference.get(self.underlying, self.times)
        self.assertEqual(result["spot"], 6010)
        self.assertEqual(result["es_quote_source"], "ES 买卖中间价")
        self.ticker.bid = 6040
        self.assertEqual(self.reference.get(self.underlying, self.times)["source"], "SPX_PREVIOUS_CLOSE")
        self.ticker.bid = 6029.75
        self.times[10] = {1: time.monotonic() + 60, 2: time.monotonic() + 60}
        self.assertEqual(self.reference.get(self.underlying, self.times)["source"], "SPX_PREVIOUS_CLOSE")

    def test_missing_same_time_es_bar_throttles_retry_and_never_invents_offset(self):
        self.es_minutes = [bar(self.stamp - timedelta(minutes=1), 6020)]
        result = self.reference.get(self.underlying, self.times)
        self.assertEqual(result["source"], "SPX_PREVIOUS_CLOSE")
        self.assertIn("完全同时间戳", result["error"])
        self.assertIsNone(result["offset"])
        self.reference.get(self.underlying, self.times)
        self.assertEqual(self.ib.reqHistoricalData.call_count, 3)

    def test_missing_spx_history_is_unknown_and_throttled(self):
        self.daily = []
        first = self.reference.get(self.underlying, self.times)
        second = self.reference.get(self.underlying, self.times)
        self.assertIsNone(first["spot"])
        self.assertEqual(first["source"], "unavailable")
        self.assertEqual(second["error"], first["error"])
        self.assertEqual(self.ib.reqHistoricalData.call_count, 1)
        self.ib.reqContractDetails.assert_not_called()

    def test_current_es_is_reread_after_historical_requests(self):
        def history_and_tick(contract, **kwargs):
            if contract.secType == "FUT":
                self.ticker.last = 6050
                self.times[10][4] = time.monotonic()
            return self.history(contract, **kwargs)
        self.ib.reqHistoricalData.side_effect = history_and_tick
        result = self.reference.get(self.underlying, self.times)
        self.assertEqual(result["spot"], 6030)
        self.assertEqual(result["es_price"], 6050)

    def test_spy_or_unknown_underlying_cannot_use_spx_basis(self):
        result = self.reference.get(NS(symbol="SPY", secType="STK"), self.times)
        self.assertIsNone(result["spot"])
        self.assertEqual(result["source"], "unavailable")
        self.ib.reqHistoricalData.assert_not_called()


@unittest.skipUnless(find_spec("ib_async"), "Install requirements.txt for reference contract tests")
class SpyRatioTests(unittest.TestCase):
    def setUp(self):
        SpotReferenceTests.setUp(self)
        self.underlying.currency = "USD"
        self.spy_minutes = [bar(self.stamp, 600)]
        self.spy_ticker = NS(contract=self.spy, marketDataType=1, halted=0,
                             last=601, bid=600.99, ask=601.01)
        self.times[self.spy.conId] = {4: time.monotonic()}
        self.subscribe.side_effect = lambda contract: self.spy_ticker if contract.symbol == "SPY" else self.ticker

    def tearDown(self):
        SpotReferenceTests.tearDown(self)

    def history(self, contract, **kwargs):
        if kwargs["barSizeSetting"] == "1 day":
            return self.daily
        if contract.symbol == "SPY":
            return self.spy_minutes
        return self.spx_minutes if contract.secType == "IND" else self.es_minutes

    def test_spy_ratio_has_priority_and_is_an_estimate_not_index_or_option_quote(self):
        result = self.reference.get(self.underlying, self.times)
        self.assertEqual((result["source"], result["spot"], result["ratio"]), ("SPY_RATIO_ESTIMATE", 6010, 10))
        self.assertTrue(result["anchor_verified"])
        self.assertTrue(result["estimated"])
        self.assertTrue(result["usable_for_matching"])
        self.assertFalse(result["usable_for_order"])
        self.assertIn("估算", result["kind"])
        self.assertEqual(result["basis_time"], "2026-11-10T21:00:00+00:00")
        self.ib.reqContractDetails.assert_not_called()  # no ES needed
        self.assertEqual(self.ib.reqHistoricalData.call_count, 3)
        for call in self.ib.reqHistoricalData.call_args_list:
            self.assertEqual(call.kwargs["whatToShow"], "TRADES")
            self.assertTrue(call.kwargs["useRTH"])

    def test_daily_close_difference_is_recorded_not_used_in_ratio_or_rejected(self):
        self.spx_minutes = [bar(self.stamp, 6006)]
        result = self.reference.get(self.underlying, self.times)
        self.assertEqual(result["source"], "SPY_RATIO_ESTIMATE")
        self.assertEqual(result["spx_daily_close_difference"], 6)
        self.assertEqual(result["spx_close"], 6000)
        self.assertAlmostEqual(result["spot"], 601 * 6006 / 600)

    def test_xsp_is_spx_estimate_divided_by_ten_and_shares_same_anchor(self):
        spx = self.reference.get(self.underlying, self.times)
        xsp = self.reference.get(NS(symbol="XSP", secType="IND", conId=99), self.times)
        self.assertEqual(xsp["spot"], spx["spot"] / 10)
        self.assertEqual(xsp["ratio"], spx["ratio"])
        self.assertEqual(xsp["symbol"], "XSP")
        self.assertIn("÷ 10", xsp["kind"])
        self.assertEqual(self.ib.reqHistoricalData.call_count, 3)

    def test_early_close_and_weekend_use_actual_friday_session(self):
        self.today = datetime(2026, 11, 30, 8, tzinfo=NY)
        self.stamp = datetime(2026, 11, 27, 12, 59, tzinfo=NY)
        self.daily = [bar("20261125", 5900), bar("20261127", 6000)]
        self.spx_minutes, self.spy_minutes = [bar(self.stamp, 6000)], [bar(self.stamp, 600)]
        self.ib.reqHistoricalScheduleAsync.side_effect = lambda *a, **k: schedule(self.stamp.date(), 13)
        result = self.reference.get(self.underlying, self.times)
        self.assertEqual(result["basis_date"], "2026-11-27")
        self.assertEqual(result["basis_time"], "2026-11-27T18:00:00+00:00")
        self.assertEqual(result["spot"], 6010)

    def test_incomplete_final_minute_is_not_replaced_with_an_arbitrary_last_bar(self):
        self.spx_minutes = [bar(self.stamp - timedelta(minutes=1), 6000)]
        self.spy_minutes = [bar(self.stamp - timedelta(minutes=1), 600)]
        self.ib.reqContractDetails.return_value = []
        result = self.reference.get(self.underlying, self.times)
        self.assertEqual(result["source"], "SPX_PREVIOUS_CLOSE")
        self.assertFalse(result["anchor_verified"])
        self.assertIn("最后一分钟", result["error"])

    def test_cross_date_or_shifted_spy_sample_is_rejected(self):
        self.ib.reqContractDetails.return_value = []
        for shift in (timedelta(days=-1), timedelta(minutes=-1)):
            with self.subTest(shift=shift):
                self.reference.clear()
                self.spy_minutes = [bar(self.stamp + shift, 600)]
                result = self.reference.get(self.underlying, self.times)
                self.assertEqual(result["source"], "SPX_PREVIOUS_CLOSE")
                self.assertFalse(result["anchor_verified"])

    def test_stale_spy_can_use_fresh_es_backup_but_never_old_spy_ratio(self):
        self.reference.get(self.underlying, self.times)
        self.times[self.spy.conId][4] = time.monotonic() - 60
        result = self.reference.get(self.underlying, self.times)
        self.assertEqual(result["source"], "ES_ADJUSTED_ESTIMATE")
        self.assertIn("SPY", result["fallback_reason"])
        self.assertIsNone(result["ratio"])

    def test_delayed_or_frozen_spy_cannot_enable_matching(self):
        self.ib.reqContractDetails.return_value = []
        for kind in (2, 3, 4):
            self.spy_ticker.marketDataType = kind
            result = self.reference.get(self.underlying, self.times)
            self.assertFalse(result["usable_for_matching"])
            self.assertEqual(result["source"], "SPX_PREVIOUS_CLOSE")

    def test_new_day_clears_ratio_and_calendar_cache(self):
        self.reference.get(self.underlying, self.times)
        self.today += timedelta(days=1)
        self.stamp += timedelta(days=1)
        self.daily = [bar("20261111", 6100)]
        self.spx_minutes, self.spy_minutes = [bar(self.stamp, 6100)], [bar(self.stamp, 600)]
        result = self.reference.get(self.underlying, self.times)
        self.assertEqual(result["basis_date"], "2026-11-11")
        self.assertAlmostEqual(result["ratio"], 6100 / 600)
        self.assertEqual(self.ib.reqHistoricalScheduleAsync.call_count, 2)
        self.assertEqual(len(self.reference._bases), 1)

    def test_missing_calendar_blocks_ratio_and_retries_are_throttled(self):
        self.ib.reqHistoricalScheduleAsync.return_value = NS(timeZone="US/Eastern", sessions=[])
        self.ib.reqHistoricalScheduleAsync.side_effect = None
        first = self.reference.get(self.underlying, self.times)
        second = self.reference.get(self.underlying, self.times)
        self.assertFalse(first["anchor_verified"])
        self.assertEqual(second["source"], "SPX_PREVIOUS_CLOSE")
        self.assertIn("不能猜测收盘", first["error"])
        self.assertEqual(self.ib.reqHistoricalScheduleAsync.call_count, 1)

    def test_post_history_live_spy_price_is_used(self):
        def history_tick(contract, **kwargs):
            if contract.symbol == "SPY":
                self.spy_ticker.last = 602
                self.times[self.spy.conId][4] = time.monotonic()
            return self.history(contract, **kwargs)
        self.ib.reqHistoricalData.side_effect = history_tick
        result = self.reference.get(self.underlying, self.times)
        self.assertEqual(result["spot"], 6020)
        self.assertEqual(result["spy_price"], 602)

    def test_calendar_wrong_day_or_unknown_timezone_is_rejected(self):
        with self.assertRaisesRegex(ReferenceError, "基准日"):
            scheduled_close(schedule(date(2026, 11, 9)), date(2026, 11, 10))
        data = schedule(date(2026, 11, 10))
        data.timeZone = ""
        with self.assertRaises(ReferenceError):
            scheduled_close(data, date(2026, 11, 10))

    def test_schedule_timeout_cancels_only_its_exact_request(self):
        own, other = self.loop.create_future(), self.loop.create_future()
        self.ib.wrapper._futures = {7: own, 8: other}
        self.ib.wrapper._endReq.side_effect = lambda reqid: self.ib.wrapper._futures.pop(reqid, None)
        self.ib.reqHistoricalScheduleAsync = Mock(return_value=own)
        self.reference.historical_timeout = .001
        result = self.reference.get(self.underlying, self.times)
        self.assertEqual(result["source"], "SPX_PREVIOUS_CLOSE")
        self.assertFalse(result["anchor_verified"])
        self.ib.client.cancelHistoricalData.assert_called_once_with(7)
        self.assertTrue(own.cancelled())
        self.assertFalse(other.cancelled())
        self.assertEqual(set(self.ib.wrapper._futures), {8})
        other.cancel()

    def test_new_york_midnight_during_history_cannot_return_old_anchor_as_ready(self):
        def history_and_midnight(contract, **kwargs):
            result = self.history(contract, **kwargs)
            if contract.symbol == "SPY":
                self.today += timedelta(days=1)
            return result
        self.ib.reqHistoricalData.side_effect = history_and_midnight
        result = self.reference.get(self.underlying, self.times)
        self.assertEqual(result["source"], "SPX_PREVIOUS_CLOSE")
        self.assertFalse(result["anchor_verified"])
        self.assertFalse(result["usable_for_matching"])
        self.assertIn("纽约日期已变化", result["error"])


if __name__ == "__main__":
    unittest.main()
