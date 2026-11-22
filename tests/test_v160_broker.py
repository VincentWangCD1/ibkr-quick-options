"""V1.6 order integration; every IB transport method is mocked, never connected."""
import asyncio
import copy
from datetime import datetime, timezone
import time
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

from ib_async import Option, ContractDetails, OrderState, Trade, OrderStatus
from ib_broker import IBBroker, BrokerError


class BrokerV160Tests(unittest.TestCase):
    def setUp(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.b = IBBroker()
        self.b.ib = Mock()
        self.b.ib.isConnected.return_value = True
        self.b.account = "DU123"
        self.b._account_data = Mock()
        self.financial = dict(ready=True, cash=5000., usd_cash=5000., available_funds=5000., base_currency="USD")
        self.b._account_data.refresh.side_effect = lambda *_: copy.deepcopy(self.financial)
        self.b.ib.reqAllOpenOrders.return_value = []
        self.b.ib.trades.return_value = []
        self.b.ib.positions.return_value = []
        self.b.ib.reqPositions.return_value = []
        self.b._session = Mock(return_value=NS(kind="regular", outside_rth=False))
        self.b._execution_detail = Mock(side_effect=lambda detail: detail)
        self.b._quote = Mock(return_value={"bid": 1.01, "ask": 1.09})
        self.b._increments = Mock(return_value=[(0, .01)])
        self.b._selection_spot = Mock(return_value=(600, "真实标的", False))
        self.contracts = {}
        for side, identifier in (("C", 123), ("P", 124)):
            contract = Option("SPY", "20261218", 600, side, "SMART", multiplier="100",
                              currency="USD", tradingClass="SPY", conId=identifier)
            contract.localSymbol = "SPY " + side
            self.contracts[side] = ContractDetails(contract=contract, minTick=.01,
                                                   validExchanges="SMART")
        self.b._contracts = Mock(side_effect=lambda symbol, expiry, right, trading_class: [self.contracts[right]])
        self.b.ib.reqContractDetails.side_effect = lambda contract: [self.contracts[contract.right]]
        self.state = OrderState(warningText="", commission=1., commissionCurrency="USD",
                               initMarginChange="500", maintMarginChange="400", equityWithLoanChange="0",
                               initMarginAfter="1500", maintMarginAfter="1400", equityWithLoanAfter="10000")
        self.b.ib.whatIfOrder.side_effect = lambda *_: copy.deepcopy(self.state)
        self.placed = []
        self.b.ib.placeOrder.side_effect = self.place
        self.settings = dict(symbol="SPY", expiry="20261218", right="C", trading_class="SPY",
                             selection_mode="depth", moneyness="ATM", depth=1, percent=50,
                             open_action="BUY", order_mode="MKT", slippage_pct=5,
                             commission_per_contract=1, min_commission=1, max_contracts=100,
                             rounding_mode="nearest", seller_quantity=1)

    def tearDown(self):
        self.b.ib.connect.assert_not_called()
        self.loop.close()
        asyncio.set_event_loop(None)

    def place(self, contract, order):
        order.orderId, order.clientId = 90 + len(self.placed), 31
        trade = Trade(contract=contract, order=order, orderStatus=OrderStatus(
            status="Submitted", filled=0, remaining=order.totalQuantity, orderId=order.orderId))
        self.placed.append(trade)
        return trade

    def seller(self, **overrides):
        return self.b.preview({**self.settings, "open_action": "SELL", **overrides})

    def position(self, quantity=-2, right="C"):
        return NS(account=self.b.account, contract=self.contracts[right].contract,
                  position=quantity, avgCost=100)

    def fill(self, trade, quantity=1, price=1., commission=1.):
        trade.orderStatus.status = "Filled"
        trade.orderStatus.filled = quantity
        trade.orderStatus.remaining = 0
        trade.fills = [NS(execution=NS(execId="fill", shares=quantity, price=price),
                          commissionReport=NS(execId="fill", currency="USD", commission=commission))]

    def test_pair_has_independent_single_use_tokens_and_no_order(self):
        previews = self.b.preview_pair(self.settings)
        self.assertNotEqual(previews["C"]["token"], previews["P"]["token"])
        self.assertEqual(previews["P"]["contract"]["right"], "P")
        self.assertEqual(len(self.b._tokens), 2)
        self.b.ib.placeOrder.assert_not_called()

    def test_pair_one_quote_failure_keeps_other_side(self):
        self.b._quote.side_effect = lambda c, **_: (_ for _ in ()).throw(BrokerError("C unavailable")) if c.right == "C" else {"bid": 1., "ask": 1.1}
        previews = self.b.preview_pair(self.settings)
        self.assertEqual(previews["C"], {"error": "C unavailable"})
        self.assertIn("token", previews["P"])

    def test_explicit_strike_uses_each_conid_and_bypasses_spot_only(self):
        result = self.b.preview_pair({**self.settings, "selection_mode": "strike", "strike": 600,
                                      "conids": {"C": 123, "P": 124}})
        self.b._selection_spot.assert_not_called()
        self.assertEqual(result["C"]["contract"]["conid"], 123)
        self.assertEqual(result["P"]["contract"]["conid"], 124)
        self.assertEqual(self.b._quote.call_count, 4)  # Initial and final fresh pair evidence.
        self.assertGreater(self.b._session.call_count, 0)

    def test_explicit_wrong_conid_or_missing_strike_is_never_substituted(self):
        for override in ({"strike": 601}, {"strike": 600, "conids": {"C": 124}}):
            with self.subTest(override=override), self.assertRaises((BrokerError, ValueError)):
                self.b.preview({**self.settings, "selection_mode": "strike", **override})
        self.b.ib.placeOrder.assert_not_called()

    def test_blank_lmt_seeds_independent_frozen_midpoints(self):
        self.b._quote.side_effect = lambda c, **_: {"bid": 1.01, "ask": 1.09} if c.right == "C" else {"bid": .4, "ask": .44}
        result = self.b.preview_pair({**self.settings, "order_mode": "LMT", "limit_prices": {"C": "", "P": ""}})
        self.assertEqual(result["C"]["limit_price"], 1.05)
        self.assertEqual(result["P"]["limit_price"], .42)
        self.assertTrue(result["C"]["limit_seeded"])
        self.assertEqual(result["P"]["price_mode"], "LMT")

    def test_user_price_not_replaced_or_borrowed_from_opposite_side(self):
        result = self.b.preview_pair({**self.settings, "order_mode": "LMT", "limit_prices": {"C": 1.02}})
        self.assertEqual(result["C"]["limit_price"], 1.02)
        self.assertEqual(result["P"]["limit_price"], 1.05)
        self.assertNotIn("limit_seeded", result["C"])

    def test_limit_reseeds_only_when_actual_matched_contract_changes(self):
        result = self.b.preview_pair({**self.settings, "order_mode": "LMT",
                                     "limit_prices": {"C": .5, "P": .7},
                                     "limit_conids": {"C": 999, "P": 124}})
        self.assertEqual(result["C"]["limit_price"], 1.05)
        self.assertTrue(result["C"]["limit_reseeded"])
        self.assertEqual(result["P"]["limit_price"], .7)
        self.assertFalse(result["P"]["limit_reseeded"])

    def test_amount_budget_holds_during_final_recheck_even_with_nearest(self):
        settings = {**self.settings, "budget_mode": "amount", "cash_amount": 200,
                    "order_mode": "LMT", "limit_prices": {"C": 1.09}}
        preview = self.b.preview(settings)
        self.assertEqual(preview["quantity"], 1)
        result = self.b.buy(preview["token"])
        self.assertEqual(result["quantity"], 1)
        self.assertEqual(self.placed[0].order.action, "BUY")
        self.assertLessEqual(self.b._debit_reserve, 200)

    def test_seller_preview_uses_manual_quantity_margin_and_no_transmission(self):
        preview = self.seller(seller_quantity=2)
        self.assertEqual(preview["quantity"], 2)
        self.assertEqual(preview["action"], "SELL")
        self.assertTrue(preview["opens_short"])
        self.assertEqual(preview["estimated_credit"], 202)
        self.assertEqual(preview["margin_required"], 500)
        self.assertEqual(preview["buffered_cost"], 502)
        self.b.ib.placeOrder.assert_not_called()
        self.assertEqual(self.b.ib.whatIfOrder.call_args.args[1].action, "SELL")

    def test_seller_never_sizes_from_high_premium(self):
        self.b._quote.return_value = {"bid": 100., "ask": 101.}
        preview = self.seller(seller_quantity=1)
        self.assertEqual(preview["quantity"], 1)
        self.assertEqual(preview["estimated_credit"], 10000.)
        self.assertEqual(preview["margin_required"], 500)

    def test_seller_margin_exceeds_explicit_allocation_rejects(self):
        with self.assertRaisesRegex(BrokerError, "超过预算"):
            self.seller(budget_mode="amount", cash_amount=500)
        self.b.ib.placeOrder.assert_not_called()

    def test_seller_missing_or_invalid_whatif_margin_never_sends(self):
        for bad in ("", "NaN", "1.7976931348623157e308", "-1", "0"):
            with self.subTest(bad=bad):
                self.state.initMarginChange = bad
                with self.assertRaises(BrokerError):
                    self.seller()
        self.b.ib.placeOrder.assert_not_called()

    def test_seller_whatif_absolute_equity_insufficient_rejects(self):
        self.state.equityWithLoanAfter = "1499"
        with self.assertRaisesRegex(BrokerError, "权益不足"):
            self.seller()

    def test_seller_rejects_existing_long_or_short_instead_of_crossing(self):
        for qty in (1, -1):
            with self.subTest(qty=qty):
                self.b.ib.reqPositions.return_value = [self.position(qty)]
                with self.assertRaisesRegex(BrokerError, "已有持仓"):
                    self.seller()

    def test_seller_send_routes_sell_and_cannot_be_quick_amended(self):
        preview = self.seller(order_mode="MID")
        result = self.b.buy(preview["token"])
        self.assertEqual((result["action"], result["order_type"], result["quantity"]), ("SELL", "LMT", 1))
        self.assertEqual(result["position_effect"], "OPEN_SHORT")
        self.assertFalse(result["modifiable"])
        self.assertEqual(self.placed[0].order.lmtPrice, preview["limit_price"])
        self.assertEqual(len(self.placed), 1)
        with self.assertRaisesRegex(BrokerError, "卖方开仓/买回"):
            self.b._plain_order(self.placed[0])
        with self.assertRaises(BrokerError):
            self.b.buy(preview["token"])
        self.assertEqual(len(self.placed), 1)

    def test_seller_margin_increase_after_preview_requires_reconfirmation(self):
        preview = self.seller()
        self.state.initMarginChange = "501"
        with self.assertRaisesRegex(BrokerError, "保证金或数量已变化"):
            self.b.buy(preview["token"])
        self.b.ib.placeOrder.assert_not_called()

    def test_seller_funds_drop_during_whatif_prevents_send(self):
        preview = self.seller()
        def whatif(*_):
            self.financial["available_funds"] = self.financial["cash"] = 300
            return self.state
        self.b.ib.whatIfOrder.side_effect = whatif
        with self.assertRaisesRegex(BrokerError, "超过预算"):
            self.b.buy(preview["token"])
        self.b.ib.placeOrder.assert_not_called()

    def test_seller_position_appearing_after_whatif_prevents_send(self):
        preview = self.seller()
        def whatif(*_):
            self.b.ib.reqPositions.return_value = [self.position(-1)]
            return self.state
        self.b.ib.whatIfOrder.side_effect = whatif
        with self.assertRaisesRegex(BrokerError, "订单或持仓变化"):
            self.b.buy(preview["token"])
        self.b.ib.placeOrder.assert_not_called()

    def test_seller_deadline_expiry_after_whatif_does_not_send(self):
        preview = self.seller()
        with self.assertRaisesRegex(BrokerError, "有效期"):
            self.b._send_short_open(self.contracts["C"], preview, {**self.settings, "open_action": "SELL"}, time.monotonic() - 1)
        self.b.ib.placeOrder.assert_not_called()

    def test_seller_socket_exception_locks_and_never_retries(self):
        preview = self.seller()
        self.b.ib.placeOrder.side_effect = TimeoutError("transport")
        with self.assertRaisesRegex(BrokerError, "结果不明确"):
            self.b.buy(preview["token"])
        self.assertTrue(self.b._uncertain)
        self.b.ib.placeOrder.assert_called_once()

    def test_seller_hold_blocks_entry_until_fills_and_margin_reflected(self):
        preview = self.seller()
        self.b.buy(preview["token"])
        with self.assertRaises(BrokerError):
            self.b._check_short_margin_hold()
        self.fill(self.placed[0])
        with self.assertRaises(BrokerError):
            self.b._check_short_margin_hold()
        self.financial["available_funds"] = 4500
        self.b._sync_cash(force=True)
        self.b._check_short_margin_hold()
        self.assertIsNone(self.b._short_margin_hold)

    def test_positions_include_signed_short_and_close_partial_buys_to_cover(self):
        self.b.ib.positions.return_value = self.b.ib.reqPositions.return_value = [self.position(-5)]
        shown = self.b.positions()[0]
        self.assertEqual((shown["position_side"], shown["quantity"]), ("SHORT", -5))
        result = self.b.close(123, 50)
        self.assertEqual((result["action"], result["quantity"], result["position_effect"]), ("BUY", 2, "CLOSE_SHORT"))
        self.assertFalse(result["modifiable"])
        self.assertIsNotNone(self.b._cover_cash_hold)

    def test_cover_after_manual_position_change_cannot_open_long(self):
        self.b.ib.reqPositions.return_value = [self.position(-2)]
        def whatif(*_):
            self.b.ib.reqPositions.return_value = [self.position(-1)]
            return self.state
        self.b.ib.whatIfOrder.side_effect = whatif
        with self.assertRaisesRegex(BrokerError, "空头持仓变化"):
            self.b.close(123, 100)
        self.b.ib.placeOrder.assert_not_called()

    def test_short_advanced_sell_exit_cannot_accidentally_increase_short(self):
        self.b.ib.reqPositions.return_value = [self.position(-2)]
        with self.assertRaisesRegex(BrokerError, "快捷买回"):
            self.b.close(123, 100, order_spec={"type": "STP", "stop_price": 1})
        self.b.ib.placeOrder.assert_not_called()

    def test_cover_hold_requires_usd_cash_reflect_all_fills(self):
        self.b.ib.reqPositions.return_value = [self.position(-2)]
        self.b.close(123, 50)
        self.fill(self.placed[0], price=1.09)
        with self.assertRaises(BrokerError):
            self.b._check_cover_cash_hold()
        self.financial["usd_cash"] = 4890
        self.b._sync_cash(force=True)
        self.b._check_cover_cash_hold()
        self.assertIsNone(self.b._cover_cash_hold)

    def test_partial_seller_cancel_rechecks_actual_margin_without_linear_guess(self):
        self.state.initMarginChange = "1000"
        preview = self.seller(seller_quantity=2)
        self.b.buy(preview["token"])
        trade = self.placed[0]
        self.fill(trade, quantity=1)
        trade.orderStatus.status = "Cancelled"
        self.b.ib.reqPositions.return_value = [self.position(-1)]
        self.financial["available_funds"] = 4500
        self.b._sync_cash(force=True)
        # Actual margin for one filled contract is deliberately nonlinear:
        # the current portfolio Before values, not half the old estimate, prove it.
        self.state.initMarginBefore = "1700"
        self.state.equityWithLoanBefore = "6200"
        self.b._check_short_margin_hold()
        self.assertIsNone(self.b._short_margin_hold)
        self.assertEqual(len(self.placed), 1)

    def test_partial_seller_cancel_stale_available_funds_still_blocks(self):
        self.state.initMarginChange = "1000"
        preview = self.seller(seller_quantity=2)
        self.b.buy(preview["token"])
        self.fill(self.placed[0], quantity=1)
        self.placed[0].orderStatus.status = "Cancelled"
        self.b.ib.reqPositions.return_value = [self.position(-1)]
        self.state.initMarginBefore = "1700"
        self.state.equityWithLoanBefore = "6200"
        with self.assertRaisesRegex(BrokerError, "保证金扣减"):
            self.b._check_short_margin_hold()

    def test_buy_open_refuses_existing_short_at_preview_and_after_whatif(self):
        self.b.ib.reqPositions.return_value = [self.position(-1)]
        with self.assertRaisesRegex(BrokerError, "已有空头"):
            self.b.preview(self.settings)
        self.b.ib.reqPositions.return_value = []
        preview = self.b.preview(self.settings)
        def whatif(*_):
            self.b.ib.reqPositions.return_value = [self.position(-1)]
            return self.state
        self.b.ib.whatIfOrder.side_effect = whatif
        with self.assertRaisesRegex(BrokerError, "已有空头"):
            self.b.buy(preview["token"])
        self.b.ib.placeOrder.assert_not_called()

    def test_pair_refreshes_aged_first_token_from_fresh_book_without_repricing_limit(self):
        clock = [100.]
        initial_preview = self.b.preview
        def preview(settings):
            value = initial_preview(settings)
            if settings["right"] == "P":
                clock[0] += 12
                self.b._quote.return_value = {"bid": 1.20, "ask": 1.30}
            return value
        self.b.preview = preview
        with patch("ib_broker.time.monotonic", side_effect=lambda: clock[0]):
            result = self.b.preview_pair({**self.settings, "order_mode": "MID"})
            self.assertEqual(result["C"]["bid"], 1.20)
            self.assertEqual(result["C"]["limit_price"], 1.05)
            self.assertEqual(self.b._tokens[result["C"]["token"]][2], 112.)
            self.assertEqual(self.b._tokens[result["P"]["token"]][2], 112.)

    def test_pair_does_not_renew_without_final_live_quote(self):
        calls = {"C": 0, "P": 0}
        def quote(contract, **kwargs):
            calls[contract.right] += 1
            if contract.right == "C" and calls["C"] == 2:
                raise BrokerError("C stale at final check")
            return {"bid": 1.01, "ask": 1.09}
        self.b._quote.side_effect = quote
        result = self.b.preview_pair(self.settings)
        self.assertEqual(result["C"], {"error": "C stale at final check"})
        self.assertIn("token", result["P"])
        self.assertEqual(len(self.b._tokens), 1)

    def test_fresh_last_only_underlying_is_not_discarded(self):
        contract = NS(conId=900, localSymbol="SPX", symbol="SPX")
        self.b._tickers[900] = NS(contract=contract, marketDataType=1, halted=0,
                                last=6000., bid=float("nan"), ask=float("nan"))
        self.b._price_times[900] = {4: time.monotonic()}
        result = IBBroker._quote(self.b, contract, underlying=True, wait=.1)
        self.assertEqual(result["spot"], 6000.)
        self.b.ib.cancelMktData.assert_not_called()
        self.b.ib.reqMktData.assert_not_called()


if __name__ == "__main__":
    unittest.main()
