"""Independent offline execution integration regressions; never connects IB."""
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock

from ib_broker import IBBroker, BrokerError
from trading_sessions import SessionDecision
from order_policy import protected_limit


class ExecutionReviewTests(unittest.TestCase):
    def test_estimated_spot_cannot_turn_into_rth_market_preview_during_quote_wait(self):
        broker = IBBroker.__new__(IBBroker)
        contract = NS(conId=12, symbol="SPX", lastTradeDateOrContractMonth="20261112",
                      right="C", tradingClass="SPXW", strike=6010, multiplier="100",
                      localSymbol="SPXW C6010", exchange="CBOE", currency="USD")
        detail = NS(contract=contract)
        broker._require = Mock()
        broker._contracts = Mock(return_value=[detail])
        broker._selection_spot = Mock(return_value=(6000, "SPY估算", True))
        broker._execution_detail = Mock(return_value=detail)
        # Time advances into RTH while _quote is waiting for a fresh book.
        broker._session = Mock(side_effect=[SessionDecision("extended", "", "LMT", True),
                                           SessionDecision("regular", "", "MKT", False)])
        broker._quote = Mock(return_value={"bid": 1, "ask": 1.1})
        broker._sync_cash = Mock(return_value=1000)
        broker._tokens, broker.mode, broker.account = {}, "PAPER", "DU1"
        broker._details = {}
        settings = dict(symbol="SPX", expiry="20261112", right="C", moneyness="OTM", depth=1, percent=50)
        with self.assertRaises(BrokerError):
            broker.preview(settings)
        self.assertEqual(broker._tokens, {})

    def test_limit_sizing_uses_rounded_limit_without_adding_slippage_twice(self):
        broker = IBBroker.__new__(IBBroker)
        limit = protected_limit("BUY", .23, 5, [(0, .05)])
        self.assertEqual(limit, .25)
        result = broker._plan_size(1000, {"ask": .23}, NS(multiplier="100"),
                                  dict(percent=50, slippage_pct=5, commission_per_contract=1,
                                       min_commission=1, max_contracts=100, rounding_mode="floor"),
                                  dict(order_type="LMT", limit_price=limit))
        self.assertEqual(result["quantity"], 19)
        self.assertEqual(result["buffered_cost"], 494)


if __name__ == "__main__":
    unittest.main()
