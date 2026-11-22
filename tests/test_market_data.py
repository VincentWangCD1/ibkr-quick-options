"""Read-only option chain tests, using mock IB objects and synthetic demo only."""
from datetime import datetime, timedelta
import asyncio
from importlib.util import find_spec
from types import SimpleNamespace as NS
import time
import unittest
from unittest.mock import Mock
from zoneinfo import ZoneInfo

from core import ValidationError
from demo_broker import DemoBroker
from market_data import MarketDataMixin, MarketDataError, chain_page, _same_contract_id


class ChainPageTests(unittest.TestCase):
    def test_forty_strikes_center_on_reference_boundary_not_full_chain_middle(self):
        page, offset, previous, following = chain_page(range(6500, 8500, 5), 7520.4)
        self.assertEqual(len(page), 40)
        self.assertEqual(sum(strike < 7520.4 for strike in page), 20)
        self.assertEqual(sum(strike >= 7520.4 for strike in page), 20)
        self.assertEqual(offset, 0)
        self.assertTrue(previous and following)

    def test_contract_id_string_integer_equivalence_is_strict(self):
        self.assertTrue(_same_contract_id("100", 100))
        self.assertTrue(_same_contract_id(100, "100"))
        for value in ("101", "100.0", 100.0, True, False, None, "", "-100", "0", 0, "nan"):
            with self.subTest(value=value):
                self.assertFalse(_same_contract_id(value, 100))
        self.assertFalse(_same_contract_id(None, None))

    def test_atm_window_and_pages_are_bounded(self):
        values = list(range(70, 131))
        first, offset, previous, following = chain_page(values, 100.4, 0, 12)
        self.assertIn(100, first)
        self.assertEqual((len(first), offset, previous, following), (12, 0, True, True))
        page, offset, previous, following = chain_page(values, 100.4, 1000, 12)
        self.assertEqual(page[-1], 130)
        self.assertFalse(following)
        self.assertLess(offset, 1000)
        page, _, previous, _ = chain_page(values, None, -1000, 12)
        self.assertEqual(page, [])
        self.assertFalse(previous)

    def test_page_rejects_invalid_display_count(self):
        for limit in (-1, 10001, 1.5, True, float('nan'), None):
            with self.subTest(limit=limit), self.assertRaises(MarketDataError):
                chain_page([100], 100, 0, limit)


class DemoChainTests(unittest.TestCase):
    def setUp(self):
        self.broker = DemoBroker()
        self.broker.connect()

    def test_presets_and_custom_have_both_sides_and_explicit_demo_labels(self):
        for symbol in ("SPX", "XSP", "SPY", "QQQ", "aapl"):
            with self.subTest(symbol=symbol):
                expiry = self.broker.expirations(symbol)[0]
                chain = self.broker.chain(symbol, expiry)
                self.assertEqual(chain["symbol"], symbol.upper())
                self.assertEqual(len(chain["rows"]), 40)
                self.assertEqual(chain["mode"], "DEMO")
                for row in chain["rows"]:
                    self.assertLessEqual(row["call"]["bid"], row["call"]["ask"])
                    self.assertLessEqual(row["put"]["bid"], row["put"]["ask"])
                    self.assertIn("演示", row["call"]["quote_type"])
                self.assertEqual(self.broker.orders(), [])

    def test_spx_contract_class_is_pinned_and_distinct(self):
        expiry = self.broker.expirations("SPX")[0]
        settings = dict(symbol="SPX", expiry=expiry, right="C", moneyness="OTM", depth=4, percent=50)
        default = self.broker.preview(settings)
        self.assertEqual(default["contract"]["trading_class"], "SPXW")
        regular = self.broker.preview({**settings, "trading_class": "SPX"})
        self.assertNotEqual(default["contract"]["conid"], regular["contract"]["conid"])
        chain = self.broker.chain("SPX", expiry, "SPX")
        row = next(row for row in chain["rows"] if row["strike"] == regular["contract"]["strike"])
        self.assertEqual(row["call"]["conid"], regular["contract"]["conid"])

    def test_funds_snapshot_distinguishes_cash_and_net_assets(self):
        expiry = self.broker.expirations("QQQ")[0]
        preview = self.broker.preview(dict(symbol="QQQ", expiry=expiry, right="C", moneyness="OTM", depth=4, percent=50))
        self.broker.buy(preview["token"])
        funds = self.broker.snapshot()["financial"]
        self.assertGreater(funds["net_liquidation"], funds["usd_cash"])
        self.assertEqual(funds["usd_cash"], funds["cash"])
        self.assertIn("演示", funds["source"])

    def test_close_all_can_be_limited_to_current_symbol(self):
        for symbol in ("SPY", "QQQ"):
            expiry = self.broker.expirations(symbol)[0]
            preview = self.broker.preview(dict(symbol=symbol, expiry=expiry, right="C", moneyness="OTM", depth=4, percent=25))
            self.broker.buy(preview["token"])
        self.broker.close_all(symbol="SPY")
        self.assertEqual({p["symbol"] for p in self.broker.positions()}, {"QQQ"})

    def test_disconnected_demo_snapshot_does_not_leak_old_money_or_positions(self):
        self.broker.disconnect()
        snapshot = self.broker.snapshot()
        self.assertIsNone(snapshot["cash"])
        self.assertIsNone(snapshot["financial"]["net_liquidation"])
        self.assertIsNone(snapshot["financial"]["usd_cash"])
        self.assertFalse(snapshot["financial"]["ready"])
        self.assertEqual(snapshot["positions"], [])
        self.assertEqual(snapshot["orders"], [])

    def test_invalid_symbol_and_wrong_class_are_rejected(self):
        with self.assertRaises(ValidationError):
            self.broker.expirations("/bad;")
        expiry = self.broker.expirations("SPY")[0]
        with self.assertRaises(ValidationError):
            self.broker.chain("SPY", expiry, "SPXW")


