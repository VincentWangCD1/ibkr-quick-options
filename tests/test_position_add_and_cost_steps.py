"""Position ticket regressions, with in-memory fills and mocked IB transport."""
import copy
from decimal import Decimal
from unittest import TestCase
from unittest.mock import Mock

from core import ValidationError
from demo_broker import DemoBroker
from exit_orders import cost_stop_quote
from ib_broker import BrokerError
from position_add import first_buy_cost
from tests import test_v160_broker as fixture, test_order_amendments as amendments


class AddTests(TestCase):
    setUp = fixture.BrokerV160Tests.setUp
    tearDown = fixture.BrokerV160Tests.tearDown
    position = fixture.BrokerV160Tests.position
    place = fixture.BrokerV160Tests.place

    def preview(self, method="lots", value=2, held=1, **request):
        self.b.ib.reqPositions.return_value = [self.position(held, "P")]
        return self.b.preview_add(124, held, dict(method=method, value=value, **request))

    def test_exact_put_independent_of_chain_and_single_use(self):
        p = self.preview()
        self.b._contracts.assert_not_called()
        self.b._selection_spot.assert_not_called()
        self.assertEqual((p["quantity"], p["contract"]["right"], p["limit_price"]), (2, "P", 1.05))
        self.b.ib.placeOrder.assert_not_called()
        self.b.buy(p["token"])
        self.assertEqual((self.placed[0].contract.conId, self.placed[0].order.totalQuantity), (124, 2))
        with self.assertRaises(BrokerError):
            self.b.buy(p["token"])
        self.assertEqual(len(self.placed), 1)

    def test_amount_and_original_principal_use_fees_and_floor(self):
        for p in (self.preview("amount", 211), self.preview("principal", 50, principal=422)):
            self.assertEqual((p["quantity"], p["requested_budget"], p["buffered_cost"]), (1, 211, 106))
        with self.assertRaisesRegex(ValidationError, "原始本金"):
            self.preview("principal", 50)

    def test_invalid_lots_and_budget(self):
        for method, value in (("lots", 0), ("lots", 1.5), ("lots", 6000), ("amount", "nan"), ("amount", 104)):
            with self.subTest(method=method, value=value), self.assertRaises(ValueError):
                self.preview(method, value)

    def test_cash_or_holding_changes_during_preflight(self):
        for change in ("reduced", "cost", "flat", "fee"):
            self.financial.update(cash=5000, usd_cash=5000, available_funds=5000)
            p = self.preview("amount", 213)
            def changed(*_):
                if change == "reduced":
                    self.financial.update(cash=200, usd_cash=200, available_funds=200)
                if change == "flat":
                    self.b.ib.reqPositions.return_value = []
                if change == "cost":
                    self.b.ib.reqPositions.return_value[0].avgCost = 60
                state = copy.deepcopy(self.state)
                if change == "fee":
                    state.commission = 10
                return state
            self.b.ib.whatIfOrder.side_effect = changed
            with self.subTest(change=change), self.assertRaises(BrokerError):
                self.b.buy(p["token"])
        self.b.ib.placeOrder.assert_not_called()

    def test_short_add_uses_incremental_margin(self):
        for method, value, args in (("lots", 2, {}), ("amount", 1100, {}),
                                    ("principal", 50, {"principal": 2200})):
            p = self.preview(method, value, -3, **args)
            self.assertEqual((p["action"], p["quantity"], p["position_quantity"]), ("SELL", 2, -3))
        self.b.buy(p["token"])
        self.assertEqual((self.placed[0].order.action, self.placed[0].contract.conId), ("SELL", 124))

    def test_spread_or_position_flip_does_not_become_a_new_open(self):
        p = self.preview(held=-1)
        self.b.ib.reqPositions.return_value = [self.position(1, "P")]
        with self.assertRaises(BrokerError):
            self.b.buy(p["token"])
        self.b.ib.placeOrder.assert_not_called()


