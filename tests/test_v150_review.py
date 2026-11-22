"""Independent V1.5 regressions: pure/synthetic/mocked only; no Tk or network."""
from decimal import Decimal
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock

from core import size_order
from demo_broker import DemoBroker
from entry_orders import build_entry_plan
from ib_broker import IBBroker, BrokerError
from order_amendments import OrderAmendmentsMixin


class RoundingAndEntryReview(unittest.TestCase):
    def test_half_up_can_exceed_target_but_not_total_cash(self):
        result = size_order(250, 50, .5, 100, 0, 0, 0, rounding_mode="nearest")
        self.assertEqual(result["raw_quantity"], 2.5)
        self.assertEqual(result["quantity"], 3)
        self.assertEqual(result["estimated_premium"], 150)
        self.assertEqual(result["budget_difference"], 25)
        self.assertEqual(result["actual_percent"], 60)

    def test_full_cash_rounding_still_reserves_fees_and_slippage(self):
        result = size_order(100, 100, .2, 100, 5, 1, 1)
        self.assertEqual(result["quantity"], 4)
        self.assertEqual(result["buffered_cost"], 88)

    def test_floor_mode_remains_bounded_by_percentage(self):
        result = size_order(250, 50, .5, 100, 0, 0, 0, rounding_mode="floor")
        self.assertEqual(result["quantity"], 2)
        self.assertLessEqual(result["buffered_cost"], result["budget"])

    def test_midpoint_is_fixed_limit_conservatively_rounded(self):
        quote = {"bid": .20, "ask": .25}
        buy = build_entry_plan("BUY", "MID", quote, [(0, .01)])
        sell = build_entry_plan("SELL", "MID", quote, [(0, .01)])
        self.assertEqual((buy["order_type"], buy["limit_price"], buy["midpoint"]), ("LMT", .22, .225))
        self.assertEqual(sell["limit_price"], .23)
        quote["ask"] = .99
        self.assertEqual(buy["limit_price"], .22)


class DemoEntryReview(unittest.TestCase):
    def setUp(self):
        self.broker = DemoBroker()
        self.broker.connect()
        self.ask = Decimal(".23")
        self.broker._quote = lambda strike, right, symbol="SPY": (Decimal("600.4"), Decimal(".20"), self.ask)
        self.settings = dict(symbol="SPY", expiry=self.broker.expirations("SPY")[0], right="C",
                             moneyness="OTM", depth=4, percent=50, slippage_pct=0,
                             commission_per_contract=1, min_commission=1, max_contracts=100,
                             rounding_mode="nearest", order_mode="LMT", limit_price=.19)

    def test_nonmarketable_entry_rests_and_cancel_releases_cash(self):
        preview = self.broker.preview(self.settings)
        order = self.broker.buy(preview["token"])
        self.assertEqual(order["filled"], 0)
        self.assertGreater(order["remaining"], 0)
        self.assertEqual(self.broker.positions(), [])
        self.assertLess(self.broker.snapshot()["cash"], 1000)
        self.broker.cancel_order(order["order_id"])
        self.assertEqual(self.broker.snapshot()["cash"], 1000)

    def test_mid_order_limit_does_not_follow_later_quote(self):
        preview = self.broker.preview({**self.settings, "order_mode": "MID", "limit_price": None})
        order = self.broker.buy(preview["token"])
        self.assertEqual(order["limit_price"], .21)
        self.assertEqual(order["filled"], 0)
        self.ask = Decimal(".20")
        self.broker.pump()
        filled = self.broker.orders()[0]
        self.assertEqual(filled["remaining"], 0)
        self.assertEqual(filled["limit_price"], .21)
        self.assertEqual(filled["avg_fill_price"], .20)

    def test_type_change_demo_cancels_old_order_and_replaces_remaining(self):
        preview = self.broker.preview(self.settings)
        old = self.broker.buy(preview["token"])
        change = self.broker.preview_modify(old["order_id"], "MKT")
        self.assertEqual(change["method"], "replace")
        result = self.broker.modify_order(change["token"])
        orders = self.broker.orders()
        self.assertNotEqual(result["order_id"], old["order_id"])
        self.assertIn("Cancelled", orders[0]["status"])
        self.assertEqual(result["quantity"], old["remaining"])
        self.assertEqual(result["filled"], old["remaining"])


