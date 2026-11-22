"""Offline regression coverage for expiration, calendar and viewport repairs."""
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace as NS
import time
import unittest
from unittest.mock import Mock
from zoneinfo import ZoneInfo

from market_data import MarketDataMixin, MarketDataError, chain_page
from market_runtime import (expired_selection_reason, pin_quote_contract,
                            refresh_execution_calendar, reset_quote_stream,
                            selection_expiry_floor)
from trading_sessions import decide_session


def contract(conid=101, strike=740, right="C", expiry="20261119"):
    return NS(conId=conid, symbol="XSP", secType="OPT", currency="USD", right=right,
              strike=float(strike), multiplier="100", tradingClass="XSP",
              lastTradeDateOrContractMonth=expiry, localSymbol=f"XSP {expiry} {right}{strike}",
              exchange="SMART")


def detail(valid=True, **kwargs):
    return NS(contract=contract(**kwargs), underConId=100, validExchanges="SMART,CBOE",
              timeZoneId="America/New_York" if valid else "",
              tradingHours="20261119:0930-20261119:1600" if valid else "",
              liquidHours="20261119:0930-20261119:1600" if valid else "")


class ExpirySelectionTests(unittest.TestCase):
    def test_beijing_1255_is_previous_new_york_day_but_old_expiry_already_closed(self):
        now = datetime(2026, 11, 19, 12, 55, tzinfo=ZoneInfo("Asia/Shanghai"))
        self.assertEqual(now.astimezone(ZoneInfo("America/New_York")).strftime("%Y%m%d %H:%M"),
                         "20261118 23:55")
        self.assertEqual(selection_expiry_floor("XSP", now), "20261119")
        self.assertEqual(selection_expiry_floor("SPX", now), "20261119")
        self.assertIn("已过最后交易时段", expired_selection_reason(contract(expiry="20261118"), now))
        self.assertEqual(expired_selection_reason(contract(expiry="20261119"), now), "")

    def test_close_boundary_and_dst_are_new_york_based(self):
        zone = ZoneInfo("America/New_York")
        for month in (7, 11):
            with self.subTest(month=month):
                before = datetime(2026, month, 19, 15, 59, tzinfo=zone)
                after = datetime(2026, month, 19, 16, 0, tzinfo=zone)
                self.assertEqual(selection_expiry_floor("XSP", before), f"2026{month:02d}19")
                self.assertEqual(selection_expiry_floor("XSP", after), f"2026{month:02d}20")
                self.assertEqual(selection_expiry_floor("XSP", after.astimezone(timezone.utc)),
                                 selection_expiry_floor("XSP", after))


class CalendarRepairTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 19, 18, 0, tzinfo=timezone.utc)
        self.broker = NS(ib=Mock(), log=Mock())

    def test_bulk_missing_calendar_is_retried_by_exact_contract(self):
        original, verified = detail(False), detail()
        self.broker.ib.reqContractDetails.return_value = [verified]
        result = refresh_execution_calendar(self.broker, original, self.now)
        self.assertEqual(decide_session(result, self.now).kind, "regular")
        query = self.broker.ib.reqContractDetails.call_args.args[0]
        self.assertEqual((query.conId, query.exchange, query.right, query.strike),
                         (101, "SMART", "C", 740))
        self.assertEqual(original.timeZoneId, "")
        self.assertEqual(result.contract.conId, original.contract.conId)

    def test_verified_same_day_cache_reused_but_not_next_day(self):
        original, verified = detail(False), detail()
        self.broker.ib.reqContractDetails.return_value = [verified]
        first = refresh_execution_calendar(self.broker, original, self.now)
        self.assertIs(refresh_execution_calendar(self.broker, original, self.now), first)
        self.broker.ib.reqContractDetails.assert_called_once()
        # Another day cannot inherit the previous day's open calendar, even
        # when the old response still happens to be in the five-minute cache.
        next_original = detail(False, expiry="20261120")
        self.broker.ib.reqContractDetails.return_value = []
        result = refresh_execution_calendar(self.broker, next_original,
                                            datetime(2026, 11, 20, 18, tzinfo=timezone.utc))
        self.assertIs(result, next_original)
        self.assertEqual(self.broker.ib.reqContractDetails.call_count, 2)

    def test_wrong_identity_ambiguous_or_missing_timezone_never_borrow_calendar(self):
        original = detail(False)
        for candidates in ([detail(conid=999)], [detail(right="P")], [detail(strike=741)],
                           [detail(expiry="20261120")], [detail(False)], [detail(), detail()]):
            with self.subTest(candidates=len(candidates)):
                self.broker.ib.reqContractDetails.return_value = candidates
                result = refresh_execution_calendar(self.broker, original, self.now)
                self.assertIs(result, original)
                self.assertEqual(decide_session(result, self.now).kind, "closed")
                self.assertFalse(getattr(self.broker, "_verified_calendar_cache", {}))

    def test_complete_calendar_does_not_issue_extra_request(self):
        original = detail()
        self.assertIs(refresh_execution_calendar(self.broker, original, self.now), original)
        self.broker.ib.reqContractDetails.assert_not_called()

    def test_next_expiry_overnight_calendar_is_verified_without_guessing_timezone(self):
        original, verified = detail(False), detail()
        verified.tradingHours = "20261118:2015-20261119:0925;20261119:0930-20261119:1600"
        verified.liquidHours = "20261119:0930-20261119:1600"
        self.broker.ib.reqContractDetails.return_value = [verified]
        now = datetime(2026, 11, 19, 4, 55, tzinfo=timezone.utc)
        result = refresh_execution_calendar(self.broker, original, now)
        self.assertEqual(decide_session(result, now).kind, "extended")
        self.assertEqual(result.timeZoneId, "America/New_York")

    def test_expired_contract_does_not_retry_and_timezone_is_not_guessed(self):
        original = detail(False, expiry="20261118")
        self.assertIs(refresh_execution_calendar(self.broker, original, self.now), original)
        self.broker.ib.reqContractDetails.assert_not_called()
        self.assertIn("timeZoneId", decide_session(original, self.now).reason)


class QuoteRefreshTests(unittest.TestCase):
    def test_refresh_discards_old_book_stamps_and_requires_new_subscription(self):
        target = contract()
        ticker = NS(contract=target, bid=1.0, ask=1.1)
        broker = NS(ib=Mock(), _tickers={101: ticker}, _price_times={101: {1: 1.0, 2: 1.0}},
                    _display_times={101: {1: 1.0}}, _market_errors={101: (354, "denied")})
        pin_quote_contract(broker, target)
        self.assertTrue(reset_quote_stream(broker, target))
        self.assertNotIn(101, broker._tickers)
        self.assertNotIn(101, broker._price_times)
        self.assertNotIn(101, broker._display_times)
        broker.ib.cancelMktData.assert_called_once_with(target)
        broker.ib.reqMktData.assert_not_called()
        broker._tickers[101] = ticker
        self.assertFalse(reset_quote_stream(broker, target))
        self.assertIn(101, broker._priority_quote_ids)

    def test_actual_ib_async_wrapper_does_not_reuse_cancelled_book_or_old_request(self):
        from ib_async import IB, Option
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            ib = IB()
            target = Option("XSP", "20990101", 740, "C", "SMART", conId=101)
            old = ib.wrapper.startTicker(901, target, "mktData")
            ib.wrapper.priceSizeTick(901, 1, 1.0, 2)
            ib.wrapper.priceSizeTick(901, 2, 1.1, 2)
            broker = NS(ib=ib, _tickers={101: old}, _price_times={101: {1: 1, 2: 1}})
            # Exercise the real cancel bookkeeping, but replace the only
            # network-writing method with a mock. No socket is opened.
            ib.client.cancelMktData = Mock()
            self.assertTrue(reset_quote_stream(broker, target))
            ib.client.cancelMktData.assert_called_once_with(901)
            self.assertNotIn(901, ib.wrapper.reqId2Ticker)
            fresh = ib.wrapper.startTicker(902, target, "mktData")
            self.assertIsNot(fresh, old)
            self.assertEqual(broker._price_times, {})
            ib.wrapper.priceSizeTick(902, 1, 1.0, 2)
            ib.wrapper.priceSizeTick(902, 2, 1.1, 2)
            self.assertEqual((fresh.bid, fresh.ask), (1.0, 1.1))
            self.assertFalse(ib.isConnected())
        finally:
            loop.close()
            asyncio.set_event_loop(None)