class DemoTickets(TestCase):
    def setUp(self):
        self.b = DemoBroker()
        self.b.connect()
        self.b._quote = Mock(return_value=(Decimal(600), Decimal("0.99"), Decimal("1")))
        self.contract = self.b._contract("SPY", self.b.expirations("SPY")[0], "P", 600)
        self.conid = self.contract["conid"]
        self.b.buy(self.b.preview(dict(**self.contract, selection_mode="strike", max_contracts=1))["token"])

    def tearDown(self):
        self.b._history.close()

    def add(self, held, method, value, **kw):
        return self.b.preview_add(self.conid, held, dict(method=method, value=value, order_mode="LMT",
                                                        limit_price=1, **kw))

    def test_first_order_basis_does_not_grow_and_resets_after_flat(self):
        p = self.add(1, "principal", 100)
        self.assertEqual((p["principal"], p["quantity"]), (101, 1))
        self.b.buy(p["token"])
        self.assertEqual(self.add(2, "principal", 100)["principal"], 101)
        self.b.close(self.conid, 50)
        self.assertEqual(self.add(1, "principal", 100)["principal"], 101)
        self.b.close(self.conid, 100)
        self.b.buy(self.b.preview(dict(**self.contract, selection_mode="strike", max_contracts=2))["token"])
        self.assertEqual(self.add(2, "principal", 100)["principal"], 202)

    def test_one_click_after_fill_resets_stop_using_weighted_cost_and_whole_position(self):
        self.b._quote.return_value = Decimal(600), Decimal("1.40"), Decimal("1.42")
        old = self.b.set_breakeven_stop(self.conid)
        plan = self.b.preview_add(self.conid, 1, dict(method="lots", value=2, order_mode="LMT", limit_price=1.42))
        self.b.buy(plan["token"])
        self.assertEqual(self.b._pending_exits[old["order_id"]]["order"]["remaining"], 1)
        self.b._quote.return_value = Decimal(600), Decimal("1.80"), Decimal("1.82")
        new = self.b.set_breakeven_stop(self.conid)
        self.assertEqual((new["quantity"], new["stop_price"]), (3, 1.29))
        self.assertEqual(set(self.b._pending_exits), {new["order_id"]})
        self.assertTrue(self.b.set_breakeven_stop(self.conid)["stop_unchanged"])

    def test_raise_same_demo_stop_then_trigger_once(self):
        self.b._quote.return_value = Decimal(600), Decimal(3), Decimal("3.02")
        q = self.b.prepare_cost_stop(self.conid)
        stop = self.b.close(self.conid, 100, dict(type="STP", tif="DAY", stop_price=q["stop_price"]), cost_guard=q)
        for factor, price in ((1.5, 1.52), (2, 2.02)):
            q = self.b.prepare_cost_stop(self.conid, factor)
            self.assertEqual(q["old_stop"], stop["stop_price"])
            stop = self.b.raise_cost_stop(q)
            self.assertEqual((stop["stop_price"], len(self.b._pending_exits)), (price, 1))
            self.assertEqual(len(self.b.orders()), 2)
        self.b._quote.return_value = Decimal(600), Decimal(2), Decimal("2.02")
        self.b.pump()
        self.assertEqual(self.b.positions(), [])
        self.assertEqual(self.b.orders()[-1]["status"], "Filled (离线演示)")


class CostStepTests(TestCase):
    def test_grid_and_custom_factor(self):
        self.assertEqual(cost_stop_quote(103, 100, 1, 4, [(0, .05)], "1.75")["stop_price"], 1.85)
        for factor in ("0", "", "-1", "NaN", "inf"):
            with self.assertRaises(ValueError):
                cost_stop_quote(100, 100, 1, 3, [(0, .01)], factor)
        with self.assertRaisesRegex(ValidationError, "买一"):
            cost_stop_quote(100, 100, 1, 2, [(0, .01)], 2)

    def test_split_first_order_with_interleaved_fills_and_missing_fees(self):
        def row(perm, side, qty, px):
            return dict(source="API", perm_id=perm, action=side, quantity=qty, price=px,
                        multiplier=100, commission=1, currency="USD", commission_currency="USD")
        rows = [row(1, "BUY", 1, 2), row(2, "BUY", 1, 4), row(1, "BUY", 2, 3), row(3, "SELL", 1, 6)]
        self.assertEqual(first_buy_cost(rows, 3), 802)
        self.assertIsNone(first_buy_cost(rows, 4))
        rows[0]["commission"] = None
        self.assertIsNone(first_buy_cost(rows, 3))


class StopRaiseTests(TestCase):
    tearDown = amendments.AmendmentsTests.tearDown
    place = amendments.AmendmentsTests.place
    sleep = amendments.AmendmentsTests.sleep
    cancel = amendments.AmendmentsTests.cancel

    def setUp(self):
        amendments.AmendmentsTests.setUp(self)
        self.trade.order.action, self.trade.order.orderType, self.trade.order.auxPrice = "SELL", "STP", .1
        self.trade.order.tif = "DAY"
        self.b._position_records.return_value[0].avgCost = 10
        self.b._quote.return_value = {"bid": .35, "ask": .39}

    def test_step_up_same_id_and_quantity(self):
        q = self.b.prepare_cost_stop(123, 2)
        result = self.b.raise_cost_stop(q)
        self.assertEqual(result["modification"], "amend_acknowledged")
        self.assertEqual([(o.orderId, o.auxPrice, o.totalQuantity) for o in self.placements], [(7, .2, 10)])
        self.b.ib.cancelOrder.assert_not_called()
        with self.assertRaises(BrokerError):
            self.b.raise_cost_stop(q)

    def test_skip_external_or_partial_orders(self):
        self.trade.order.clientId = 123
        with self.assertRaises(BrokerError):
            self.b.prepare_cost_stop(123, 2)
        self.trade.order.clientId = 31
        self.trade.orderStatus.remaining = 9
        with self.assertRaises(BrokerError):
            self.b.prepare_cost_stop(123, 2)
        self.assertFalse(self.placements)

    def test_filled_or_changed_during_whatif_never_sends(self):
        q = self.b.prepare_cost_stop(123, 1.5)
        def fill(*_):
            self.trade.orderStatus.status = "Filled"
            self.b._position_records.return_value = []
            return self.b.ib.whatIfOrder.return_value
        self.b.ib.whatIfOrder.side_effect = fill
        with self.assertRaises(BrokerError):
            self.b.raise_cost_stop(q)
        self.assertFalse(self.placements)
