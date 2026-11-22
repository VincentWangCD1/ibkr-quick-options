import unittest
from core import affordable_size, budget_strikes
from demo_broker import DemoBroker
from ib_broker import IBBroker, BudgetScanPending
from types import SimpleNamespace
from unittest.mock import patch
import time
from contract_display import order_display
from privacy_view import mask_money


class BudgetAndDisplayTests(unittest.TestCase):
    def test_both_sides_scan_from_selected_otm(self):
        broker=DemoBroker()
        broker.connect()
        settings=dict(symbol="XSP",expiry=broker.expirations("XSP")[0],trading_class="XSP",
                      moneyness="OTM",depth=2,selection_mode="budget",percent=50,
                      budget_mode="amount",cash_amount=28,rounding_mode="floor",open_action="BUY")
        pair=broker.preview_pair(settings)
        for right,preview in pair.items():
            self.assertGreaterEqual(preview["budget_rank"],2)
            self.assertGreater(preview["quantity"],0)
            self.assertLessEqual(preview["buffered_cost"],28)
            self.assertEqual(preview["contract"]["right"],right)
        self.assertEqual(broker.snapshot()["orders"],[])
        pair=broker.preview_pair(dict(settings,cash_amount=1))
        self.assertTrue(all(p.get("error") for p in pair.values()))

    def test_budget_hard_cap_low_passive_limit_and_fees(self):
        args=dict(percent=50,min_commission=1,slippage_pct=5)
        self.assertEqual(affordable_size(100,.50,100,args,.01)["quantity"],0)
        self.assertEqual(affordable_size(100,.46,100,args,.01)["quantity"],1)
        self.assertEqual(affordable_size(100,.46,100,args,1)["quantity"],0)
        self.assertEqual(budget_strikes([99,98,100,101],100,"P",dict(depth=2,moneyness="OTM")),[(2,98)])

    def test_ib_scan_resumes_outward_and_discards_transient_lines(self):
        broker = IBBroker.__new__(IBBroker)
        broker.account, broker.mode = "TEST", "PAPER"
        broker._tickers, broker._price_times = {}, {}
        broker._sync_cash = lambda: 100
        contracts = [SimpleNamespace(contract=SimpleNamespace(strike=n, conId=n, multiplier="100")) for n in range(101, 151)]
        def subscribe(c):
            good = c.conId >= 140
            broker._tickers[c.conId] = SimpleNamespace(bid=.1, ask=.2 if good else 2, bidSize=1, askSize=1,
                                                       marketDataType=1, halted=0)
            broker._price_times[c.conId] = {1:time.monotonic(), 2:time.monotonic()}
        broker._subscribe = subscribe
        settings = dict(symbol="XSP", expiry="20261120", percent=50, selection_mode="budget")
        with patch("ib_broker.release_quote_stream", side_effect=lambda b,k:b._tickers.pop(k,None)):
            with self.assertRaises(BudgetScanPending):
                broker._budget_contract(contracts, 100, "C", settings)
            self.assertFalse(broker._tickers)
            item, rank = broker._budget_contract(contracts, 100, "C", settings)
            self.assertEqual((item.contract.conId,rank),(140,40))
            self.assertEqual(set(broker._tickers),{140})
            broker._quote = lambda *args, **kw: {"ask":.2, "bid":.1}
            self.assertEqual(broker._budget_contract(contracts,100,"C",settings)[1],40)

    def test_order_mid_requires_fresh_live_book(self):
        quote=dict(bid=2.61,ask=2.8,bid_age=0,ask_age=3,quote_type="实时订阅")
        def mid(**overrides):
            return order_display(dict(status="Submitted",quote=dict(quote,**overrides)))["midpoint"]
        self.assertEqual(mid(),"2.705")
        for change in (dict(ask=None),dict(bid=3),dict(ask_age=11),dict(quote_type="延迟"),dict(halted=True)):
            self.assertEqual(mid(**change),"—")

    def test_display_masks_totals_keeps_order_levels(self):
        value=mask_money("净值 $1,140.45 收益 -$2.52 限价 0.08 USD/份，盈亏 53.16%")
        self.assertNotIn("1,140",value)
        self.assertNotIn("2.52",value)
        self.assertIn("0.08 USD/份",value)
        self.assertIn("53.16%",value)
