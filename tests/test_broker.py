"""Mock-only IB adapter checks. This module never connects to TWS."""
import asyncio
from datetime import date, datetime, timezone
from importlib.util import find_spec
from types import SimpleNamespace as NS
import time
import unittest
from unittest.mock import Mock, patch

from ib_broker import BrokerError, IBBroker
from account_data import financial_view


@unittest.skipUnless(find_spec("ib_async"), "Install requirements.txt for mock IB adapter checks")
class BrokerGuardTests(unittest.TestCase):
    def setUp(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.broker = IBBroker()
        self.broker.ib = Mock()
        self.broker.ib.isConnected.return_value = True
        self.broker.account = "DU123456"
        self.broker._cash = 1000
        self.values = [NS(account="DU123456", tag="Currency", currency="BASE", value="USD", modelCode="")]
        for tag, value in [("NetLiquidation", 1500), ("CashBalance", 1000), ("SettledCash", 800), ("AvailableFunds", 900), ("BuyingPower", 10000)]:
            self.values.append(NS(account="DU123456", tag=tag, currency="USD", value=str(value), modelCode=""))
        self.broker.ib.accountValues.side_effect = lambda *_: self.values
        self.broker._account_data = Mock()
        self.broker._account_data.refresh.side_effect = lambda account: financial_view(
            [(v.tag, v.value, v.currency) for v in self.values if v.account == account and not v.modelCode])
        self.broker.ib.positions.return_value = []
        self.broker.ib.reqPositions.return_value = []
        self.broker.ib.trades.return_value = []
        self.broker.ib.reqAllOpenOrders.return_value = []

    def tearDown(self):
        self.loop.close()
        asyncio.set_event_loop(None)

    def test_cash_is_minimum_of_three_cash_fields_not_buying_power(self):
        self.assertEqual(self.broker._sync_cash(force=True), 800)
        self.broker._account_data.refresh.assert_called_once_with("DU123456")
        self.broker.ib.accountValues.assert_not_called()

    def test_missing_settled_cash_blocks_sizing(self):
        self.values = [v for v in self.values if v.tag != "SettledCash"]
        with self.assertRaises(BrokerError):
            self.broker._sync_cash(force=True)

    def test_stale_cached_cash_cannot_fill_a_missing_download_field(self):
        stale = list(self.values)
        self.values = [v for v in self.values if v.tag != "SettledCash"]
        self.broker.ib.accountValues.side_effect = lambda *_: stale
        with self.assertRaises(BrokerError):
            self.broker._sync_cash(force=True)

    def test_non_usd_base_currency_blocks_sizing(self):
        for value in self.values:
            if value.tag in ("NetLiquidation", "AvailableFunds", "SettledCash"):
                value.currency = "EUR"
        with self.assertRaises(BrokerError):
            self.broker._sync_cash(force=True)

    def test_new_tws_ledger_cash_and_currency_keys_are_accepted(self):
        for value in self.values:
            if value.tag in {"CashBalance", "Currency"}:
                value.tag = "$LEDGER-" + value.tag
        self.assertEqual(self.broker._sync_cash(force=True), 800)

    def test_simultaneous_cash_formats_use_lower_value_not_sum(self):
        self.values.append(NS(account="DU123456", tag="$LEDGER-CashBalance",
                              currency="USD", value="300", modelCode=""))
        self.assertEqual(self.broker._sync_cash(force=True), 300)
        self.values[-1].value = "1200"
        next(value for value in self.values if value.tag == "CashBalance").value = "250"
        self.assertEqual(self.broker._sync_cash(force=True), 250)

    def test_invalid_prefixed_cash_is_not_hidden_by_valid_legacy_value(self):
        self.values.append(NS(account="DU123456", tag="$LEDGER-CashBalance",
                              currency="USD", value="NaN", modelCode=""))
        with self.assertRaises(BrokerError):
            self.broker._sync_cash(force=True)

    def test_base_or_eur_ledger_balance_cannot_substitute_for_usd(self):
        value = next(value for value in self.values if value.tag == "CashBalance")
        value.tag = "$LEDGER-CashBalance"
        for currency in ("BASE", "EUR"):
            with self.subTest(currency=currency):
                value.currency = currency
                with self.assertRaises(BrokerError):
                    self.broker._sync_cash(force=True)

    def test_other_account_or_model_ledger_cash_cannot_fill_missing_cash(self):
        value = next(value for value in self.values if value.tag == "CashBalance")
        value.tag = "$LEDGER-CashBalance"
        for account, model in (("DU999999", ""), ("DU123456", "other-model")):
            with self.subTest(account=account, model=model):
                value.account, value.modelCode = account, model
                with self.assertRaises(BrokerError):
                    self.broker._sync_cash(force=True)

    def test_optional_ledger_settled_cash_is_additional_cap(self):
        self.values.append(NS(account="DU123456", tag="$LEDGER-SettledCash",
                              currency="USD", value="600", modelCode=""))
        self.assertEqual(self.broker._sync_cash(force=True), 600)

    def test_ledger_fields_cannot_replace_aggregate_settled_or_available(self):
        for tag in ("SettledCash", "AvailableFunds"):
            with self.subTest(tag=tag):
                value = next(value for value in self.values if value.tag == tag)
                value.tag = "$LEDGER-" + tag
                with self.assertRaises(BrokerError):
                    self.broker._sync_cash(force=True)
                value.tag = tag

    def test_ledger_currency_base_is_not_account_base_currency(self):
        self.values.append(NS(account="DU123456", tag="$LEDGER-Currency",
                              currency="BASE", value="BASE", modelCode=""))
        self.assertEqual(self.broker._sync_cash(force=True), 800)
        self.assertEqual(self.broker._financial["base_currency"], "USD")

    def test_cached_prefixed_cash_cannot_fill_missing_current_download(self):
        cash = next(value for value in self.values if value.tag == "CashBalance")
        cash.tag = "$LEDGER-CashBalance"
        stale = list(self.values)
        self.values = [value for value in self.values if value is not cash]
        self.broker.ib.accountValues.side_effect = lambda *_: stale
        with self.assertRaises(BrokerError):
            self.broker._sync_cash(force=True)

    def test_error_unicode_escapes_decode_without_corrupting_chinese(self):
        self.broker.log = Mock()
        self.broker._on_error(-1, 2158, r"现有中文 Sec-def\u6570\u636e 正常")
        self.assertIn("现有中文 Sec-def数据 正常", self.broker.log.call_args.args[0])

    def test_2100_during_account_refresh_is_informational(self):
        self.broker.log = Mock()
        self.broker._account_sync_values = {}
        self.broker._tokens = {"valid-preview": object()}
        self.broker._on_error(-1, 2100, "API account subscription cancelled")
        self.assertIn("[2100]", self.broker.log.call_args.args[0])
        self.assertIn("重新同步资金", self.broker.log.call_args.args[0])
        self.assertFalse(self.broker._uncertain)
        self.assertIn("valid-preview", self.broker._tokens)
        self.broker.ib.disconnect.assert_not_called()

    def setup_mock_connection(self):
        state = {"connected": False}
        self.broker.account = ""
        self.broker.ib.isConnected.side_effect = lambda: state["connected"]
        self.broker.ib.connect.side_effect = lambda *args, **kwargs: state.update(connected=True)
        self.broker.ib.disconnect.side_effect = lambda: state.update(connected=False)
        self.broker.ib.managedAccounts.return_value = ["DU123456"]
        return state

    def test_connect_keeps_session_and_partial_funds_when_settled_missing(self):
        self.setup_mock_connection()
        self.values = [value for value in self.values if value.tag != "SettledCash"]
        snapshot = self.broker.connect(mode="PAPER", account="DU123456")
        self.assertTrue(snapshot["connected"])
        self.assertFalse(snapshot["financial"]["ready"])
        self.assertEqual(snapshot["financial"]["net_liquidation"], 1500)
        self.assertEqual(snapshot["financial"]["usd_cash"], 1000)
        self.assertIsNone(snapshot["cash"])
        self.broker.ib.disconnect.assert_not_called()
        self.broker.ib.placeOrder.assert_not_called()

    def test_funds_can_recover_without_disconnect_or_relogin(self):
        self.setup_mock_connection()
        settled = next(value for value in self.values if value.tag == "SettledCash")
        self.values.remove(settled)
        self.broker.connect(mode="PAPER", account="DU123456")
        self.values.append(settled)
        snapshot = self.broker.refresh_funds()
        self.assertTrue(snapshot["connected"])
        self.assertTrue(snapshot["financial"]["ready"])
        self.assertEqual(snapshot["cash"], 800)
        self.assertEqual(self.broker.ib.connect.call_count, 1)
        self.broker.ib.disconnect.assert_not_called()

    def test_dated_settlement_recovers_budget_without_assuming_margin(self):
        self.setup_mock_connection()
        rows = [(v.tag, v.value, v.currency) for v in self.values if v.tag != "SettledCash"]
        rows.append(("AccountType", "INDIVIDUAL", ""))
        self.broker._account_data.refresh.side_effect = lambda account: financial_view(
            rows, [("$LEDGER-SettledCashByDate", "20261111:146.01;20261112:1000.01", "USD")],
            today=date(2026, 11, 11))
        state = self.broker.connect(mode="PAPER", account="DU123456")
        self.assertTrue(state["financial"]["ready"])
        self.assertEqual(state["cash"], 146.01)
        self.assertEqual(state["financial"]["settled_cash"], 146.01)
        self.assertEqual(state["financial"]["account_type_raw"], "INDIVIDUAL")
        self.broker.ib.placeOrder.assert_not_called()

    def test_future_only_settlement_invalidates_previous_budget_and_preview(self):
        self.assertEqual(self.broker._sync_cash(force=True), 800)
        self.broker._tokens = {"previous": object()}
        rows = [(v.tag, v.value, v.currency) for v in self.values if v.tag != "SettledCash"]
        self.broker._account_data.refresh.side_effect = lambda account: financial_view(
            rows, [("SettledCashByDate", "20261112:1000", "USD")], today=date(2026, 11, 11))
        state = self.broker.refresh_funds()
        self.assertTrue(state["connected"])
        self.assertFalse(state["financial"]["ready"])
        self.assertIsNone(state["cash"])
        self.assertEqual(self.broker._tokens, {})
        self.broker.ib.placeOrder.assert_not_called()

    def test_failed_refresh_clears_prior_budget_and_preview_token(self):
        self.assertEqual(self.broker._sync_cash(force=True), 800)
        self.broker._tokens = {"old-preview": object()}
        self.values = [value for value in self.values if value.tag != "SettledCash"]
        snapshot = self.broker.refresh_funds()
        self.assertTrue(snapshot["connected"])
        self.assertIsNone(snapshot["cash"])
        self.assertEqual(self.broker._tokens, {})
        with self.assertRaises(BrokerError):
            self.broker._sync_cash()  # Cached error may not fall back to old cash.

    def test_disconnected_snapshot_cannot_expose_previous_cash(self):
        self.broker._sync_cash(force=True)
        self.broker.ib.isConnected.return_value = False
        snapshot = self.broker.snapshot()
        self.assertFalse(snapshot["connected"])
        self.assertIsNone(snapshot["financial"]["cash"])
        self.assertIsNone(snapshot["cash"])

    def test_uncertain_send_snapshot_disables_budget_but_preserves_display_values(self):
        self.broker._sync_cash(force=True)
        self.broker._uncertain = True
        snapshot = self.broker.snapshot()
        self.assertTrue(snapshot["connected"])
        self.assertTrue(snapshot["locked"])
        self.assertFalse(snapshot["financial"]["ready"])
        self.assertEqual(snapshot["financial"]["usd_cash"], 1000)
        self.assertIsNone(snapshot["cash"])

    def test_snapshot_returns_independent_financial_copy(self):
        self.broker._sync_cash(force=True)
        snapshot = self.broker.snapshot()
        snapshot["financial"]["usd_cash"] = 999999
        self.assertEqual(self.broker._financial["usd_cash"], 1000)

    @staticmethod
    def mock_position(symbol, conid, quantity=2, account="DU123456", sec_type="OPT"):
        return NS(account=account, position=quantity, avgCost=25,
                  contract=NS(symbol=symbol, conId=conid, secType=sec_type, currency="USD", multiplier="100"))

    def test_close_all_scopes_to_selected_symbol_and_eligible_signed_positions(self):
        self.broker.ib.reqPositions.return_value = [
            self.mock_position("SPY", 1), self.mock_position("SPY", 2),
            self.mock_position("QQQ", 3), self.mock_position("SPY", 4, quantity=-2),
            self.mock_position("SPY", 5, account="U999"), self.mock_position("SPY", 6, sec_type="STK"),
        ]
        self.broker.close = Mock(return_value={"status": "Submitted"})
        result = self.broker.close_all(symbol="spy")
        self.assertEqual(len(result), 3)
        self.assertEqual([call.args for call in self.broker.close.call_args_list], [(1, 100), (2, 100), (4, 100)])
        self.broker.ib.placeOrder.assert_not_called()

    def test_close_all_requires_explicit_symbol(self):
        self.broker.close = Mock()
        with self.assertRaises(BrokerError):
            self.broker.close_all()
        self.broker.close.assert_not_called()

    def test_custom_loaded_symbol_can_close_only_its_own_positions(self):
        self.broker._watched_symbols.add("AAPL")
        self.broker.ib.reqPositions.return_value = [self.mock_position("AAPL", 1), self.mock_position("QQQ", 2)]
        self.broker.close = Mock(return_value={"status": "Submitted"})
        self.broker.close_all(symbol="AAPL")
        self.broker.close.assert_called_once_with(1, 100)

    def set_reserve(self, status, reported_filled, fills=()):
        for fill in fills:
            if not hasattr(fill, "commissionReport"):
                fill.commissionReport = NS(execId="mock-exec", currency="USD", commission=0)
        self.broker._debit_reserve = 500
        self.broker._reserve_cash_baseline = 1000
        self.broker._reserve_trade = NS(
            isDone=lambda: status in {"Filled", "Cancelled", "ApiCancelled"},
            orderStatus=NS(status=status, filled=reported_filled),
            fills=list(fills), contract=NS(multiplier="100"),
        )

    def test_filled_status_before_execution_does_not_release_cash(self):
        self.set_reserve("Filled", 2)
        self.assertEqual(self.broker._sync_cash(force=True), 500)
        self.assertEqual(self.broker._debit_reserve, 500)

    def test_partially_filled_cancel_before_execution_does_not_release_cash(self):
        self.set_reserve("Cancelled", 1)
        self.assertEqual(self.broker._sync_cash(force=True), 500)
        self.assertEqual(self.broker._debit_reserve, 500)

    def test_complete_fills_and_reflected_premium_release_cash(self):
        fill = NS(execution=NS(shares=2, price=1))
        self.set_reserve("Filled", 2, [fill])
        self.assertEqual(self.broker._sync_cash(force=True), 800)
        self.assertEqual(self.broker._debit_reserve, 0)

    def test_cancel_with_no_fills_releases_reserve(self):
        self.set_reserve("Cancelled", 0)
        self.assertEqual(self.broker._sync_cash(force=True), 800)
        self.assertEqual(self.broker._debit_reserve, 0)

    def test_complete_executions_without_cash_debit_keep_reserve(self):
        for value in self.values:
            if value.tag in {"CashBalance", "SettledCash", "AvailableFunds"}:
                value.value = "1000"
        self.set_reserve("Filled", 2, [NS(execution=NS(shares=2, price=1))])
        self.assertEqual(self.broker._sync_cash(force=True), 500)
        self.assertEqual(self.broker._debit_reserve, 500)

    def test_missing_commission_report_keeps_buy_reserve(self):
        fill = NS(execution=NS(shares=2, price=1),
                  commissionReport=NS(execId="", currency="", commission=0))
        self.set_reserve("Filled", 2, [fill])
        self.assertEqual(self.broker._sync_cash(force=True), 500)
        self.assertEqual(self.broker._debit_reserve, 500)

    def test_close_filled_before_execution_callbacks_keeps_reservation(self):
        self.set_reserve("Filled", 2)
        self.broker._close_reservations[123] = (self.broker._reserve_trade, 10)
        with self.assertRaises(BrokerError):
            self.broker._check_close_reservation(123, 8)
        self.assertIn(123, self.broker._close_reservations)

    def test_close_execution_before_position_update_keeps_reservation(self):
        self.set_reserve("Filled", 2, [NS(execution=NS(shares=2, price=1))])
        self.broker._close_reservations[123] = (self.broker._reserve_trade, 10)
        with self.assertRaises(BrokerError):
            self.broker._check_close_reservation(123, 10)
        self.assertIn(123, self.broker._close_reservations)

    def test_close_with_complete_execution_and_position_update_releases(self):
        self.set_reserve("Filled", 2, [NS(execution=NS(shares=2, price=1))])
        self.broker._close_reservations[123] = (self.broker._reserve_trade, 10)
        self.broker._check_close_reservation(123, 8)
        self.assertNotIn(123, self.broker._close_reservations)

    def test_close_cancel_after_partial_fill_needs_complete_callbacks(self):
        self.set_reserve("Cancelled", 2, [NS(execution=NS(shares=1, price=1))])
        self.broker._close_reservations[123] = (self.broker._reserve_trade, 10)
        with self.assertRaises(BrokerError):
            self.broker._check_close_reservation(123, 8)

    def test_close_cancelled_without_fills_releases_reservation(self):
        self.set_reserve("Cancelled", 0)
        self.broker._close_reservations[123] = (self.broker._reserve_trade, 10)
        self.broker._check_close_reservation(123, 10)
        self.assertNotIn(123, self.broker._close_reservations)

    def quote_fixture(self):
        contract = NS(conId=123, localSymbol="SPY MOCK", symbol="SPY")
        ticker = NS(marketDataType=1, halted=0, bid=.2, ask=.23, last=.22, bidSize=10, askSize=10)
        self.broker._subscribe = Mock(return_value=ticker)
        self.broker._price_times = {123: {key: time.monotonic() for key in (1, 2, 4)}}
        return contract, ticker

    def test_fresh_real_bid_ask_is_usable(self):
        contract, _ = self.quote_fixture()
        self.assertEqual(self.broker._quote(contract, wait=0)["ask"], .23)

    def test_size_updates_refresh_only_observed_real_side_not_other_prices(self):
        from ib_async import Contract, Ticker
        contract = Contract(conId=123, symbol="SPY", localSymbol="MOCK")
        ticker = Ticker(contract=contract, bid=.2, ask=.23, bidSize=10, askSize=10, marketDataType=1, halted=0)
        ticker.bid, ticker.ask, ticker.bidSize, ticker.askSize = .2, .23, 10, 10
        self.broker.ib.reqMktData.return_value = ticker
        self.broker._subscribe(contract)
        ticker.ticks = [NS(tickType=1, price=.2, size=10), NS(tickType=2, price=.23, size=10)]
        ticker.updateEvent.emit(ticker)
        self.broker._price_times[123][1] -= 130
        self.broker._price_times[123][2] -= 130
        old_ask = self.broker._price_times[123][2]
        ticker.ticks = [NS(tickType=0, price=.2, size=11)]
        ticker.updateEvent.emit(ticker)
        self.assertLess(time.monotonic() - self.broker._price_times[123][1], 1)
        self.assertEqual(self.broker._price_times[123][2], old_ask)
        with self.assertRaises(BrokerError):
            self.broker._quote(contract, wait=0)
        ticker.ticks = [NS(tickType=3, price=.23, size=12)]
        ticker.updateEvent.emit(ticker)
        self.assertEqual(self.broker._quote(contract, wait=0)["ask"], .23)

    def test_delayed_and_cancelled_stream_events_cannot_make_quote_fresh(self):
        from ib_async import Contract, Ticker
        contract = Contract(conId=123, symbol="SPY", localSymbol="MOCK")
        ticker = Ticker(contract=contract, bid=.2, ask=.23, bidSize=10, askSize=10, marketDataType=3, halted=0)
        ticker.bid, ticker.ask, ticker.bidSize, ticker.askSize = .2, .23, 10, 10
        self.broker.ib.reqMktData.return_value = ticker
        self.broker._subscribe(contract)
        ticker.ticks = [NS(tickType=66, price=.2, size=10), NS(tickType=67, price=.23, size=10)]
        ticker.updateEvent.emit(ticker)
        self.assertEqual(self.broker._price_times[123], {})
        self.assertIn(1, self.broker._display_times[123])
        with self.assertRaises(BrokerError):
            self.broker._quote(contract, wait=0)
        self.broker._tickers.pop(123)
        self.broker._price_times.pop(123)
        ticker.ticks = [NS(tickType=1, price=.2, size=10)]
        ticker.updateEvent.emit(ticker)
        self.assertNotIn(123, self.broker._price_times)

    def test_delayed_data_cannot_submit_even_with_fresh_timestamps(self):
        contract, ticker = self.quote_fixture()
        ticker.marketDataType = 3
        with self.assertRaises(BrokerError):
            self.broker._quote(contract, wait=0)

    def test_stale_bid_is_not_refreshed_by_fresh_last(self):
        contract, _ = self.quote_fixture()
        self.broker._price_times[123][1] -= 11
        with self.assertRaises(BrokerError):
            self.broker._quote(contract, wait=0)

    def test_crossed_quote_and_halted_quote_are_rejected(self):
        contract, ticker = self.quote_fixture()
        ticker.bid = .3
        with self.assertRaises(BrokerError):
            self.broker._quote(contract, wait=0)
        ticker.bid, ticker.halted = .2, 1
        with self.assertRaises(BrokerError):
            self.broker._quote(contract, wait=0)

    def test_single_tws_account_connects_without_typing_credentials(self):
        for mode, account in (("PAPER", "DU123456"), ("LIVE", "U123456")):
            with self.subTest(mode=mode):
                self.setup_mock_connection()
                self.broker.ib.managedAccounts.return_value = [account]
                snapshot = self.broker.connect(mode=mode)
                self.assertTrue(snapshot["connected"])
                self.assertEqual(snapshot["account"], account)
                self.broker._account_data.refresh.assert_called_with(account)

    def test_multiple_tws_accounts_require_explicit_choice(self):
        self.setup_mock_connection()
        self.broker.ib.managedAccounts.return_value = ["U101", "U202", "DU303"]
        snapshot = self.broker.connect(mode="LIVE")
        self.assertFalse(snapshot["connected"])
        self.assertEqual(snapshot["account_choices"], ("U101", "U202"))
        self.broker._account_data.refresh.assert_not_called()
        selected = self.broker.connect(mode="LIVE", account="U202")
        self.assertTrue(selected["connected"])
        self.assertEqual(selected["account"], "U202")
        self.broker.ib.placeOrder.assert_not_called()

    def test_wrong_mode_missing_or_changed_account_ends_connection(self):
        for mode, accounts, requested in (
            ("LIVE", ["DU123"], ""), ("PAPER", ["U123"], ""),
            ("LIVE", [], ""), ("LIVE", ["U202"], "U101"),
            ("LIVE", ["DU101", "DU202"], ""),
        ):
            with self.subTest(mode=mode, accounts=accounts, requested=requested):
                state = self.setup_mock_connection()
                self.broker.ib.managedAccounts.return_value = accounts
                with self.assertRaises(BrokerError):
                    self.broker.connect(mode=mode, account=requested)
                self.assertFalse(state["connected"])
                self.assertFalse(self.broker.account)
                self.broker.ib.placeOrder.assert_not_called()

    def test_real_account_in_paper_mode_is_rejected_before_connect(self):
        with self.assertRaises(BrokerError):
            self.broker.connect(mode="PAPER", account="U123")
        self.broker.ib.connect.assert_not_called()

    def test_client_zero_is_not_allowed_to_bind_manual_orders(self):
        with self.assertRaises(BrokerError):
            self.broker.connect(client_id=0)
        self.broker.ib.connect.assert_not_called()

    def test_liquid_hours_enforce_early_close(self):
        detail = NS(timeZoneId="US/Eastern", tradingHours="20261127:0930-20261127:1300",
                    liquidHours="20261127:0930-20261127:1300")
        with patch("ib_broker.datetime") as clock:
            clock.now.return_value = datetime(2026, 11, 27, 17, 59, tzinfo=timezone.utc)
            self.broker._session(detail)
            clock.now.return_value = datetime(2026, 11, 27, 18, 0, tzinfo=timezone.utc)
            with self.assertRaises(BrokerError):
                self.broker._session(detail)

    def test_local_pending_order_cannot_be_lost_from_open_order_snapshot(self):
        trade = NS(order=NS(account="DU123456", clientId=31, orderId=8, orderRef="QOPT1:test"),
                   isDone=lambda: False)
        self.broker.ib.reqAllOpenOrders.return_value = []
        self.broker.ib.trades.return_value = [trade]
        self.assertEqual(self.broker._open_orders(), [trade])

    def send_fixture(self):
        events = []
        contract = NS(conId=123, multiplier="100", localSymbol="SPY MOCK", symbol="SPY", exchange="SMART")
        detail = NS(contract=contract, minTick=.01, marketRuleIds="", validExchanges="SMART")
        self.broker._session = Mock(return_value=NS(kind="regular", order_type="MKT", outside_rth=False))
        self.broker._open_orders = Mock(return_value=[])
        self.broker._quote = Mock(return_value={"bid": .22, "ask": .23})
        state = NS(warningText="", commission=0, commissionCurrency="USD")
        self.broker.ib.whatIfOrder.side_effect = lambda *_: (events.append("whatif") or state)
        def refresh_cash(**_):
            events.append("cash")
            self.broker._cash = self.broker._raw_cash = 200
            return 200
        self.broker._sync_cash = Mock(side_effect=refresh_cash)
        def place_mock(c, order):
            events.append("send")
            order.orderId, order.clientId = 99, 31
            return NS(contract=c, order=order, fills=[], isDone=lambda: False,
                      orderStatus=NS(status="Submitted", filled=0, remaining=order.totalQuantity, avgFillPrice=0))
        self.broker.ib.placeOrder.side_effect = place_mock
        settings = dict(percent=50, slippage_pct=0, commission_per_contract=0, min_commission=0, max_contracts=100)
        return detail, settings, state, events

    def test_send_rechecks_cash_after_whatif_and_builds_account_bound_market_order(self):
        detail, settings, _, events = self.send_fixture()
        result = self.broker._send(detail, "BUY", 21, settings=settings, cash=1000, budget=500)
        self.assertEqual(events, ["whatif", "cash", "send"])
        self.assertEqual(result["quantity"], 4)  # 50% of refreshed $200 / $23 per contract.
        self.broker.ib.placeOrder.assert_called_once()
        order = self.broker.ib.placeOrder.call_args.args[1]
        self.assertEqual((order.account, order.action, order.orderType, order.tif), ("DU123456", "BUY", "MKT", "DAY"))
        self.assertFalse(order.outsideRth)
        self.assertEqual(self.broker._debit_reserve, 92)
        self.broker.ib.connect.assert_not_called()

    def test_whatif_warning_blocks_send(self):
        detail, settings, state, _ = self.send_fixture()
        state.warningText = "Mock credit check warning"
        with self.assertRaises(BrokerError):
            self.broker._send(detail, "BUY", 21, settings=settings, cash=1000, budget=500)
        self.broker.ib.placeOrder.assert_not_called()

    def test_final_price_increase_aborts_without_send(self):
        detail, settings, _, _ = self.send_fixture()
        self.broker._quote.side_effect = [{"ask": .23}, {"ask": .23}, {"ask": .24}]
        with self.assertRaises(BrokerError):
            self.broker._send(detail, "BUY", 21, settings=settings, cash=1000, budget=500)
        self.broker.ib.placeOrder.assert_not_called()

    def test_send_timeout_locks_without_automatic_retry(self):
        detail, settings, _, _ = self.send_fixture()
        self.broker.ib.placeOrder.side_effect = TimeoutError("mock socket timeout")
        with self.assertRaises(BrokerError):
            self.broker._send(detail, "BUY", 21, settings=settings, cash=1000, budget=500)
        self.assertTrue(self.broker._uncertain)
        self.broker.ib.placeOrder.assert_called_once()

    def test_sell_rechecks_position_after_whatif(self):
        detail, _, _, _ = self.send_fixture()
        self.broker._position_records = Mock(return_value=[NS(contract=detail.contract, position=1)])
        with self.assertRaises(BrokerError):
            self.broker._send(detail, "SELL", 2)
        self.broker.ib.placeOrder.assert_not_called()

    def test_extended_buy_caps_premium_at_limit_and_sets_outside_rth(self):
        detail, settings, _, _ = self.send_fixture()
        self.broker._session.return_value = NS(kind="extended", order_type="LMT", outside_rth=True)
        settings["slippage_pct"] = 5
        result = self.broker._send(detail, "BUY", 30, settings=settings, budget=100)
        order = self.broker.ib.placeOrder.call_args.args[1]
        self.assertEqual((order.orderType, order.lmtPrice, order.totalQuantity), ("LMT", .25, 4))
        self.assertTrue(order.outsideRth)
        self.assertEqual(self.broker._debit_reserve, 100)
        self.assertEqual(result["limit_price"], .25)

    def test_extended_sell_is_protected_limit_with_fresh_book(self):
        detail, _, _, _ = self.send_fixture()
        self.broker._session.return_value = NS(kind="extended", order_type="LMT", outside_rth=True)
        self.broker._position_records = Mock(return_value=[NS(contract=detail.contract, position=5)])
        self.broker._send(detail, "SELL", 2)
        order = self.broker.ib.placeOrder.call_args.args[1]
        self.assertEqual((order.action, order.orderType, order.lmtPrice), ("SELL", "LMT", .20))
        self.assertTrue(order.outsideRth)
        self.assertIn(123, self.broker._close_reservations)

    def test_extended_price_cannot_exceed_displayed_limit(self):
        detail, settings, _, _ = self.send_fixture()
        self.broker._session.return_value = NS(kind="extended", order_type="LMT", outside_rth=True)
        plan = dict(order_type="LMT", limit_price=.20, outside_rth=True, route="SMART", session_label="延长")
        with self.assertRaisesRegex(BrokerError, "超过预览限价"):
            self.broker._send(detail, "BUY", 2, settings=settings, budget=100, plan=plan)
        self.broker.ib.placeOrder.assert_not_called()

    def test_session_boundary_during_whatif_aborts(self):
        detail, settings, state, _ = self.send_fixture()
        def whatif(*_):
            self.broker._session.return_value = NS(kind="extended", order_type="LMT", outside_rth=True)
            return state
        self.broker.ib.whatIfOrder.side_effect = whatif
        with self.assertRaisesRegex(BrokerError, "时段改变"):
            self.broker._send(detail, "BUY", 2, settings=settings, budget=100)
        self.broker.ib.placeOrder.assert_not_called()

    def test_routed_option_calendar_can_enable_verified_cboe_extended_session(self):
        from ib_async import Option, ContractDetails
        contract = Option("SPY", "20261111", 750, "C", "SMART", currency="USD", conId=123)
        base = ContractDetails(contract=contract, timeZoneId="US/Eastern", validExchanges="SMART,CBOE",
                               tradingHours="20261111:0930-20261111:1615",
                               liquidHours="20261111:0930-20261111:1615")
        import copy
        routed = copy.deepcopy(base)
        routed.tradingHours = "20261111:0730-20261111:0925,20261111:0930-20261111:1615"
        self.broker.ib.reqContractDetails.return_value = [routed]
        with patch("ib_broker.datetime") as clock:
            clock.now.return_value = datetime(2026, 11, 11, 13, 0, tzinfo=timezone.utc)
            result = self.broker._execution_detail(base)
        self.assertEqual(result.contract.exchange, "CBOE")
        self.assertEqual(result.contract.conId, 123)
        self.assertEqual(base.contract.exchange, "SMART")
        self.broker.ib.placeOrder.assert_not_called()

    def test_market_rules_select_correct_route_and_price_tier(self):
        detail, _, _, _ = self.send_fixture()
        detail.validExchanges, detail.marketRuleIds = "SMART,CBOE", "26,27"
        detail.contract.exchange = "CBOE"
        self.broker.ib.reqMarketRule.return_value = [NS(lowEdge=0, increment=.05), NS(lowEdge=3, increment=.10)]
        self.broker._session.return_value = NS(kind="extended", order_type="LMT", outside_rth=True)
        plan = self.broker._execution_plan(detail, "BUY", {"ask": 2.95}, {"slippage_pct": 5})
        self.assertEqual(plan["limit_price"], 3.1)
        self.broker.ib.reqMarketRule.assert_called_once_with(27)

    def test_only_verified_fresh_proxy_is_accepted_for_index_selection(self):
        self.broker._underlying = Mock(return_value=NS(symbol="SPX"))
        self.broker._quote = Mock(side_effect=BrokerError("no index"))
        def display(_):
            self.broker._spot_reference_meta = self.ref
        self.broker._display_spot = display
        self.ref = dict(spot=7500, kind="SPY 估算", source="SPY_RATIO_ESTIMATE", anchor_verified=True,
                        estimated=True, quote_age=1, quote_checked_at=time.monotonic())
        self.assertEqual(self.broker._selection_spot("SPX"), (7500, "SPY 估算", True))
        for change in ({"quote_age": 11}, {"anchor_verified": False}, {"source": "MANUAL"}, {"estimated": False}):
            original = self.ref.copy()
            self.ref.update(change)
            with self.assertRaises(BrokerError):
                self.broker._selection_spot("SPX")
            self.ref = original

    def test_user_take_profit_limit_may_rest_above_bid_and_preserves_gtc(self):
        detail, _, _, _ = self.send_fixture()
        self.broker._position_records = Mock(return_value=[NS(contract=detail.contract, position=5)])
        result = self.broker._send(detail, "SELL", 3, exit_spec={"type": "LMT", "limit_price": .5, "tif": "GTC"})
        order = self.broker.ib.placeOrder.call_args.args[1]
        self.assertEqual((order.action, order.orderType, order.lmtPrice, order.tif), ("SELL", "LMT", .5, "GTC"))
        self.assertTrue(order.outsideRth)
        self.assertEqual(result["remaining"], 3)
        self.assertIn(123, self.broker._close_reservations)

    def test_stop_limit_retains_user_trigger_and_limit(self):
        detail, _, _, _ = self.send_fixture()
        self.broker._position_records = Mock(return_value=[NS(contract=detail.contract, position=5)])
        result = self.broker._send(detail, "SELL", 2,
                                  exit_spec={"type": "STP LMT", "stop_price": .2, "limit_price": .15, "tif": "DAY"})
        order = self.broker.ib.placeOrder.call_args.args[1]
        self.assertEqual((order.orderType, order.auxPrice, order.lmtPrice), ("STP LMT", .2, .15))
        self.assertTrue(order.outsideRth)
        self.assertEqual(result["stop_price"], .2)
        self.assertEqual(result["limit_price"], .15)

    def test_trailing_exit_is_broker_order_not_local_trigger(self):
        detail, _, _, _ = self.send_fixture()
        self.broker._position_records = Mock(return_value=[NS(contract=detail.contract, position=5)])
        result = self.broker._send(detail, "SELL", 2,
                                  exit_spec={"type": "TRAIL", "trail_mode": "percent", "trail_value": 10, "tif": "GTC"})
        order = self.broker.ib.placeOrder.call_args.args[1]
        self.assertEqual((order.orderType, order.trailingPercent, order.tif), ("TRAIL", 10, "GTC"))
        self.assertFalse(order.outsideRth)
        self.assertEqual((result["trail_mode"], result["trail_value"]), ("percent", 10))
        self.broker.ib.placeOrder.assert_called_once()

    def test_extended_conditional_sell_never_transmits(self):
        detail, _, _, _ = self.send_fixture()
        self.broker._session.return_value = NS(kind="extended", order_type="LMT", outside_rth=True)
        with self.assertRaisesRegex(BrokerError, "仅在合约常规时段"):
            self.broker._send(detail, "SELL", 2, exit_spec={"type": "STP", "stop_price": .2, "tif": "DAY"})
        self.broker.ib.whatIfOrder.assert_not_called()
        self.broker.ib.placeOrder.assert_not_called()

    def test_extended_explicit_limit_uses_user_price_without_slippage(self):
        detail, _, _, _ = self.send_fixture()
        self.broker._session.return_value = NS(kind="extended", order_type="LMT", outside_rth=True)
        self.broker._position_records = Mock(return_value=[NS(contract=detail.contract, position=5)])
        self.broker._send(detail, "SELL", 2, exit_spec={"type": "LMT", "limit_price": .4, "tif": "DAY"})
        order = self.broker.ib.placeOrder.call_args.args[1]
        self.assertEqual(order.lmtPrice, .4)
        self.assertTrue(order.outsideRth)

    def test_extended_limit_conditional_orders_request_broker_managed_outside_rth(self):
        for spec in ({"type": "STP LMT", "stop_price": .2, "limit_price": .15, "tif": "DAY"},
                     {"type": "TRAIL LIMIT", "stop_price": .2, "trail_mode": "percent", "trail_value": 10,
                      "limit_offset": .05, "tif": "GTC"}):
            with self.subTest(kind=spec["type"]):
                self.broker._close_reservations.clear()
                detail, _, _, _ = self.send_fixture()
                self.broker._session.return_value = NS(kind="extended", order_type="LMT", outside_rth=True)
                self.broker._position_records = Mock(return_value=[NS(contract=detail.contract, position=5)])
                self.broker._send(detail, "SELL", 2, exit_spec=spec)
                order = self.broker.ib.placeOrder.call_args.args[1]
                self.assertEqual(order.orderType, spec["type"])
                self.assertTrue(order.outsideRth)

    def test_conditional_order_boundary_after_whatif_does_not_send(self):
        detail, _, state, _ = self.send_fixture()
        def whatif(*_):
            self.broker._session.return_value = NS(kind="extended", order_type="LMT", outside_rth=True)
            return state
        self.broker.ib.whatIfOrder.side_effect = whatif
        with self.assertRaisesRegex(BrokerError, "离开常规时段"):
            self.broker._send(detail, "SELL", 2, exit_spec={"type": "STP", "stop_price": .2, "tif": "DAY"})
        self.broker.ib.placeOrder.assert_not_called()

    def test_pending_protective_order_blocks_duplicate_close(self):
        self.broker._open_orders = Mock(return_value=[NS(contract=NS(conId=123))])
        with self.assertRaisesRegex(BrokerError, "已有未完成订单"):
            self.broker.close(123, 100)
        self.broker.ib.placeOrder.assert_not_called()

    def test_outside_rth_ignored_warning_is_retained_on_order_row(self):
        detail, _, _, _ = self.send_fixture()
        self.broker._position_records = Mock(return_value=[NS(contract=detail.contract, position=5)])
        self.broker._send(detail, "SELL", 2, exit_spec={"type": "LMT", "limit_price": .4, "tif": "DAY"})
        trade = self.broker.ib.placeOrder.return_value
        order = self.broker.ib.placeOrder.call_args.args[1]
        trade = NS(order=order, contract=detail.contract,
                   orderStatus=NS(filled=0, remaining=2, status="Submitted", avgFillPrice=0))
        self.broker._on_error(order.orderId, 2109, "Outside RTH ignored")
        row = self.broker._order_dict(trade)
        self.assertIn("忽略盘外设置", row["execution_warning"])
        self.assertEqual(row["status"], "Submitted")


if __name__ == "__main__":
    unittest.main()