class BrokerExecutionReview(unittest.TestCase):
    def fixture(self, cash=250):
        broker = IBBroker.__new__(IBBroker)
        broker.ib = Mock()
        broker.ib.reqPositions.return_value = []
        broker._require = Mock()
        broker._session = Mock(return_value=NS(kind="regular", order_type="MKT", outside_rth=False))
        broker._increments = Mock(return_value=[(0, .01)])
        broker._open_orders = Mock(return_value=[])
        broker._quote = Mock(return_value={"bid": .49, "ask": .50})
        broker._sync_cash = Mock(return_value=cash)
        broker.ib.whatIfOrder.return_value = NS(warningText="", commission=0, commissionCurrency="USD")
        broker.account, broker.mode, broker.client_id = "DU1", "PAPER", 31
        broker._cash = broker._raw_cash = cash
        broker._reserve_cash_baseline = None
        broker._debit_reserve = 0
        broker._reserve_trade = None
        broker._tokens, broker._close_reservations, broker._order_sizing_settings = {}, {}, {}
        broker.log = Mock()
        broker._order_dict = lambda trade: {"quantity": trade.order.totalQuantity, "status": trade.orderStatus.status}
        def placed(contract, order):
            return NS(contract=contract, order=order, fills=[],
                      orderStatus=NS(status="Submitted", filled=0, remaining=order.totalQuantity))
        broker.ib.placeOrder.side_effect = placed
        contract = NS(conId=12, multiplier="100", localSymbol="MOCK C604", exchange="SMART")
        detail = NS(contract=contract)
        settings = dict(percent=50, slippage_pct=0, commission_per_contract=0, min_commission=0,
                        max_contracts=100, rounding_mode="nearest", order_mode="MKT")
        return broker, detail, settings

    def test_send_does_not_floor_half_up_quantity_back_to_target(self):
        broker, detail, settings = self.fixture()
        result = broker._send(detail, "BUY", 3, settings=settings, cash=250, budget=125)
        self.assertEqual(result["quantity"], 3)
        self.assertEqual(broker._debit_reserve, 150)
        broker.ib.connect.assert_not_called()

    def test_regular_session_intentional_limit_below_ask_is_sent(self):
        broker, detail, settings = self.fixture()
        settings.update(order_mode="LMT", limit_price=.40)
        plan = broker._execution_plan(detail, "BUY", {"bid": .49, "ask": .50}, settings)
        result = broker._send(detail, "BUY", 3, settings=settings, cash=250, budget=125, plan=plan)
        order = broker.ib.placeOrder.call_args.args[1]
        self.assertEqual((order.orderType, order.lmtPrice, order.account), ("LMT", .40, "DU1"))
        self.assertEqual(result["quantity"], 3)
        self.assertFalse(order.outsideRth)


