"""V1.7 mocked position/close/history regression cases; never connects to TWS."""
import copy
import time
from datetime import datetime, timezone
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

from ib_async import LimitOrder, StopOrder, Trade, OrderStatus, Execution, Fill, CommissionReport
from ib_broker import BrokerError
import tests.test_v160_broker as fixtures


class BrokerV170Tests(unittest.TestCase):
    setUp = fixtures.BrokerV160Tests.setUp
    tearDown = fixtures.BrokerV160Tests.tearDown
    place = fixtures.BrokerV160Tests.place
    position = fixtures.BrokerV160Tests.position

    def holding(self, quantity=5):
        p = self.position(quantity)
        self.b.ib.reqPositions.return_value = self.b.ib.positions.return_value = [p]
        return p

    def closing_trade(self, total=2, filled=2, status="Filled", action="SELL", perm=700):
        return Trade(contract=self.contracts["C"].contract,
                     order=LimitOrder(action, total, 1., account=self.b.account,
                                      clientId=31, orderId=70, permId=perm, orderRef="QOPT1:close-test"),
                     orderStatus=OrderStatus(orderId=70, permId=perm, status=status,
                                             filled=filled, remaining=max(0, total-filled)))

    def execution(self, qty=2, account=None, perm=700, exec_id="fill.01", side="SLD"):
        e = Execution(execId=exec_id, acctNumber=account or self.b.account, permId=perm,
                      clientId=31, orderId=70, side=side, shares=qty, price=1., cumQty=qty,
                      time=datetime.now(timezone.utc))
        return Fill(self.contracts["C"].contract, e, CommissionReport(execId=exec_id, commission=1., currency="USD"), e.time)

    def reservation(self, trade=None, baseline=5, observed=3, fills=None, completed=None):
        trade = trade or self.closing_trade()
        self.b._close_reservations[123] = (trade, baseline)
        self.holding(observed)
        self.b.ib.trades.return_value = [trade]
        self.b.ib.reqAllOpenOrders.return_value = []
        self.b.ib.reqCompletedOrders.return_value = completed or []
        self.b.ib.fills.return_value = []
        self.b.ib.reqExecutions.return_value = fills if fills is not None else [self.execution()]
        return trade

    def test_position_pnl_has_per_option_cost_signed_value_and_live_mark(self):
        p = self.holding(-3)
        p.avgCost = 100
        self.b._position_quote = Mock(return_value={"bid": 1.1, "ask": 1.3, "last": 1.15,
                                                    "market_data_type": 1, "bid_age": 1, "ask_age": 1})
        row = self.b.positions()[0]
        self.assertEqual(row["avg_option_price"], 1)
        self.assertEqual(row["entry_cost"], 300)
        self.assertAlmostEqual(row["current_price"], 1.2)
        self.assertAlmostEqual(row["market_value"], -360)
        self.assertAlmostEqual(row["unrealized_pnl"], -60)
        self.assertAlmostEqual(row["unrealized_pnl_pct"], -20)
        self.assertTrue(row["price_fresh"])
        self.b.ib.reqExecutions.assert_not_called()

    def test_existing_other_symbol_position_visible_without_loading_chain(self):
        p = self.holding(2)
        p.contract = copy.deepcopy(p.contract)
        p.contract.symbol = "AAPL"
        self.assertNotIn("AAPL", self.b._watched_symbols)
        self.b._position_quote = Mock(return_value={})
        self.assertEqual(self.b.positions()[0]["symbol"], "AAPL")
        self.b._contracts.assert_not_called()

    def test_portfolio_fallback_is_explicit_reference(self):
        p = self.holding(2)
        self.b.ib.portfolio.return_value = [NS(account=self.b.account, contract=p.contract,
                                             marketPrice=1.5, marketValue=300, unrealizedPNL=100)]
        self.b._position_quote = Mock(return_value={})
        row = self.b.positions()[0]
        self.assertEqual(row["current_price"], 1.5)
        self.assertFalse(row["price_fresh"])
        self.assertIn("组合估值", row["price_source"])
        self.assertEqual(row["unrealized_pnl"], 100)

    def test_active_protection_shows_actual_trigger_status_and_blocks_duplicate(self):
        self.holding(5)
        trade = Trade(contract=self.contracts["C"].contract,
                      order=StopOrder("SELL", 5, .8, account=self.b.account,
                                      clientId=31, orderId=70, orderRef="QOPT1:stop"),
                      orderStatus=OrderStatus(status="PreSubmitted", remaining=5))
        self.b.ib.trades.return_value = [trade]
        self.b._position_quote = Mock(return_value={})
        row = self.b.positions()[0]
        self.assertEqual(row["active_exit_orders"][0]["stop_price"], .8)
        self.assertIn("0.8", row["stop_loss_summary"])
        self.assertTrue(row["close_pending"])
        with self.assertRaisesRegex(BrokerError, "未完成订单"):
            self.b.preview_close(123)
        self.b.ib.placeOrder.assert_not_called()
        self.b.ib.cancelOrder.assert_not_called()

    def attach_protective_stop(self):
        self.b.ib.reqExecutions.return_value = []
        self.holding(4)
        trade = self.closing_trade(total=4, filled=0, status="PreSubmitted")
        trade.order = StopOrder("SELL", 4, .80, account=self.b.account, clientId=31, orderId=70,
                                permId=700, orderRef="QOPT1:PS:test", tif="DAY")
        self.b.ib.trades.return_value = [trade]
        self.b.ib.reqAllOpenOrders.side_effect = lambda: [] if trade.isDone() else [trade]
        self.b._close_reservations[123] = (trade, 4)
        self.b._position_quote = Mock(return_value={})
        return trade

    def test_unrelated_covered_stop_does_not_block_new_option(self):
        trade = self.attach_protective_stop()
        self.assertFalse(self.b._opening_blocked(123))
        self.assertFalse(self.b._opening_blocked(124))
        preview = self.b.preview(dict(self.settings, right="P"))
        self.assertEqual(self.b.buy(preview["token"])["conid"], 124)
        self.b.ib.cancelOrder.assert_not_called()
        trade.orderStatus.remaining = 5
        self.assertTrue(self.b._opening_blocked(124))

    def test_buy_same_contract_keeps_original_stop(self):
        stop = self.attach_protective_stop()
        before = self.b._order_fingerprint(stop)
        result = self.b.buy(self.b.preview(self.settings)["token"])
        self.assertEqual((result["action"], result["conid"]), ("BUY", 123))
        self.assertEqual(self.b._order_fingerprint(stop), before)
        self.b.ib.cancelOrder.assert_not_called()

    def test_add_ticket_with_partial_coverage_keeps_trailing_stop(self):
        stop = self.attach_protective_stop()
        stop.order.orderType = "TRAIL LIMIT"
        self.b._close_reservations[123] = (stop, 6)
        self.holding(6)
        self.assertTrue(self.b.positions()[0]["can_add_with_stop"])
        plan = self.b.preview_add(123, 6, {"method": "lots", "value": 2})
        self.assertEqual(self.b.buy(plan["token"])["quantity"], 2)
        self.assertEqual((stop.order.totalQuantity, stop.orderStatus.remaining), (4, 4))
        self.b.ib.cancelOrder.assert_not_called()
        # Later execution of the original four contracts leaves the added lots free to close.
        add = self.placed[0]
        fill = self.execution(qty=2, perm=901, side="BOT", exec_id="add.01")
        add.order.permId, add.orderStatus.filled, add.fills = 901, 2, [fill]
        self.assertEqual(self.b._reservation_size(self.b._close_reservations[123]), 8)
        stop.orderStatus.status, stop.orderStatus.filled = "Filled", 4
        stop.fills = [self.execution(4)]
        self.b._check_close_reservation(123, 4)
        self.assertNotIn(123, self.b._close_reservations)

    def test_other_contract_can_sell_to_open_while_stop_limit_works(self):
        stop = self.attach_protective_stop()
        stop.order.orderType = "STP LMT"
        preview = self.b.preview(dict(self.settings, right="P", open_action="SELL"))
        order = self.b.buy(preview["token"])
        self.assertEqual((order["conid"], order["position_effect"]), (124, "OPEN_SHORT"))
        self.b.ib.cancelOrder.assert_not_called()

    def test_close_other_position_leaves_working_stop_alone(self):
        self.attach_protective_stop()
        self.b.ib.reqPositions.return_value = [self.position(4), self.position(2, "P")]
        self.assertEqual(self.b.close(124, 100)["quantity"], 2)
        self.b.ib.cancelOrder.assert_not_called()

    def test_quick_close_each_supported_stop_after_adding(self):
        for kind in ("STP", "STP LMT", "TRAIL", "TRAIL LIMIT"):
            with self.subTest(kind=kind):
                stop = self.attach_protective_stop()
                stop.order.orderType = kind
                self.holding(7)
                self.assertEqual(self.b.positions()[0]["protection_id"], 70)
                placed = len(self.placed)
                def cancel(o):
                    self.assertIs(o, stop.order)
                    self.assertEqual(len(self.placed), placed)
                    stop.orderStatus.status = "Cancelled"
                self.b.ib.cancelOrder.side_effect = cancel
                self.assertEqual(self.b.close(123, 100)["quantity"], 7)
                self.assertEqual(len(self.placed), placed + 1)

    def test_partial_stop_fill_during_cancel_counts_toward_requested_close(self):
        for percent, expected in ((50, 1), (100, 3)):
            with self.subTest(percent=percent):
                stop = self.attach_protective_stop()
                def cancel(o):
                    fill = self.execution(1)
                    stop.orderStatus.status, stop.orderStatus.filled = "Cancelled", 1
                    stop.fills = [fill]
                    self.b.ib.reqExecutions.return_value = [fill]  # Same fill via two transports.
                    self.holding(3)
                self.b.ib.cancelOrder.side_effect = cancel
                self.assertEqual(self.b.close(123, percent, protection_id=70)["quantity"], expected)

    def test_one_click_replaces_old_stop_with_new_full_cost_even_if_lower(self):
        for kind in ("STP", "STP LMT", "TRAIL", "TRAIL LIMIT"):
            with self.subTest(kind=kind):
                stop = self.attach_protective_stop()
                stop.order.orderType, stop.order.auxPrice = kind, .95
                self.holding(6).avgCost = 87.11
                def cancel(order):
                    self.assertIs(order, stop.order)
                    stop.orderStatus.status = "Cancelled"
                self.b.ib.cancelOrder.side_effect = cancel
                result = self.b.set_breakeven_stop(123)
                self.assertEqual((result["order_type"], result["quantity"], result["stop_price"], result["tif"]),
                                 ("STP", 6, .88, "DAY"))

    def test_one_click_after_manual_stop_cancel_uses_latest_average(self):
        stop = self.attach_protective_stop()
        stop.orderStatus.status = "Cancelled"
        self.holding(6).avgCost = 92.10
        order = self.b.set_breakeven_stop(123)
        self.assertEqual((order["quantity"], order["stop_price"]), (6, .93))
        self.b.ib.cancelOrder.assert_not_called()

    def test_one_click_repeated_at_same_quantity_and_price_keeps_original(self):
        stop = self.attach_protective_stop()
        self.holding(4).avgCost = 80
        result = self.b.set_breakeven_stop(123)
        self.assertTrue(result["stop_unchanged"])
        self.assertEqual(result["order_id"], stop.order.orderId)
        self.b.ib.cancelOrder.assert_not_called()
        self.b.ib.placeOrder.assert_not_called()

    def test_one_click_uses_reconciled_size_and_cost_after_intervening_fill(self):
        stop = self.attach_protective_stop()
        self.holding(6).avgCost = 83
        def partial(_):
            self.b.ib.placeOrder.assert_not_called()
            stop.orderStatus.status, stop.orderStatus.filled = "Cancelled", 2
            stop.fills = [self.execution(2)]
            self.holding(4).avgCost = 84.2
        self.b.ib.cancelOrder.side_effect = partial
        order = self.b.set_breakeven_stop(123)
        self.assertEqual((order["quantity"], order["stop_price"]), (4, .85))

    def test_one_click_full_stop_fill_leaves_flat_account_without_new_sell(self):
        stop = self.attach_protective_stop()
        def filled(_):
            stop.orderStatus.status, stop.orderStatus.filled = "Filled", 4
            stop.fills = [self.execution(4)]
            self.holding(0)
        self.b.ib.cancelOrder.side_effect = filled
        self.assertTrue(self.b.set_breakeven_stop(123)["closed_by_stop"])
        self.b.ib.placeOrder.assert_not_called()

    def test_one_click_rejects_stale_bid_foreign_or_non_stop_before_cancel(self):
        stop = self.attach_protective_stop()
        self.b._quote.return_value = {"bid": .99}
        with self.assertRaisesRegex(ValueError, "买一"):
            self.b.set_breakeven_stop(123)
        self.b._quote.return_value = {"bid": 1.09}
        for client, kind in ((32, "STP"), (31, "LMT")):
            stop.order.clientId, stop.order.orderType = client, kind
            with self.assertRaises(BrokerError):
                self.b.set_breakeven_stop(123)
        self.b.ib.cancelOrder.assert_not_called()
        self.b.ib.placeOrder.assert_not_called()

    def test_one_click_price_falls_after_cancel_reports_protection_gap(self):
        stop = self.attach_protective_stop()
        def cancel(_):
            stop.orderStatus.status = "Cancelled"
            self.b._quote.return_value = {"bid": .98}
        self.b.ib.cancelOrder.side_effect = cancel
        with self.assertRaisesRegex(BrokerError, "旧止损已结束.*买一"):
            self.b.set_breakeven_stop(123)
        self.b.ib.placeOrder.assert_not_called()

    def test_one_click_only_cancels_stops_on_selected_contract(self):
        stop = self.attach_protective_stop()
        untouched = copy.deepcopy(stop)
        untouched.contract, untouched.order.orderId, untouched.order.permId = self.contracts["P"].contract, 71, 701
        self.b.ib.trades.return_value = [stop, untouched]
        self.b.ib.reqAllOpenOrders.side_effect = lambda: [t for t in (stop, untouched) if not t.isDone()]
        self.b.ib.cancelOrder.side_effect = lambda _: setattr(stop.orderStatus, "status", "Cancelled")
        self.holding(6).avgCost = 82
        order = self.b.set_breakeven_stop(123)
        self.assertEqual((order["conid"], order["quantity"], order["stop_price"]), (123, 6, .82))
        self.b.ib.cancelOrder.assert_called_once_with(stop.order)
        self.assertFalse(untouched.isDone())

    def test_one_click_whatif_warning_keeps_original_protection(self):
        self.attach_protective_stop()
        self.holding(6).avgCost = 85
        self.state.warningText = "rejected"
        with self.assertRaisesRegex(BrokerError, "预检"):
            self.b.set_breakeven_stop(123)
        probe = self.b.ib.whatIfOrder.call_args.args[1]
        self.assertEqual((probe.orderType, probe.totalQuantity, probe.auxPrice), ("STP", 6, .85))
        self.b.ib.cancelOrder.assert_not_called()
        self.b.ib.placeOrder.assert_not_called()

    def test_one_click_handles_missing_ack_and_new_conflicting_sell(self):
        stop = self.attach_protective_stop()
        self.b.CANCEL_WAIT_SECONDS = 0
        with self.assertRaisesRegex(BrokerError, "确认"):
            self.b.set_breakeven_stop(123)
        self.b.ib.placeOrder.assert_not_called()
        self.b.CANCEL_WAIT_SECONDS = 1
        other = self.closing_trade(total=1, filled=0, status="Submitted", perm=701)
        other.order.orderId = 71
        def cancel(_):
            stop.orderStatus.status = "Cancelled"
            self.b.ib.reqAllOpenOrders.side_effect = lambda: [other]
        self.b.ib.cancelOrder.side_effect = cancel
        with self.assertRaisesRegex(BrokerError, "旧止损已结束"):
            self.b.set_breakeven_stop(123)
        self.b.ib.placeOrder.assert_not_called()

    def test_previous_stop_fill_is_not_deducted_again_during_close(self):
        trade = self.attach_protective_stop()
        prior = self.execution(1, exec_id="prior.01")
        trade.orderStatus.filled, trade.orderStatus.remaining, trade.fills = 1, 3, [prior]
        self.holding(3)
        def cancel(_):
            new = self.execution(1, exec_id="new.01")
            trade.orderStatus.status, trade.orderStatus.filled = "Cancelled", 2
            self.b.ib.reqExecutions.return_value = [prior, new]
            self.holding(2)
        self.b.ib.cancelOrder.side_effect = cancel
        self.assertEqual(self.b.close(123, 100, protection_id=70)["quantity"], 2)

    def test_stale_order_status_disagrees_with_execution_download_after_cancel(self):
        stop = self.attach_protective_stop()
        self.b.CANCEL_WAIT_SECONDS = .05
        self.b.ib.reqExecutions.return_value = [self.execution(1)]
        self.b.ib.cancelOrder.side_effect = lambda o: setattr(stop.orderStatus, "status", "Cancelled")
        with self.assertRaisesRegex(BrokerError, "未在时限内确认"):
            self.b.close(123, 100, protection_id=70)
        self.b.ib.placeOrder.assert_not_called()

    def test_full_fill_while_cancelling_does_not_submit_another_sell(self):
        stop = self.attach_protective_stop()
        def cancel(o):
            stop.orderStatus.status, stop.orderStatus.filled = "Filled", 4
            self.b.ib.reqExecutions.return_value = [self.execution(4)]
            self.holding(0)
        self.b.ib.cancelOrder.side_effect = cancel
        outcome = self.b.close(123, 100, protection_id=70)
        self.assertTrue(outcome["closed_by_stop"])
        self.assertEqual(outcome["filled_by_stop"], 4)
        self.b.ib.placeOrder.assert_not_called()

    def test_manual_stop_or_mixed_exit_orders_are_never_auto_cancelled(self):
        stop = self.attach_protective_stop()
        stop.order.orderRef = "manual"
        self.assertNotIn("protection_id", self.b.positions()[0])
        self.assertFalse(self.b._opening_blocked(123))
        with self.assertRaises(BrokerError):
            self.b.close(123, 100, protection_id=70)
        stop.order.orderRef = "QOPT1:PS:test"
        other = self.closing_trade(total=1, filled=0, status="Submitted", perm=701)
        other.order.orderId = 71
        self.b.ib.reqAllOpenOrders.side_effect = lambda: [stop, other]
        with self.assertRaises(BrokerError):
            self.b.close(123, 100, protection_id=70)
        self.b.ib.cancelOrder.assert_not_called()
        self.b.ib.placeOrder.assert_not_called()

    def test_multiple_owned_stops_only_cancel_for_selected_contract(self):
        first = self.attach_protective_stop()
        first.order.totalQuantity = first.orderStatus.remaining = 2
        second = copy.deepcopy(first)
        second.order.orderId, second.order.permId, second.order.orderType = 71, 701, "TRAIL LIMIT"
        foreign_contract = copy.deepcopy(first)
        foreign_contract.contract = self.contracts["P"].contract
        foreign_contract.order.orderId = 72
        orders = [first, second, foreign_contract]
        self.b.ib.trades.return_value = orders
        self.b.ib.reqAllOpenOrders.side_effect = lambda: [t for t in orders if not t.isDone()]
        self.b.ib.cancelOrder.side_effect = lambda o: setattr(next(t for t in orders if t.order is o).orderStatus,
                                                               "status", "Cancelled")
        ids = self.b.positions()[0]["protection_id"]
        self.assertEqual(ids, (70, 71))
        self.assertEqual(self.b.close(123, 100, protection_id=ids)["quantity"], 4)
        self.assertEqual([c.args[0].orderId for c in self.b.ib.cancelOrder.call_args_list], [70, 71])
        self.assertFalse(foreign_contract.isDone())

    def test_toolbar_waits_for_stop_cancel_before_partial_market_close(self):
        trade = self.attach_protective_stop()
        self.assertEqual(self.b.positions()[0]["protection_id"], 70)
        def cancel(order):
            self.b.ib.placeOrder.assert_not_called()
            self.assertIs(order, trade.order)
            trade.orderStatus.status = "Cancelled"
        self.b.ib.cancelOrder.side_effect = cancel
        result = self.b.close(123, 50, protection_id=70)
        self.assertEqual((result["action"], result["quantity"]), ("SELL", 2))
        self.b.ib.cancelOrder.assert_called_once()
        self.b.ib.placeOrder.assert_called_once()

    def test_ticket_does_not_cancel_before_confirmation_and_keeps_limit(self):
        trade = self.attach_protective_stop()
        preview = self.b.preview_close(123, 100, "LMT", 1.35, protection_id=70)
        self.b.ib.cancelOrder.assert_not_called()
        self.b.ib.cancelOrder.side_effect = lambda _: setattr(trade.orderStatus, "status", "Cancelled")
        result = self.b.close_confirm(preview["token"])
        self.assertEqual((result["order_type"], result["limit_price"]), ("LMT", 1.35))
        with self.assertRaises(BrokerError):
            self.b.close_confirm(preview["token"])
        self.b.ib.placeOrder.assert_called_once()

    def test_changed_stop_or_fill_during_cancel_never_sends_second_sell(self):
        trade = self.attach_protective_stop()
        preview = self.b.preview_close(123, protection_id=70)
        trade.order.auxPrice = .90
        with self.assertRaises(BrokerError):
            self.b.close_confirm(preview["token"])
        self.b.ib.cancelOrder.assert_not_called()
        def cancel(_):
            trade.orderStatus.status, trade.orderStatus.filled = "Cancelled", 1
            trade.fills = [self.execution(1)]
        self.b.ib.cancelOrder.side_effect = cancel
        with self.assertRaisesRegex(BrokerError, "部分成交"):
            self.b.close(123, 100, protection_id=70)
        self.b.ib.placeOrder.assert_not_called()

    def test_missing_cancel_ack_keeps_protection_from_double_close(self):
        self.attach_protective_stop()
        self.b.CANCEL_WAIT_SECONDS = 0
        with self.assertRaisesRegex(BrokerError, "未在时限内确认"):
            self.b.close(123, 100, protection_id=70)
        self.b.ib.placeOrder.assert_not_called()

    def test_long_resting_limit_close_preserves_price_and_original_quantity(self):
        self.holding(5)
        preview = self.b.preview_close(123, 50, "LMT", 2.)
        self.assertEqual((preview["action"], preview["quantity"], preview["position_quantity"]), ("SELL", 2, 5))
        self.b.ib.placeOrder.assert_not_called()
        result = self.b.close_confirm(preview["token"])
        self.assertEqual((result["action"], result["quantity"], result["limit_price"]), ("SELL", 2, 2.))
        with self.assertRaisesRegex(BrokerError, "失效"):
            self.b.close_confirm(preview["token"])
        self.assertEqual(len(self.placed), 1)

    def test_blank_limit_close_seeds_midpoint_without_transmission(self):
        self.holding(2)
        preview = self.b.preview_close(123, order_mode="LMT")
        self.assertEqual(preview["limit_price"], 1.05)
        self.assertTrue(preview["limit_seeded"])
        self.b.ib.placeOrder.assert_not_called()

    def test_short_limit_cover_can_rest_below_ask_without_becoming_long(self):
        self.holding(-4)
        preview = self.b.preview_close(123, 50, "LMT", .95)
        result = self.b.close_confirm(preview["token"])
        self.assertEqual((result["action"], result["quantity"], result["limit_price"]), ("BUY", 2, .95))
        self.assertEqual(result["position_effect"], "CLOSE_SHORT")

    def test_close_rejects_changed_position_after_preview_and_whatif(self):
        for after_whatif in (False, True):
            with self.subTest(after_whatif=after_whatif):
                self.holding(5)
                preview = self.b.preview_close(123, 50, "MKT")
                if after_whatif:
                    def whatif(*_):
                        self.holding(4)
                        return self.state
                    self.b.ib.whatIfOrder.side_effect = whatif
                else:
                    self.holding(4)
                with self.assertRaisesRegex(BrokerError, "持仓数量"):
                    self.b.close_confirm(preview["token"])
                self.b.ib.placeOrder.assert_not_called()

    def test_expired_close_preview_and_quote_failure_never_send(self):
        self.holding(5)
        preview = self.b.preview_close(123)
        data, detail, created = self.b._close_tokens[preview["token"]]
        self.b._close_tokens[preview["token"]] = data, detail, created - 11
        with self.assertRaisesRegex(BrokerError, "有效期"):
            self.b.close_confirm(preview["token"])
        preview = self.b.preview_close(123)
        self.b._quote.side_effect = BrokerError("实时盘口无效")
        with self.assertRaisesRegex(BrokerError, "实时盘口"):
            self.b.close_confirm(preview["token"])
        self.b.ib.placeOrder.assert_not_called()

    def test_resync_rehydrates_fill_missing_from_retained_trade(self):
        trade = self.reservation()
        self.assertEqual(trade.fills, [])
        self.b._check_close_reservation(123, 3)
        self.assertNotIn(123, self.b._close_reservations)
        self.assertEqual(len(trade.fills), 1)
        self.assertEqual(self.b.ib.reqExecutions.call_args.args[0].acctCode, self.b.account)
        self.b.ib.placeOrder.assert_not_called()

    def test_completed_order_rebinds_stale_submitted_trade_without_resending(self):
        stale = self.closing_trade(status="Submitted", filled=0)
        complete = self.closing_trade(status="Filled", filled=0)
        self.reservation(stale, completed=[complete])
        self.b._check_close_reservation(123, 3)
        self.assertEqual(stale.orderStatus.status, "Filled")
        self.assertEqual(stale.orderStatus.filled, 2)
        self.assertNotIn(123, self.b._close_reservations)
        self.b.ib.placeOrder.assert_not_called()

    def test_resync_never_unlocks_before_position_deduction(self):
        self.reservation(observed=5)
        with self.assertRaisesRegex(BrokerError, "尚未确认"):
            self.b._check_close_reservation(123, 5)
        self.assertIn(123, self.b._close_reservations)
        self.b.ib.placeOrder.assert_not_called()

    def test_resync_rejects_wrong_account_perm_id_side_or_contract(self):
        for change in ("account", "perm", "side", "contract"):
            with self.subTest(change=change):
                fill = self.execution(account="DU999" if change == "account" else None,
                                      perm=999 if change == "perm" else 700,
                                      side="BOT" if change == "side" else "SLD")
                if change == "contract":
                    fill = fill._replace(contract=self.contracts["P"].contract)
                self.reservation(fills=[fill])
                self.b._close_sync_at = -1e9
                with self.assertRaises(BrokerError):
                    self.b._check_close_reservation(123, 3)
                self.assertIn(123, self.b._close_reservations)

    def test_active_server_order_cannot_unlock_even_with_filled_local_status(self):
        trade = self.reservation()
        active = self.closing_trade(status="Submitted", filled=1)
        self.b.ib.reqAllOpenOrders.return_value = [active]
        with self.assertRaises(BrokerError):
            self.b._check_close_reservation(123, 3)
        self.assertEqual(trade.fills, [])

    def test_duplicate_and_corrected_exec_ids_are_not_summed(self):
        original = self.execution(qty=2, exec_id="fill.01")
        correction = self.execution(qty=1, exec_id="fill.02")
        self.reservation(fills=[original, original, correction], observed=3)
        with self.assertRaises(BrokerError):
            self.b._check_close_reservation(123, 3)
        self.assertIn(123, self.b._close_reservations)

    def test_partial_fill_cancel_uses_completed_filled_quantity_and_cumulative(self):
        stale = self.closing_trade(total=3, filled=0, status="Submitted")
        complete = self.closing_trade(total=3, filled=0, status="Cancelled")
        complete.order.filledQuantity = 1
        self.reservation(stale, completed=[complete], fills=[self.execution(1)], observed=4)
        self.b._check_close_reservation(123, 4)
        self.assertNotIn(123, self.b._close_reservations)
        self.assertEqual(stale.orderStatus.filled, 1)

    def test_resync_failure_retains_reservation_and_rate_limits_retries(self):
        self.reservation()
        self.b.ib.reqExecutions.side_effect = TimeoutError("mock")
        for _ in range(2):
            with self.assertRaises(BrokerError):
                self.b._check_close_reservation(123, 3)
        self.b.ib.reqExecutions.assert_called_once()
        self.assertIn(123, self.b._close_reservations)

    def test_cancelled_partial_fill_recovers_stale_nonzero_status_from_full_execution_reply(self):
        stale = self.closing_trade(total=3, filled=1, status="Submitted")
        complete = self.closing_trade(total=3, filled=0, status="Cancelled")
        self.reservation(stale, completed=[complete], fills=[self.execution(2)], observed=3)
        self.b._check_close_reservation(123, 3)
        self.assertEqual(stale.orderStatus.filled, 2)
        self.assertNotIn(123, self.b._close_reservations)

    def test_history_is_account_mode_scoped_and_only_actual_executions(self):
        self.b.ib.reqExecutions.return_value = [self.execution(), self.execution(account="DU999", exec_id="other.01")]
        data = self.b.execution_history(refresh=True)
        self.assertEqual(len(data["rows"]), 1)
        self.b.ib.reqExecutions.assert_called_once()
        self.b.ib.placeOrder.assert_not_called()

    def test_successful_position_download_removes_absent_cached_positions_only_for_current_account(self):
        old = self.position(5)
        other = self.position(7)
        other.account = "DU999"
        cache = {self.b.account: {123: old}, "DU999": {123: other}}
        self.b.ib.wrapper = NS(positions=cache)
        self.b.ib.positions.side_effect = lambda account: list(cache[account].values())
        self.b.ib.reqPositions.return_value = [other]
        self.assertEqual(self.b._position_records(fresh=True, include_short=True), [])
        self.assertEqual(self.b.positions(), [])
        self.assertEqual(cache["DU999"][123].position, 7)

    def test_fresh_subscription_retains_updates_delivered_after_position_end(self):
        opening = self.position(1)
        latest = self.position(2)
        cache = {self.b.account: {}}
        self.b.ib.wrapper = NS(positions=cache)

        def download():
            self.b.ib.client.cancelPositions.assert_called_once()
            self.b._on_position_update(latest)
            return [opening]

        self.b.ib.reqPositions.side_effect = download
        self.assertEqual(self.b._position_records(fresh=True), [latest])
        self.assertIs(cache[self.b.account][123], latest)
        self.assertIsNone(self.b._position_download_updates)

    def test_fill_rechecks_positions_automatically_without_order_submission(self):
        self.b._position_records = Mock(return_value=[self.position(1)])
        with patch("ib_broker.time.monotonic", return_value=400):
            self.b._on_execution(self.closing_trade(), self.execution(1))
            self.b.pump()
            self.b._position_records.assert_not_called()
        for tick in (401, 404, 405):
            with patch("ib_broker.time.monotonic", return_value=tick):
                self.b.pump()
        self.assertEqual(self.b._position_records.call_count, 2)
        self.assertEqual(self.b._position_sync_times, [])
        self.b.ib.placeOrder.assert_not_called()

    def test_opening_fill_retries_empty_download_until_contract_arrives(self):
        position = self.position(1)
        self.b.ib.reqPositions.side_effect = [[], [], [position]]
        self.b.ib.wrapper = NS(positions={self.b.account: {}})
        with patch("ib_broker.time.monotonic", return_value=400):
            self.b._on_execution(self.closing_trade(action="BUY"), self.execution(1, side="BOT"))
        for tick in (401, 404):
            with patch("ib_broker.time.monotonic", return_value=tick):
                self.b.pump()
        self.assertTrue(self.b._unconfirmed_opens)
        self.assertFalse(self.b.ib.wrapper.positions[self.b.account])
        with patch("ib_broker.time.monotonic", return_value=415):
            self.b.pump()
        self.assertFalse(self.b._unconfirmed_opens)
        self.assertFalse(self.b._position_sync_times)
        self.assertEqual(self.b.ib.wrapper.positions[self.b.account][position.contract.conId], position)
        self.b.ib.placeOrder.assert_not_called()

    def test_unconfirmed_position_can_arrive_after_automatic_retries_end(self):
        self.b.ib.wrapper = NS(positions={self.b.account: {}})
        self.b.ib.reqPositions.return_value = []
        with patch("ib_broker.time.monotonic", return_value=400):
            self.b._on_order_status(self.closing_trade(action="BUY"))
        for tick in (401, 404, 520):
            with patch("ib_broker.time.monotonic", return_value=tick):
                self.b.pump()
        self.assertFalse(self.b._position_sync_times)
        self.assertTrue(self.b._unconfirmed_opens)
        position = self.position(1)
        self.b.ib.reqPositions.return_value = [position]
        self.assertEqual(self.b._position_records(fresh=True), [position])
        self.assertFalse(self.b._unconfirmed_opens)
        self.b.ib.placeOrder.assert_not_called()

    def test_unconfirmed_short_accepts_only_its_account_and_side(self):
        trade = self.closing_trade(action="SELL")
        trade.order.orderRef = "QOPT1:SO:test"
        self.b._on_order_status(trade)
        position = self.position(-1)
        other = copy.copy(position)
        other.account = "DU999"
        self.b._on_position_update(other)
        self.b._on_position_update(self.position(1))
        self.assertTrue(self.b._unconfirmed_opens)
        self.b._on_position_update(position)
        self.assertFalse(self.b._unconfirmed_opens)

    def test_failed_position_download_never_clears_existing_cache(self):
        old = self.position(5)
        cache = {self.b.account: {123: old}}
        self.b.ib.wrapper = NS(positions=cache)
        self.b.ib.reqPositions.side_effect = TimeoutError("mock")
        with self.assertRaises(TimeoutError):
            self.b._position_records(fresh=True, include_short=True)
        self.assertEqual(cache[self.b.account][123].position, 5)

    def test_confirmed_inactive_rejection_recovers_only_after_no_order_no_fill_and_same_position(self):
        trade = self.closing_trade(total=2, filled=0, status="Inactive")
        self.reservation(trade, observed=5, fills=[])
        self.b._on_error(70, 201, "Order rejected")
        self.b._check_close_reservation(123, 5)
        self.assertNotIn(123, self.b._close_reservations)
        self.assertEqual(self.b._open_orders(), [])
        self.b.ib.placeOrder.assert_not_called()

    def test_inactive_without_definitive_rejection_does_not_unlock(self):
        trade = self.closing_trade(total=2, filled=0, status="Inactive")
        self.reservation(trade, observed=5, fills=[])
        with self.assertRaises(BrokerError):
            self.b._check_close_reservation(123, 5)
        self.assertIn(123, self.b._close_reservations)

    def test_rejection_error_does_not_release_a_still_server_working_order(self):
        trade = self.closing_trade(total=2, filled=0, status="Inactive")
        self.reservation(trade, observed=5, fills=[])
        self.b._on_error(70, 201, "Amend rejected")
        self.b.ib.reqAllOpenOrders.return_value = [self.closing_trade(total=2, filled=0, status="Submitted")]
        with self.assertRaises(BrokerError):
            self.b._check_close_reservation(123, 5)
        self.assertIn(123, self.b._close_reservations)

    def test_position_download_last_zero_callback_wins_and_cannot_create_close(self):
        first, last = self.position(5), self.position(0)
        cache = {self.b.account: {123: first}}
        self.b.ib.wrapper = NS(positions=cache)
        self.b.ib.reqPositions.return_value = [first, last]
        self.assertEqual(self.b._position_records(fresh=True, include_short=True), [])
        self.assertEqual(cache[self.b.account], {})
        with self.assertRaisesRegex(BrokerError, "没有该"):
            self.b.preview_close(123)
        self.b.ib.placeOrder.assert_not_called()

    def test_position_download_last_reduction_wins_without_duplicate_rows(self):
        first, last = self.position(5), self.position(3)
        other = self.position(9)
        other.account = "DU999"
        cache = {self.b.account: {}, "DU999": {123: other}}
        self.b.ib.wrapper = NS(positions=cache)
        self.b.ib.reqPositions.return_value = [first, other, last]
        rows = self.b._position_records(fresh=True, include_short=True)
        self.assertEqual([p.position for p in rows], [3])
        self.assertEqual(cache[self.b.account][123].position, 3)
        self.assertEqual(cache["DU999"][123].position, 9)

    def test_reusing_broker_after_socket_disconnect_clears_session_but_keeps_history(self):
        store = self.b._get_history_store()
        self.b._tickers[123] = object()
        self.b._close_reservations[123] = self.closing_trade(), 5
        self.b._short_margin_hold = {"old": True}
        self.b._cover_cash_hold = {"old": True}
        self.b._priority_quote_ids = {123: time.monotonic() + 1000}
        connected = [False]
        self.b.ib.isConnected.side_effect = lambda: connected[0]
        self.b.ib.connect.side_effect = lambda *args, **kwargs: connected.__setitem__(0, True)
        self.b.ib.managedAccounts.return_value = ["DU456"]
        self.b.ib.reqPositions.return_value = []
        self.b.ib.positions.return_value = []
        self.b.connect(account="DU456")
        self.assertEqual(self.b.account, "DU456")
        self.assertEqual(self.b._tickers, {})
        self.assertEqual(self.b._close_reservations, {})
        self.assertIsNone(self.b._short_margin_hold)
        self.assertIsNone(self.b._cover_cash_hold)
        self.assertEqual(self.b._priority_quote_ids, {})
        self.assertIs(self.b._get_history_store(), store)
        self.b.ib.reqAllOpenOrders.assert_called()
        self.b.ib.reqPositions.assert_called()
        self.b.ib.placeOrder.assert_not_called()
        self.b.ib.cancelOrder.assert_not_called()
        # This one test intentionally exercises the mocked connect API.
        self.b.ib.connect.reset_mock()

    def test_reconnect_reloads_server_working_order_and_still_blocks_duplicate_close(self):
        trade = self.closing_trade(total=2, filled=0, status="Submitted")
        connected = [False]
        self.b.ib.isConnected.side_effect = lambda: connected[0]
        self.b.ib.connect.side_effect = lambda *args, **kwargs: connected.__setitem__(0, True)
        self.b.ib.managedAccounts.return_value = [self.b.account]
        self.b.ib.reqAllOpenOrders.return_value = [trade]
        self.holding(5)
        self.b.connect(account=self.b.account)
        with self.assertRaisesRegex(BrokerError, "未完成订单"):
            self.b.preview_close(123)
        self.b.ib.cancelOrder.assert_not_called()
        self.b.ib.placeOrder.assert_not_called()
        self.b.ib.connect.reset_mock()


if __name__ == "__main__":
    unittest.main()
