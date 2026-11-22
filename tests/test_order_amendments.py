"""Broker-only regression tests. IB is replaced by a mock before any call."""
import asyncio
import copy
import time
import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock

from eventkit import Event
from ib_async import Option, LimitOrder, MarketOrder, Trade, OrderStatus
from ib_broker import IBBroker, BrokerError


class AmendmentsTests(unittest.TestCase):
    def setUp(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.b = IBBroker()
        self.b.ib = Mock()
        self.b.ib.isConnected.return_value = True
        self.b.ib.openOrderEvent = Event()
        self.b.account = "DU123"
        self.b.mode = "PAPER"
        self.b.client_id = 31
        self.b._cash = self.b._raw_cash = 1000
        self.b._financial = {"ready": True, "cash": 1000}
        self.contract = Option("SPY", "20261218", 600, "C", "SMART", multiplier="100", currency="USD")
        self.contract.conId = 123
        self.contract.localSymbol = "SPY MOCK"
        self.detail = NS(contract=self.contract, minTick=.01, marketRuleIds="", validExchanges="SMART")
        self.trade = Trade(contract=self.contract,
                           order=LimitOrder("BUY", 10, .2, orderId=7, clientId=31,
                                            account="DU123", orderRef="QOPT1:test", tif="GTC"),
                           orderStatus=OrderStatus(orderId=7, status="Submitted", filled=0, remaining=10))
        self.trades = [self.trade]
        self.b.ib.trades.side_effect = lambda: self.trades
        self.b.ib.reqAllOpenOrders.side_effect = lambda: [t for t in self.trades if not t.isDone()]
        self.b.ib.reqContractDetails.return_value = [self.detail]
        self.b.ib.reqExecutions.return_value = []
        self.b._session = Mock(return_value=NS(kind="regular", order_type="MKT", outside_rth=False))
        self.b._increments = Mock(return_value=[(0, .01)])
        self.b._quote = Mock(return_value={"bid": .2, "ask": .24})
        self.b._sync_cash = Mock(side_effect=lambda **_: self.b._raw_cash)
        self.b._position_records = Mock(return_value=[NS(contract=self.contract, position=10)])
        self.b.ib.whatIfOrder.return_value = NS(warningText="", commission=1, commissionCurrency="USD")
        self.placements = []
        self.pending_ack = None
        self.on_sleep = None
        self.b.ib.sleep.side_effect = self.sleep
        self.b.ib.placeOrder.side_effect = self.place
        self.b.ib.cancelOrder.side_effect = self.cancel

    def tearDown(self):
        self.loop.close()
        asyncio.set_event_loop(None)

    def fill(self, quantity, status="Submitted"):
        self.trade.orderStatus.filled = quantity
        self.trade.orderStatus.remaining = float(self.trade.order.totalQuantity) - quantity
        self.trade.orderStatus.status = status
        self.trade.fills = [] if quantity == 0 else [NS(
            execution=NS(execId="exec1", shares=quantity, price=.2),
            commissionReport=NS(execId="exec1", currency="USD", commission=1))]

    def sleep(self, _):
        if self.on_sleep:
            callback, self.on_sleep = self.on_sleep, None
            callback()
        if self.pending_ack:
            trade, order = self.pending_ack
            self.pending_ack = None
            trade.order = order
            self.b.ib.openOrderEvent.emit(trade)

    def place(self, contract, order):
        self.placements.append(copy.deepcopy(order))
        if order.orderId == self.trade.order.orderId:
            self.pending_ack = (self.trade, copy.deepcopy(order))
            return self.trade
        order.orderId = 99
        new = Trade(contract=contract, order=order,
                    orderStatus=OrderStatus(orderId=99, status="Submitted", filled=0,
                                            remaining=order.totalQuantity))
        self.trades.append(new)
        return new

    def cancel(self, _):
        self.trade.orderStatus.status = "PendingCancel"
        if self.on_sleep is None:
            self.on_sleep = lambda: setattr(self.trade.orderStatus, "status", "Cancelled")

    def test_same_id_partial_limit_amend_preserves_total_tif_and_route(self):
        self.fill(3)
        preview = self.b.preview_modify(7, "LMT", .18)
        self.assertEqual((preview["method"], preview["remaining"]), ("amend", 7))
        result = self.b.modify_order(preview["token"])
        order = self.placements[0]
        self.assertEqual((order.orderId, order.totalQuantity, order.lmtPrice, order.tif), (7, 10, .18, "GTC"))
        self.assertEqual(result["modification"], "amend_acknowledged")
        self.b.ib.cancelOrder.assert_not_called()

    def test_mid_preview_is_fixed_even_if_book_moves(self):
        preview = self.b.preview_modify(7, "MID")
        self.assertEqual(preview["limit_price"], .22)
        self.b._quote.return_value = {"bid": .3, "ask": .4}
        self.b.modify_order(preview["token"])
        self.assertEqual(self.placements[0].lmtPrice, .22)

    def test_type_replace_recomputes_partial_fills_during_cancel(self):
        self.fill(2)
        preview = self.b.preview_modify(7, "MKT")
        self.on_sleep = lambda: self.fill(4, "Cancelled")
        result = self.b.modify_order(preview["token"])
        self.assertEqual((result["quantity"], result["order_type"], result["replaced_order_id"]), (6, "MKT", 7))
        self.assertEqual(self.placements[0].tif, "GTC")
        self.b.ib.cancelOrder.assert_called_once()

    def test_fully_filled_cancel_race_sends_no_replacement(self):
        preview = self.b.preview_modify(7, "MKT")
        self.on_sleep = lambda: self.fill(10, "Filled")
        result = self.b.modify_order(preview["token"])
        self.assertEqual(result["modification"], "filled_before_replace")
        self.assertFalse(self.placements)

    def test_pending_cancel_timeout_sends_no_replacement(self):
        self.b.CANCEL_WAIT_SECONDS = 0
        preview = self.b.preview_modify(7, "MKT")
        with self.assertRaises(BrokerError):
            self.b.modify_order(preview["token"])
        self.assertFalse(self.placements)

    def test_unacknowledged_amendment_is_visible_and_not_modifiable(self):
        self.b.AMEND_WAIT_SECONDS = 0
        preview = self.b.preview_modify(7, "LMT", .18)
        result = self.b.modify_order(preview["token"])
        self.assertEqual(result["modification"], "amend_requested")
        self.assertEqual(result["limit_price"], .2)  # old broker-confirmed price
        self.assertTrue(result["owned"])
        self.assertFalse(result["modifiable"])
        self.assertIn("待确认", result["execution_warning"])

    def test_quantity_increase_during_cancel_never_increases_replacement(self):
        preview = self.b.preview_modify(7, "MKT")
        def changed():
            self.trade.order.totalQuantity = 20
            self.trade.orderStatus.remaining = 20
            self.trade.orderStatus.status = "Cancelled"
        self.on_sleep = changed
        with self.assertRaisesRegex(BrokerError, "参数变化"):
            self.b.modify_order(preview["token"])
        self.assertFalse(self.placements)

    def test_missing_execution_after_cancel_sends_no_replacement(self):
        self.b.CANCEL_WAIT_SECONDS = .002
        preview = self.b.preview_modify(7, "MKT")
        def incomplete():
            self.trade.orderStatus.status = "Cancelled"
            self.trade.orderStatus.filled = 3
            self.trade.orderStatus.remaining = 7
        self.on_sleep = incomplete
        with self.assertRaises(BrokerError):
            self.b.modify_order(preview["token"])
        self.assertFalse(self.placements)

    def test_state_change_before_submit_rejects_token(self):
        preview = self.b.preview_modify(7, "LMT", .25)
        self.fill(1)
        with self.assertRaises(BrokerError):
            self.b.modify_order(preview["token"])
        self.assertFalse(self.placements)

    def test_expired_token_and_repeated_token_rejected(self):
        preview = self.b.preview_modify(7, "LMT", .25)
        self.b._modify_tokens[preview["token"]]["created"] -= 11
        for _ in range(2):
            with self.assertRaises(BrokerError):
                self.b.modify_order(preview["token"])
        self.assertFalse(self.placements)

    def test_foreign_client_cannot_preview_modify_or_cancel(self):
        self.trade.order.clientId = 44
        for action in (lambda: self.b.preview_modify(7, "MKT"), lambda: self.b.cancel_order(7)):
            with self.assertRaises(BrokerError):
                action()
        self.b.ib.cancelOrder.assert_not_called()

    def test_conditional_order_clearly_restricted(self):
        self.trade.order.orderType = "STP"
        with self.assertRaisesRegex(BrokerError, "止损"):
            self.b.preview_modify(7, "MKT")

    def test_whatif_rejection_keeps_original_order(self):
        preview = self.b.preview_modify(7, "MKT")
        self.b.ib.whatIfOrder.return_value.warningText = "credit check"
        with self.assertRaises(BrokerError):
            self.b.modify_order(preview["token"])
        self.b.ib.cancelOrder.assert_not_called()
        self.assertFalse(self.placements)

    def test_buy_existing_local_reserve_counted_once(self):
        self.b._reserve_trade = self.trade
        self.b._reserve_cash_baseline = 1000
        self.b._debit_reserve = 900
        self.b._cash = 100  # _raw_cash remains1000: local hold is this same order.
        preview = self.b.preview_modify(7, "LMT", .5)
        self.b.modify_order(preview["token"])
        self.assertEqual(self.b._debit_reserve, 510)
        self.assertEqual(self.b._cash, 490)

    def test_sell_replace_cannot_oversell_after_partial_fills(self):
        self.trade.order.action = "SELL"
        self.b._close_reservations[123] = (self.trade, 10)
        self.fill(2)
        preview = self.b.preview_modify(7, "MKT")
        self.on_sleep = lambda: self.fill(4, "Cancelled")
        result = self.b.modify_order(preview["token"])
        self.assertEqual(result["quantity"], 6)
        self.assertEqual(self.b._close_reservations[123][1], 6)

    def test_broker_available_hold_is_restored_once_but_ledger_caps_remain(self):
        self.b._reserve_trade = self.trade
        self.b._reserve_cash_baseline = 1000
        self.b._debit_reserve = 210
        self.b._raw_cash = 790
        self.b._financial = dict(ready=True, usd_cash=1000, settlement_cash_cap=1000,
                                 available_funds=790, ledger_settled_cash=950)
        plan = dict(order_type="LMT", limit_price=.94)
        risk = self.b._amend_capacity(self.trade, plan, 10)
        self.assertEqual(risk["capacity"], 950)
        self.assertEqual(risk["cost"], 950)
        with self.assertRaises(BrokerError):
            self.b._amend_capacity(self.trade, dict(order_type="LMT", limit_price=.95), 10)
        # A repeated preflight cannot repeatedly credit the same old hold.
        self.assertEqual(self.b._amend_capacity(self.trade, plan, 10)["capacity"], 950)

    def test_existing_fills_and_negative_fees_cannot_inflate_restored_cash(self):
        self.b._reserve_trade = self.trade
        self.b._reserve_cash_baseline = 1000
        self.b._debit_reserve = 210
        self.fill(3)
        self.trade.fills[0].commissionReport.commission = -5  # rebate is not spendable credit
        self.b._raw_cash = 790
        self.b._financial = dict(ready=True, usd_cash=1000, settlement_cash_cap=1000,
                                 available_funds=790, ledger_settled_cash=None)
        risk = self.b._amend_capacity(self.trade, dict(order_type="LMT", limit_price=.2), 7)
        self.assertAlmostEqual(risk["paid"], 60)
        self.assertEqual(risk["capacity"], 930)  # min(1000-60,790+7*.2*100)

    def test_market_replacement_final_price_rise_aborts_after_cancel(self):
        preview = self.b.preview_modify(7, "MKT")
        original_capacity = self.b._amend_capacity
        calls = []
        def capacity(*args, **kwargs):
            risk = original_capacity(*args, **kwargs)
            calls.append(1)
            if len(calls) == 2:  # final post-cancel funds check is finished
                self.b._quote.return_value = {"bid": .4, "ask": .5}
            return risk
        self.b._amend_capacity = capacity
        with self.assertRaisesRegex(BrokerError, "上涨"):
            self.b.modify_order(preview["token"])
        self.assertEqual(self.trade.orderStatus.status, "Cancelled")
        self.assertFalse(self.placements)

    def test_insufficient_buy_cash_rejects_before_cancel(self):
        self.b._raw_cash = 5
        preview = self.b.preview_modify(7, "MKT")
        with self.assertRaises(BrokerError):
            self.b.modify_order(preview["token"])
        self.b.ib.cancelOrder.assert_not_called()

    def test_extended_amend_to_true_market_rejected(self):
        self.b._session.return_value.kind = "extended"
        self.trade.order.outsideRth = True
        with self.assertRaisesRegex(BrokerError, "真正市价"):
            self.b.preview_modify(7, "MKT")

    def test_manual_limit_entry_below_ask_is_valid_in_regular_session(self):
        self.trades.clear()
        settings = dict(percent=50, order_mode="LMT", limit_price=.1, rounding_mode="nearest",
                        slippage_pct=0, commission_per_contract=0, min_commission=0, max_contracts=100)
        result = self.b._send(self.detail, "BUY", 2, settings=settings, budget=500)
        self.assertEqual((result["order_type"], result["limit_price"]), ("LMT", .1))

    def test_nearest_entry_can_exceed_target_but_not_cash(self):
        self.trades.clear()
        settings = dict(percent=3.6, order_mode="MKT", rounding_mode="nearest",
                        slippage_pct=0, commission_per_contract=0, min_commission=0, max_contracts=100)
        result = self.b._send(self.detail, "BUY", 2, settings=settings, budget=36)
        self.assertEqual(result["quantity"], 2)  # $36/$24=1.5 ->2; $48>$36 target.
        self.assertLessEqual(self.b._debit_reserve, 1000)


if __name__ == "__main__":
    unittest.main()