class FakeMarket(MarketDataMixin):
    QUOTE_TTL = 10

    def __init__(self):
        self.ib = Mock()
        self.mode = "PAPER"
        self._underlyings, self._chains, self._chain_details, self._details = {}, {}, {}, {}
        self._tickers, self._price_times = {}, {}
        self._watched_symbols = set()
        self._require = Mock()
        self._quote = Mock(side_effect=AssertionError("Display must not execute the trade quote path"))

    def _subscribe(self, contract):
        if contract.conId not in self._tickers:
            self._tickers[contract.conId] = NS(contract=contract, bid=.2, ask=.23, last=.22,
                                              close=.21, marketDataType=1, halted=0)
            self._price_times[contract.conId] = {1: time.monotonic(), 2: time.monotonic(), 4: time.monotonic()}
        return self._tickers[contract.conId]


@unittest.skipUnless(find_spec("ib_async"), "Install requirements.txt to test mock contract types")
class MarketMixinTests(unittest.TestCase):
    def test_missing_reference_returns_empty_instead_of_arbitrary_strikes(self):
        self.make_chains()
        self.market._display_spot = Mock(return_value=(None, "等待标的行情"))
        self.market._contracts = Mock()
        result = self.market.chain("SPX", self.expiry)
        self.assertTrue(result["awaiting_reference"])
        self.assertEqual(result["rows"], [])
        self.market._contracts.assert_not_called()

    def test_manual_fallback_centers_chain_and_is_labelled_display_only(self):
        self.make_chains()
        self.market._display_spot = Mock(return_value=(None, "等待标的行情"))
        self.market._contracts = Mock(side_effect=lambda symbol, expiry, right, cls: [
            self.detail(strike, right=right) for strike in range(7000, 8000, 5)])
        result = self.market.chain("SPX", self.expiry, reference_price=7520.4)
        self.assertEqual(result["spot"], 7520.4)
        self.assertEqual(result["spot_ref"]["source"], "MANUAL")
        self.assertIn("仅供看链", result["spot_kind"])
        self.assertEqual(sum(row["strike"] < 7520.4 for row in result["rows"]), 20)
        self.market._quote.assert_not_called()
        self.market.ib.placeOrder.assert_not_called()

    def setUp(self):
        self.market = FakeMarket()
        self.expiry = (datetime.now(ZoneInfo("America/New_York")) + timedelta(days=1)).strftime("%Y%m%d")

    def make_underlying(self, symbol="SPX", conid=100):
        underlying = NS(symbol=symbol, secType="IND" if symbol in ("SPX", "XSP") else "STK",
                        conId=conid, currency="USD")
        self.market._underlyings[symbol] = underlying
        return underlying

    def make_chains(self, symbol="SPX", classes=("SPX", "SPXW")):
        underlying = self.make_underlying(symbol)
        self.market.ib.reqSecDefOptParams.return_value = [
            NS(multiplier="100", tradingClass=name, underlyingConId=underlying.conId, expirations={self.expiry})
            for name in classes]

    def detail(self, strike, right="C", trading_class="SPXW", conid=None, **overrides):
        contract = NS(symbol="SPX", secType="OPT", currency="USD", multiplier="100", strike=float(strike),
                      tradingClass=trading_class, right=right, conId=conid or int(strike) * 2 + (right == "P"),
                      lastTradeDateOrContractMonth=self.expiry, localSymbol=f"{trading_class}{strike}{right}", exchange="SMART")
        for name, value in overrides.items():
            setattr(contract, name, value)
        return NS(contract=contract, underConId=100, validExchanges="SMART,CBOE")

    def test_preset_underlying_routes_and_custom_stock(self):
        def qualify(contract):
            contract.conId = 100 + len(self.market._underlyings)
            return [contract]
        self.market.ib.qualifyContracts.side_effect = qualify
        for symbol in ("SPX", "XSP", "SPY", "QQQ", "aapl"):
            contract = self.market._underlying(symbol)
            if symbol in ("SPX", "XSP"):
                self.assertEqual((contract.secType, contract.exchange), ("IND", "CBOE"))
            else:
                self.assertEqual((contract.secType, contract.exchange, contract.currency), ("STK", "SMART", "USD"))
        self.assertIn("AAPL", self.market._watched_symbols)
        self.market.ib.connect.assert_not_called()
        self.market.ib.placeOrder.assert_not_called()

    def test_trading_class_default_and_explicit_class(self):
        self.make_chains()
        self.assertEqual(self.market._choose_class("SPX", self.expiry), "SPXW")
        self.assertEqual(self.market._choose_class("SPX", self.expiry, "SPX"), "SPX")
        with self.assertRaises(MarketDataError):
            self.market._choose_class("SPX", self.expiry, "SPY")

    def test_actual_ib_async_decoder_string_id_is_accepted(self):
        # Replay the real installed decoder and wrapper without a socket. Its
        # annotated-int underlyingConId is actually a str in ib_async 2.1.0.
        from ib_async import IB
        from ib_async.decoder import Decoder
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            ib = IB()
            req_id = 902
            ib.wrapper._results[req_id] = []
            decoder = Decoder(ib.wrapper, 178)
            for symbol, sec_type, trading_class in (("SPY", "STK", "SPY"),
                                                    ("XSP", "IND", "XSP"),
                                                    ("SPX", "IND", "SPXW")):
                with self.subTest(symbol=symbol):
                    decoder.securityDefinitionOptionParameter(
                        ["75", str(req_id), "SMART", "100", trading_class, "100",
                         "1", self.expiry, "2", "600", "605"])
                    decoded = ib.wrapper._results[req_id][-1]
                    self.assertEqual(str(decoded.underlyingConId), "100")
                    self.market._chains.clear()
                    self.make_underlying(symbol)
                    self.market.ib.reqSecDefOptParams.return_value = [decoded]
                    self.assertEqual(self.market.expirations(symbol), [self.expiry])
                    self.assertIn(trading_class, self.market.trading_classes(symbol, self.expiry))
            self.assertFalse(ib.isConnected())
        finally:
            loop.close()
            asyncio.set_event_loop(None)

    def test_chain_rejections_report_reason_and_never_accept_wrong_underlying(self):
        self.make_chains("SPY", ("SPY",))
        self.market.log = Mock()
        self.market.ib.reqSecDefOptParams.return_value = [
            NS(multiplier="100", tradingClass="SPY", underlyingConId="999", expirations={self.expiry}),
            NS(multiplier="10", tradingClass="SPY", underlyingConId="100", expirations={self.expiry})]
        with self.assertRaisesRegex(MarketDataError, "标的ID不匹配或无效"):
            self.market.expirations("SPY")
        message = self.market.log.call_args.args[0]
        self.assertIn("返回 2 条，匹配 0 条", message)
        self.assertIn("非100乘数 1", message)
        self.market.ib.placeOrder.assert_not_called()

    def test_contract_details_also_accept_string_underlying_id(self):
        self.make_chains()
        detail = self.detail(6000)
        detail.underConId = "100"
        self.market.ib.reqContractDetails.return_value = [detail]
        self.assertEqual(self.market._contracts("SPX", self.expiry, "C", "SPXW"), [detail])

    def test_unknown_or_ambiguous_custom_stock_is_rejected(self):
        self.market.ib.qualifyContracts.return_value = []
        with self.assertRaises(MarketDataError):
            self.market._underlying("NOTREAL")
        self.market.ib.qualifyContracts.return_value = [NS(conId=12), NS(conId=13)]
        with self.assertRaises(MarketDataError):
            self.market._underlying("ABC")
        self.assertEqual(self.market._watched_symbols, set())

    def test_canonical_broker_symbol_is_included_in_watched_holdings(self):
        canonical = NS(conId=88, symbol="BRK B", secType="STK", currency="USD")
        self.market.ib.qualifyContracts.return_value = [canonical]
        self.assertIs(self.market._underlying("BRK.B"), canonical)
        self.assertEqual(self.market._watched_symbols, {"BRK.B", "BRK B"})

    def test_ambiguous_custom_class_is_not_guessed(self):
        self.make_chains("ABC", ("ABC1", "ABC2"))
        with self.assertRaises(MarketDataError):
            self.market._choose_class("ABC", self.expiry)

    def test_contracts_validate_actual_expiry_class_underlying_and_currency(self):
        self.make_chains()
        correct = self.detail(6000)
        wrong_underlying = self.detail(6005)
        wrong_underlying.underConId = 999
        self.market.ib.reqContractDetails.return_value = [correct, wrong_underlying,
            self.detail(6010, trading_class="SPX"), self.detail(6015, right="P"),
            self.detail(6020, lastTradeDateOrContractMonth="19990101"),
            self.detail(6025, currency="EUR")]
        details = self.market._contracts("SPX", self.expiry, "C", "SPXW")
        self.assertEqual(details, [correct])
        self.assertEqual(self.market._contracts("SPX", self.expiry, "C", "SPXW"), [correct])
        self.market.ib.reqContractDetails.assert_called_once()

    def test_quote_state_labels_and_stale_data_not_called_live(self):
        detail = self.detail(6000)
        ticker = self.market._subscribe(detail.contract)
        self.assertEqual(self.market._display_leg(detail)["quote_type"], "实时订阅")
        self.market._price_times[detail.contract.conId][1] -= 11
        self.assertEqual(self.market._display_leg(detail)["quote_type"], "实时订阅·盘口久未更新")
        ticker.marketDataType = 3
        self.assertEqual(self.market._display_leg(detail)["quote_type"], "延迟")
        ticker.marketDataType, ticker.bid = 1, float("nan")
        self.assertEqual(self.market._display_leg(detail)["quote_type"], "等待盘口报价")
        self.assertIsNone(self.market._display_leg(detail)["bid"])

    def test_chain_page_cancels_old_streams_and_never_trades(self):
        self.make_chains()
        self.market._contracts = Mock(side_effect=lambda symbol, expiry, right, cls: [
            self.detail(strike, right=right) for strike in range(5900, 6110, 5)])
        underlying = self.market._underlyings["SPX"]
        ticker = self.market._subscribe(underlying)
        ticker.bid, ticker.ask, ticker.last, ticker.close = 6003, 6005, 6004, 5990
        initial = self.market.chain("SPX", self.expiry)
        self.assertEqual(len(initial["rows"]), 40)
        old_ids = {row["call"]["conid"] for row in initial["rows"]}
        later = self.market.chain("SPX", self.expiry, offset=12)
        new_ids = {row["call"]["conid"] for row in later["rows"]}
        self.assertTrue(old_ids - new_ids)
        self.market.ib.cancelMktData.assert_called()
        self.assertLessEqual(len(self.market._tickers), 81)
        self.market._quote.assert_not_called()
        self.market.ib.placeOrder.assert_not_called()

    def test_previous_close_is_labelled_display_only(self):
        underlying = self.make_underlying()
        ticker = self.market._subscribe(underlying)
        ticker.bid = ticker.ask = ticker.last = float("nan")
        ticker.close = 5990
        spot, kind = self.market._display_spot(underlying)
        self.assertEqual(spot, 5990)
        self.assertIn("前收盘价", kind)
        self.assertIn("仅供", kind)
        self.market._quote.assert_not_called()


if __name__ == "__main__":
    unittest.main()
