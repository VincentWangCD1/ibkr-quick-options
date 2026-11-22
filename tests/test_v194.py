"""Broker, order presentation and protection regressions using synthetic fills and quotes."""
import time
import unittest
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace as NS
from unittest.mock import Mock

import tests.test_v160_broker as fixtures
from ib_broker import IBBroker, BrokerError
from contract_display import order_display
from core import ValidationError
from ib_async import Trade, LimitOrder, OrderStatus
from ib_async.objects import TradeLogEntry


class LifecycleTests(unittest.TestCase):
    setUp = fixtures.BrokerV160Tests.setUp
    tearDown = fixtures.BrokerV160Tests.tearDown
    place = fixtures.BrokerV160Tests.place
    position = fixtures.BrokerV160Tests.position

    def buy_and_sell(self):
        c = self.contracts["C"].contract
        stamp = datetime.now(timezone.utc)
        def fill(identifier, side, price, t):
            return NS(contract=c, execution=NS(execId=identifier, acctNumber=self.b.account, shares=1,
                      time=t, side=side, price=price), commissionReport=NS(execId=identifier, currency="USD", commission=1.05))
        bought = fill("buy.01", "BOT", 4.01, stamp)
        sold = fill("sell.01", "SLD", 3.73, stamp + timedelta(seconds=39))
        trade = Trade(contract=c, order=LimitOrder("BUY", 1, 4.01),
                      orderStatus=OrderStatus(status="Filled", filled=1, remaining=0))
        trade.fills = [bought]
        self.b._reserve_trade = trade
        self.b._reserve_cash_baseline = 933.92
        self.b._reserve_usd_baseline = 1174.02
        self.b._debit_reserve = 423.09
        self.financial.update(usd_cash=1143.92, cash=933.92)
        self.b.ib.fills.return_value = [bought, sold, sold]
        return sold

    def test_rapid_round_trip_releases_reserve_to_verified_broker_cap(self):
        self.buy_and_sell()
        self.assertAlmostEqual(self.b._sync_cash(force=True), 933.92)
        self.assertEqual(self.b._debit_reserve, 0)
        self.assertAlmostEqual(self.b._sync_cash(force=True), 933.92)

    def test_flat_round_trip_with_zero_net_ledger_change(self):
        sale = self.buy_and_sell()
        sale.execution.price = 4.031  # 403.10 proceeds minus 1.05 sale fee = 402.05 opening debit
        self.financial.update(usd_cash=1174.02)
        self.assertEqual(self.b._sync_cash(force=True), 933.92)

    def test_settled_ceiling_cannot_hide_posted_purchase(self):
        self.buy_and_sell()
        self.financial.update(usd_cash=771.97, cash=771.97)
        self.assertAlmostEqual(self.b._sync_cash(force=True), 771.97)
        self.assertEqual(self.b._debit_reserve, 0)

    def test_unrelated_sale_or_unreported_fee_cannot_release_reserve(self):
        for cause in ("account", "contract", "fee", "cash", "unchanged"):
            with self.subTest(cause=cause):
                sale = self.buy_and_sell()
                if cause == "account":
                    sale.execution.acctNumber = "another"
                elif cause == "contract":
                    sale.contract = self.contracts["P"].contract
                elif cause == "fee":
                    sale.commissionReport.execId = ""
                elif cause == "cash":
                    self.financial["usd_cash"] += 1
                else:
                    self.financial["usd_cash"] = 1174.02
                self.assertAlmostEqual(self.b._sync_cash(force=True), 510.83)

    def setup_position(self, bid):
        p = self.position(1)
        p.avgCost = 3.74
        self.b.ib.reqPositions.return_value = self.b.ib.positions.return_value = [p]
        self.b._quote.return_value = {"bid": bid, "ask": None}
        return p

    def test_cost_stop_legal_tick_and_one_submission_after_filled_entry(self):
        p = self.setup_position(.06)
        self.b.ib.trades.return_value = [Trade(contract=p.contract, order=LimitOrder("BUY", 1, .03,
            account=self.b.account, orderRef="QOPT1:edited-entry"), orderStatus=OrderStatus(status="Filled", filled=1))]
        preview = self.b.prepare_cost_stop(123)
        self.assertEqual(preview["stop_price"], .04)
        self.assertEqual(len(self.placed), 0)
        self.b.close(123, 100, dict(type="STP", stop_price=.04, tif="DAY"), cost_guard=preview)
        self.assertEqual(len(self.placed), 1)
        self.assertEqual((self.placed[0].order.action, self.placed[0].order.auxPrice), ("SELL", .04))

    def test_cost_stop_rechecks_bid_and_cost_after_confirmation(self):
        for changed in ("price", "cost", "quantity"):
            with self.subTest(changed=changed):
                p = self.setup_position(.06)
                preview = self.b.prepare_cost_stop(123)
                if changed == "cost":
                    p.avgCost = 3.90
                elif changed == "quantity":
                    p.position = 2
                else:
                    self.b._quote.return_value["bid"] = .03
                with self.assertRaises(ValidationError):
                    self.b.close(123, 100, dict(type="STP", stop_price=.04, tif="DAY"), cost_guard=preview)
                self.assertEqual(len(self.placed), 0)

    def test_cost_stop_already_underwater_explains_current_bid(self):
        self.setup_position(.01)
        with self.assertRaisesRegex(ValidationError, "0.04.*买一 0.01"):
            self.b.prepare_cost_stop(123)
        self.b.ib.whatIfOrder.assert_not_called()

    def test_only_correct_live_side_can_value_market_close(self):
        c = self.contracts["C"].contract
        ticker = NS(bid=.01, ask=.03, last=.02, bidSize=8, askSize=1, marketDataType=1, halted=0)
        self.b._subscribe = Mock(return_value=ticker)
        self.b._price_times[c.conId] = {1: time.monotonic(), 2: time.monotonic()-60}
        def quote(side):
            return IBBroker._quote(self.b, c, wait=0, required_side=side)
        self.assertEqual(quote("bid")["bid"], .01)
        self.assertIsNone(quote("bid")["ask"])
        for side in (None, "ask"):
            with self.assertRaises(BrokerError):
                quote(side)
        for attr, val in (("marketDataType", 3), ("bidSize", 0), ("halted", 1)):
            prev = getattr(ticker, attr)
            setattr(ticker, attr, val)
            with self.assertRaises(BrokerError):
                quote("bid")
            setattr(ticker, attr, prev)
        self.b._price_times[c.conId] = {1: time.monotonic()-60, 2: time.monotonic()}
        with self.assertRaises(BrokerError):
            quote("bid")

    def test_order_keeps_original_placement_and_fresh_quote_separate_from_limit(self):
        c = self.contracts["C"].contract
        t = Trade(contract=c, order=LimitOrder("BUY", 93, .01, account=self.b.account, clientId=self.b.client_id,
                  orderRef="QOPT1:test", orderId=51), orderStatus=OrderStatus(status="Submitted", remaining=93))
        stamp = datetime.now(timezone.utc)
        t.log = [TradeLogEntry(stamp, "PendingSubmit", "")]
        self.b.ib.trades.return_value = [t]
        self.b._position_quote = Mock(return_value=dict(bid=2.14, ask=2.16, last=2.20, bid_age=0, ask_age=0,
                                                       last_age=2, quote_type="实时订阅"))
        row = self.b._display_orders()[0]
        view = order_display(row, date(2026,12,18))
        self.assertEqual((row["limit_price"], view["book"], view["last"]), (.01, "2.14 / 2.16", "2.2"))
        self.assertIn("600 Call", view["contract"])
        self.assertEqual(view["dte"], "0 DTE")
        self.b._placement_times.clear()
        t.log = []
        self.assertEqual(self.b._display_orders()[0]["placed_at"], stamp.timestamp())