class ViewportMarket(MarketDataMixin):
    QUOTE_TTL = 10

    def __init__(self):
        self.ib, self.mode = Mock(), "PAPER"
        self._tickers, self._price_times, self._underlyings = {}, {}, {}
        self._underlyings["XSP"] = NS(conId=10000, symbol="XSP", secType="IND", currency="USD")
        self._require = Mock()
        self._choose_class = Mock(return_value="XSP")
        self.trading_classes = Mock(return_value=["XSP"])
        self._display_spot = Mock(return_value=(200.4, "实时中间价"))
        self._contracts = lambda symbol, expiry, right, cls: [
            detail(conid=strike * 2 + (right == "P"), strike=strike, right=right, expiry=expiry)
            for strike in range(100, 300)]

    def _subscribe(self, target):
        if target.conId not in self._tickers:
            self._tickers[target.conId] = NS(contract=target, bid=1.0, ask=1.1, last=1.05,
                                           close=1.0, marketDataType=1, halted=0)
            self._price_times[target.conId] = {1: time.monotonic(), 2: time.monotonic()}
        return self._tickers[target.conId]


class ViewportChainTests(unittest.TestCase):
    def test_all_and_custom_display_rows_do_not_increase_quote_budget(self):
        market = ViewportMarket()
        for limit, expected in ((0, 200), (125, 125), (1, 1)):
            result = market.chain("XSP", "20990101", limit=limit)
            self.assertEqual(len(result["rows"]), expected)
            self.assertLessEqual(len(market._tickers), 80)
            self.assertLessEqual(len(result["watched_strikes"]), 40)
            for row in result["rows"]:
                for right, name in (("C", "call"), ("P", "put")):
                    leg = row[name]
                    self.assertEqual((leg["right"], leg["strike"], leg["expiry"]),
                                     (right, row["strike"], "20990101"))
                    self.assertIsInstance(leg["conid"], int)
            if limit == 0:
                self.assertFalse(result["has_prev"] or result["has_next"])
                self.assertIn("未订阅", result["rows"][0]["call"]["quote_type"])
        market.ib.placeOrder.assert_not_called()

    def test_scroll_releases_old_rows_but_preserves_preview_pin_then_releases_it(self):
        market = ViewportMarket()
        initial = market.chain("XSP", "20990101", limit=0)
        old_strike = initial["watched_strikes"][0]
        pinned_id = int(old_strike * 2)
        pin_quote_contract(market, market._tickers[pinned_id].contract)
        result = market.chain("XSP", "20990101", limit=0, visible_strikes=list(range(100, 140)))
        self.assertEqual(result["watched_strikes"], list(range(100, 140)))
        self.assertIn(pinned_id, market._tickers)
        self.assertEqual(len(market._tickers), 81)
        market._priority_quote_ids[pinned_id] = 0
        market.chain("XSP", "20990101", limit=0, visible_strikes=list(range(100, 140)))
        self.assertNotIn(pinned_id, market._tickers)
        self.assertEqual(len(market._tickers), 80)

    def test_all_rows_cannot_turn_arbitrary_viewport_into_unbounded_subscriptions(self):
        market = ViewportMarket()
        result = market.chain("XSP", "20990101", limit=0, visible_strikes=list(range(100, 300)))
        self.assertEqual(len(result["rows"]), 200)
        self.assertEqual(len(market._tickers), 80)
        self.assertEqual(len(result["watched_strikes"]), 40)

    def test_all_pagination_has_no_offset_and_custom_125_rows_remain_centered(self):
        all_rows = chain_page(range(100, 300), 200, offset=500, limit=0)
        self.assertEqual((len(all_rows[0]), *all_rows[1:]), (200, 0, False, False))
        rows, offset, previous, following = chain_page(range(100, 300), 200, limit=125)
        self.assertEqual(len(rows), 125)
        self.assertEqual(offset, 0)
        self.assertTrue(previous and following)


if __name__ == "__main__":
    unittest.main()