class AmendmentReview(unittest.TestCase):
    @staticmethod
    def trade(total=10, filled=3, status="Submitted", order_type="LMT"):
        from ib_async import Option, Order, OrderStatus, Trade
        contract = Option("SPY", "20261120", 604, "C", "SMART", multiplier="100", currency="USD")
        contract.conId = 12
        order = Order(orderId=9, permId=90, clientId=31, orderRef="QOPT1:test", account="DU1",
                      totalQuantity=total, action="BUY", orderType=order_type, lmtPrice=.2, tif="DAY")
        states = OrderStatus(status=status, filled=filled, remaining=total-filled)
        trade = Trade(contract, order, states)
        if filled:
            trade.fills.append(NS(execution=NS(execId="mock-1", shares=filled, price=.2)))
        return trade

    def test_same_id_price_amend_preserves_original_total_after_partial_fill(self):
        old = self.trade()
        changed = OrderAmendmentsMixin._copy_changed_order(old, {"order_type": "LMT", "limit_price": .25}, 7, False)
        self.assertEqual((changed.orderId, changed.totalQuantity, changed.lmtPrice), (9, 10, .25))
        self.assertEqual(old.order.lmtPrice, .2)
        self.assertEqual((changed.account, changed.clientId), ("DU1", 31))

    def test_type_replacement_uses_remaining_and_new_id(self):
        old = self.trade()
        changed = OrderAmendmentsMixin._copy_changed_order(old, {"order_type": "MKT", "limit_price": None}, 7, True)
        self.assertEqual((changed.orderId, changed.permId, changed.totalQuantity), (0, 0, 7))
        self.assertIn(":replace:", changed.orderRef)
        self.assertEqual(old.order.totalQuantity, 10)

    def test_pending_cancel_is_not_a_terminal_cancel(self):
        broker = IBBroker.__new__(IBBroker)
        broker.ib, broker._require = Mock(), Mock()
        broker.CANCEL_WAIT_SECONDS = 0
        with self.assertRaises(BrokerError):
            broker._wait_cancelled(self.trade(status="PendingCancel"))
        broker.ib.placeOrder.assert_not_called()

    def test_full_fill_during_cancel_has_no_replacement_quantity(self):
        broker = IBBroker.__new__(IBBroker)
        broker.ib, broker._require = Mock(), Mock()
        self.assertEqual(broker._wait_cancelled(self.trade(filled=10, status="Filled")), 0)
        broker.ib.reqExecutions.assert_called_once()

    def test_plain_order_rejects_conditionals_and_incomplete_fill_callbacks(self):
        broker = IBBroker.__new__(IBBroker)
        broker._amend_pending = set()
        with self.assertRaises(BrokerError):
            broker._plain_order(self.trade(order_type="STP"))
        trade = self.trade()
        trade.fills.clear()
        with self.assertRaises(BrokerError):
            broker._plain_order(trade)

    def test_tracked_custom_buffers_survive_limit_to_market_amendment_plan(self):
        broker = IBBroker.__new__(IBBroker)
        trade = self.trade(filled=0)
        broker._order_sizing_settings = {9: dict(slippage_pct=10, commission_per_contract=3, min_commission=5)}
        broker._session = Mock(return_value=NS(kind="regular", order_type="MKT", outside_rth=False))
        broker._quote = Mock(return_value={"bid": .19, "ask": .20})
        broker._increments = Mock(return_value=[(0, .01)])
        plan, _ = broker._amend_plan(trade, NS(contract=trade.contract), "MKT")
        cost, checked_ask, configured_fee = broker._amend_cost(trade, plan, 10)
        self.assertEqual(checked_ask, .20)
        self.assertAlmostEqual(cost, 250)  # $200 principal +10% +$3 per contract.
        self.assertEqual(configured_fee, 30)
        self.assertEqual(plan["reserve_settings_source"], "original")
        self.assertEqual(plan["commission_per_contract"], 3)

    def test_tracked_limit_amendment_keeps_fees_without_market_slippage(self):
        broker = IBBroker.__new__(IBBroker)
        trade = self.trade(filled=0)
        broker._order_sizing_settings = {9: dict(slippage_pct=10, commission_per_contract=3, min_commission=5)}
        broker._session = Mock(return_value=NS(kind="regular", order_type="MKT", outside_rth=False))
        broker._quote = Mock(return_value={"bid": .19, "ask": .20})
        broker._increments = Mock(return_value=[(0, .01)])
        plan, _ = broker._amend_plan(trade, NS(contract=trade.contract), "LMT", .20)
        cost, _, configured_fee = broker._amend_cost(trade, plan, 10)
        self.assertAlmostEqual(cost, 230)
        self.assertEqual(configured_fee, 30)


if __name__ == "__main__":
    unittest.main()
