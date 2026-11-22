"""Offline account-wide history, Flex transport and automatic repricing checks."""
import copy
from datetime import date, datetime, timezone
import json
from pathlib import Path
import queue
import threading
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from auto_mid import AutoMidManager, policy
from flex_sync import fetch_reports, http_get, load_credentials, save_credentials, NoRedirect
from trade_history import TradeHistoryStore, HistoryError, period_dates, execution_gross
from history_cards import grouped_cards, quote_amount
from app import Worker, App
from test_v170_history import changed, flex_xml, NOW
from test_app_state import controller, Variable
from test_v170_ui import close_controller, preview


class AccountHistoryTests(unittest.TestCase):
    def setUp(self):
        self.store = TradeHistoryStore()
        self.addCleanup(self.store.close)

    def test_all_periods_calendar_boundaries(self):
        expected = dict(today=date(2026, 11, 19), **{"7d": date(2026, 11, 13), "30d": date(2026, 10, 21),
                        "60d": date(2026, 9, 21), "90d": date(2026, 8, 22), "mtd": date(2026, 11, 1), "ytd": date(2026, 1, 1)})
        for period, start in expected.items():
            self.assertEqual(period_dates(period, NOW), (start, date(2026, 11, 19)))
        boundary = datetime(2026, 1, 1, 1, tzinfo=timezone.utc)
        self.assertEqual(period_dates("ytd", boundary, "America/New_York"), (date(2025, 1, 1), date(2025, 12, 31)))
        self.assertEqual(period_dates("ytd", boundary), (date(2026, 1, 1), date(2026, 1, 1)))

    def test_all_assets_fractional_quantity_and_unknown_multiplier(self):
        for i, asset in enumerate(("STK", "OPT", "FUT", "CASH", "BOND", "FOP", "CFD", "CRYPTO")):
            self.store.record_execution("U123", "LIVE", changed(str(i), sec_type=asset, multiplier=None, quantity=.125))
        rows = self.store.history("U123", "LIVE", now=NOW)["rows"]
        self.assertEqual(len(rows), 8)
        self.assertEqual({r["sec_type"] for r in rows}, {"STK", "OPT", "FUT", "CASH", "BOND", "FOP", "CFD", "CRYPTO"})
        self.assertEqual(next(r["gross"] for r in rows if r["sec_type"] == "STK"), .15625)
        self.assertIsNone(next(r["gross"] for r in rows if r["sec_type"] == "FUT"))

    def test_reported_bond_value_is_used_without_percent_guessing(self):
        r = changed(sec_type="BOND", quantity=1000, price=98.5, multiplier=1, reported_gross=-985)
        self.assertEqual(execution_gross(r), 985)
        r.pop("reported_gross")
        self.assertIsNone(execution_gross(r))

    def test_api_flex_same_execution_enriches_without_duplicate(self):
        self.store.record_execution("U123", "LIVE", changed())
        self.store.import_flex_text(flex_xml(exchange="CBOE", orderType="LMT"), ".xml", "U123", "LIVE", "America/New_York")
        self.store.record_execution("U123", "LIVE", changed())
        rows = self.store.history("U123", "LIVE", now=NOW)["rows"]
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["source"], rows[0]["exchange"], rows[0]["order_type"]), ("FLEX", "CBOE", "LMT"))
        self.assertEqual(rows[0]["realized_pnl"], 12.8)

    def test_multi_report_parse_failure_imports_nothing(self):
        with self.assertRaises(HistoryError):
            self.store.import_flex_batch([(flex_xml(), ".xml"), (flex_xml(account="U999"), ".xml")], "U123", "LIVE", "America/New_York")
        self.assertEqual(self.store.history("U123", "LIVE", now=NOW)["rows"], [])

    def test_multi_report_conflict_rolls_back_whole_transaction(self):
        with self.assertRaises(HistoryError):
            self.store.import_flex_batch([(flex_xml(), ".xml"), (flex_xml(tradePrice="9"), ".xml")], "U123", "LIVE", "America/New_York")
        self.assertEqual(self.store.history("U123", "LIVE", now=NOW)["rows"], [])

    def test_empty_account_report_distinguished_from_wrong_query(self):
        empty = '<FlexStatement accountId="U123"><Trades/></FlexStatement>'
        result = self.store.import_flex_text(empty, ".xml", "U123", "LIVE", "America/New_York")
        self.assertEqual(result["imported"], 0)
        for invalid in (empty.replace("U123", "U999"), empty.replace("<Trades/>", ""), flex_xml(levelOfDetail="SUMMARY")):
            with self.assertRaises(HistoryError):
                self.store.import_flex_text(invalid, ".xml", "U123", "LIVE", "America/New_York")

    def test_grouped_cards_missing_pnl_not_zero_and_precision_preserved(self):
        rows = [dict(date="2026-11-19", timestamp="2", realized_pnl=-12.3, currency="USD"),
                dict(date="2026-11-19", timestamp="1", realized_pnl=None, currency="USD")]
        group = grouped_cards(rows)[0][1]
        self.assertEqual((group["totals"]["USD"], group["missing"]), (-12.3, 1))
        self.assertEqual(quote_amount(.000075), "$0.000075")
        self.assertEqual(quote_amount(2), "$2.00")
        self.assertEqual(quote_amount(None), "—")


class ImmediateEvent:
    def is_set(self):
        return False
    def wait(self, seconds):
        return False


class FlexTests(unittest.TestCase):
    def fetch(self, get, **kwargs):
        return fetch_reports("123456789", "123", kwargs.pop("period", "today"), "Asia/Shanghai", "America/New_York",
                             cancel=kwargs.pop("cancel", ImmediateEvent()), get=get, now=kwargs.pop("now", NOW), **kwargs)

    def test_fixed_endpoints_and_wait_until_report_ready(self):
        get = Mock(side_effect=['<FlexStatementResponse><Status>Success</Status><ReferenceCode>99</ReferenceCode><url>https://bad.test</url></FlexStatementResponse>',
                               '<FlexStatementResponse><ErrorCode>1019</ErrorCode></FlexStatementResponse>', flex_xml()])
        self.assertEqual(self.fetch(get), [flex_xml()])
        self.assertEqual([call.args[0] for call in get.call_args_list], ["SendRequest", "GetStatement", "GetStatement"])
        self.assertEqual(get.call_args_list[0].args[1]["fd"], "20261118")
        self.assertEqual(get.call_args_list[0].args[1]["td"], "20261118")
        self.assertEqual(get.call_args_list[1].args[1]["q"], "99")

    def test_leap_year_ytd_splits_365_day_windows(self):
        get = Mock(side_effect=lambda endpoint, params: '<FlexStatementResponse><Status>Success</Status><ReferenceCode>99</ReferenceCode></FlexStatementResponse>' if endpoint == "SendRequest" else flex_xml())
        self.fetch(get, period="ytd", now=datetime(2024, 12, 31, 12, tzinfo=timezone.utc))
        windows = [c.args[1] for c in get.call_args_list if c.args[0] == "SendRequest"]
        self.assertEqual(len(windows), 2)
        for p in windows:
            self.assertLessEqual((datetime.strptime(p["td"], "%Y%m%d") - datetime.strptime(p["fd"], "%Y%m%d")).days, 364)
        self.assertEqual([(p["fd"], p["td"]) for p in windows],
                         [("20240101", "20241230"), ("20231231", "20231231")])

    def test_cancellation_before_network(self):
        event, get = threading.Event(), Mock()
        event.set()
        with self.assertRaisesRegex(HistoryError, "取消"):
            self.fetch(get, cancel=event)
        get.assert_not_called()

    def test_service_error_does_not_echo_response_or_token(self):
        with self.assertRaises(HistoryError) as raised:
            self.fetch(Mock(return_value='<Response><ErrorCode>1012</ErrorCode><ErrorMessage>123456789</ErrorMessage></Response>'))
        self.assertNotIn("123456789", str(raised.exception))

    def test_http_failure_redacts_url(self):
        with patch("flex_sync.urllib.request.build_opener", side_effect=RuntimeError("secret-token-in-url")):
            with self.assertRaises(HistoryError) as raised:
                http_get("SendRequest", {"t": "secret"})
        self.assertNotIn("secret", str(raised.exception))
        with self.assertRaises(HistoryError):
            NoRedirect().redirect_request(None, None, None, None, None, None)

    def test_local_windows_encryption_round_trip_and_account_isolation(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "credentials.json"
            save_credentials(path, "U123", "LIVE", "987654321012345", "42", "America/New_York")
            self.assertNotIn("987654321012345", path.read_text())
            self.assertEqual(load_credentials(path, "U123", "LIVE")["token"], "987654321012345")
            self.assertEqual(load_credentials(path, "U456", "LIVE"), {})
            self.assertEqual(load_credentials(path, "U123", "PAPER"), {})


def order(**changes):
    value = dict(order_id=7, account="DU123", client_id=31, order_ref="QOPT1:x", conid=123,
                 action="BUY", quantity=10, filled=0, remaining=10, order_type="LMT", limit_price=1.,
                 status="Submitted", modifiable=True, owned=True, local_symbol="TEST")
    value.update(changes)
    return value


class FakeBroker:
    def __init__(self):
        self.order = order()
        self.price = 1.05
        self.log = Mock()
        self.sent = []
        self.previews = 0
        self.ack = "amend_acknowledged"
        self.hook = None
    def orders(self):
        return [copy.deepcopy(self.order)]
    def snapshot(self):
        return dict(orders=self.orders())
    def preview_modify(self, oid, mode):
        self.previews += 1
        assert mode == "MID"
        return dict(token="t", limit_price=self.price, remaining=self.order["remaining"], method="amend", order_type="LMT")
    def modify_order(self, token):
        if self.hook:
            self.hook()
        if self._auto_mid_guard and not self._auto_mid_guard():
            raise ValueError("自动追价已停止")
        self.order["limit_price"] = self.price
        self.sent.append(copy.deepcopy(self.order))
        return dict(self.order, modification=self.ack)
    def buy(self, token):
        return copy.deepcopy(self.order)


class AutoMidTests(unittest.TestCase):
    def setUp(self):
        self.now = 0
        self.b = FakeBroker()
        self.manager = AutoMidManager(self.b, clock=lambda: self.now)
    def tick(self, delta=3):
        self.now += delta
        self.manager.tick()
    def start(self, **kwargs):
        return self.manager.start(7, kwargs)

    def test_partial_amend_keeps_identity_total_and_only_changes_limit(self):
        self.start()
        self.tick(2)
        self.assertFalse(self.b.sent)
        self.b.order.update(filled=3, remaining=7)
        self.tick(1)
        self.assertEqual((self.b.sent[0]["order_id"], self.b.sent[0]["quantity"], self.b.sent[0]["remaining"]), (7, 10, 7))
        self.assertEqual(self.b.sent[0]["limit_price"], 1.05)
        self.assertEqual(self.manager.describe(7)["auto_mid_count"], 1)
        self.tick()
        self.assertEqual(len(self.b.sent), 1)  # Unchanged midpoint does not churn orders.

    def test_buy_upper_and_sell_lower_bounds(self):
        for action, price in (("BUY", 1.2), ("SELL", .8)):
            self.b.order.update(action=action)
            self.manager = AutoMidManager(self.b, clock=lambda: self.now)
            self.start()
            self.b.price = price
            self.tick()
            self.assertFalse(self.manager.describe(7)["auto_mid_active"])
            self.assertFalse(self.b.sent)

    def test_budget_accounts_for_fills_and_remaining(self):
        self.start(budget_cap=1020)
        self.b.order.update(filled=3, remaining=7, avg_fill_price=1.)
        self.tick()
        self.assertFalse(self.b.sent)
        self.assertIn("预算", self.manager.describe(7)["auto_mid_status"])

    def test_stop_during_broker_check_prevents_transmission(self):
        self.start()
        self.b.hook = lambda: self.manager.request_stop(7)
        self.tick()
        self.assertFalse(self.b.sent)
        self.assertFalse(self.manager.describe(7)["auto_mid_active"])
        self.assertIsNone(self.b._auto_mid_guard)

    def test_unacknowledged_amend_never_retries(self):
        self.b.ack = "amend_requested"
        self.start()
        self.tick()
        self.tick(30)
        self.assertEqual(len(self.b.sent), 1)
        self.assertFalse(self.manager.describe(7)["auto_mid_active"])

    def test_expiry_retains_original_order(self):
        self.start(duration=5)
        self.tick(6)
        self.assertFalse(self.b.sent)
        self.assertEqual(self.b.order["status"], "Submitted")

    def test_manual_price_quantity_or_identity_change_stops(self):
        for changes in (dict(limit_price=.9), dict(quantity=11), dict(account="DU999"), dict(order_type="MKT")):
            with self.subTest(changes=changes):
                self.setUp()
                self.start()
                self.b.order.update(changes)
                self.tick()
                self.assertFalse(self.b.sent)
                self.assertFalse(self.manager.describe(7)["auto_mid_active"])

    def test_finished_cancelled_and_rejected_orders_stop(self):
        for status in ("Filled", "Cancelled", "ApiCancelled", "Inactive"):
            self.setUp()
            self.start()
            self.b.order["status"] = status
            self.tick()
            self.assertFalse(self.b.sent)
            self.assertFalse(self.manager.describe(7)["auto_mid_active"])

    def test_stale_quotes_and_nonfinite_prices_never_modify(self):
        self.start()
        self.b.preview_modify = Mock(side_effect=ValueError("盘口陈旧"))
        self.tick()
        self.assertFalse(self.b.sent)
        self.setUp()
        self.start()
        self.b.price = float("nan")
        self.tick()
        self.assertFalse(self.b.sent)

    def test_auto_never_replaces_or_converts_to_market(self):
        self.start()
        self.b.preview_modify = Mock(return_value=dict(limit_price=1.05, method="replace", order_type="MKT"))
        self.tick()
        self.assertFalse(self.b.sent)

    def test_confirmation_freezes_anchor_and_identity(self):
        expected = dict(identity=self.manager.identity(self.b.order), limit_price=1.)
        self.b.order["limit_price"] = 1.2
        with self.assertRaisesRegex(ValueError, "确认期间"):
            self.manager.start(7, expected=expected)

    def test_start_rejects_unsupported_orders_and_duplicate_start(self):
        for changes in (dict(order_type="MKT"), dict(opens_short=True), dict(owned=False), dict(parent_id=1), dict(oca_group="g")):
            self.b.order = order(**changes)
            with self.assertRaises(ValueError):
                self.start()
        self.b.order = order()
        self.start()
        with self.assertRaises(ValueError):
            self.start()

    def test_initial_zero_remaining_waits_for_ack_not_false_completion(self):
        self.b.order.update(status="PendingSubmit", remaining=0)
        self.start()
        self.tick()
        self.assertTrue(self.manager.describe(7)["auto_mid_active"])
        self.b.order.update(status="Submitted", remaining=10)
        self.tick()
        self.assertEqual(len(self.b.sent), 1)

    def test_worker_registers_initial_order_without_duplicate_placement(self):
        worker = Worker(queue.Queue(), queue.Queue())
        worker.broker = self.b
        self.b.buy = Mock(side_effect=self.b.buy)
        result = worker.execute_broker("buy", dict(token="t", auto_mid=dict(interval=3)))
        self.assertTrue(result["auto_mid_active"])
        self.b.buy.assert_called_once()
        worker.execute_broker("auto_mid_stop", dict(order_id=7))
        self.assertFalse(worker.auto_mid.describe(7)["auto_mid_active"])
        self.assertEqual(self.b.order["status"], "Submitted")

    def test_worker_invalid_policy_cannot_send_initial_order(self):
        worker = Worker(queue.Queue(), queue.Queue())
        worker.broker = self.b
        self.b.buy = Mock()
        with self.assertRaises(ValueError):
            worker.execute_broker("buy", dict(token="t", auto_mid=dict(interval=0)))
        self.b.buy.assert_not_called()

    def test_policy_rejects_nan_and_invalid_ranges(self):
        for config in (dict(interval="nan"), dict(duration=0), dict(drift_pct=101), dict(budget_cap=-1)):
            with self.assertRaises(ValueError):
                policy(config)


class NewUiTests(unittest.TestCase):
    def test_auto_entry_freezes_cash_budget_and_rejects_changed_policy(self):
        import test_v160_ui as fixture
        for mode, budget_cap in (("比例 %", 1000), ("金额 USD", 500)):
            ui = fixture.entry_controller()
            ui.entry_mode.set("中间价（自动追价）")
            ui.budget_mode.set(mode)
            ui.cash_amount.set("500")
            ui.render_pair({"C": fixture.preview(cash=1000, budget=500, price_mode="MID")}, {})
            ui.submit = Mock()
            ui.execute_side("C")
            self.assertEqual(ui.submit.call_args.kwargs["auto_mid"]["budget_cap"], budget_cap)
            self.assertEqual(ui.submit.call_args.kwargs["token"], "token-C")
        ui = fixture.entry_controller()
        ui.entry_mode.set("中间价（自动追价）")
        ui.mid_interval = Variable("3")
        ui.render_pair({"C": fixture.preview(cash=1000, budget=500, price_mode="MID")}, {})
        ui.mid_interval.set("5")
        ui.submit = Mock()
        ui.execute_side("C")
        ui.submit.assert_not_called()

    def test_close_fingerprint_handles_incomplete_typing_without_crashing(self):
        ui = close_controller()
        ui.position_close_dialog["mode"].set("自动中间价")
        ui.mid_interval = Variable("")
        first = ui.close_form_fingerprint()
        ui.mid_interval.set("3")
        self.assertNotEqual(first, ui.close_form_fingerprint())

    def test_auto_close_uses_fixed_mid_preview_then_attaches_policy(self):
        ui = close_controller()
        ui.position_close_dialog["mode"].set("自动中间价")
        ui.request_position_close()
        self.assertEqual(ui.submit.call_args.kwargs["order_mode"], "MID")
        ui.render_position_close(preview())
        self.assertIn("自动追价每 3 秒", ui.position_close_dialog["text"].get())
        ui.submit_position_close()
        self.assertEqual(ui.mutate.call_args.args, ("close_confirm",))
        self.assertEqual(ui.mutate.call_args.kwargs["auto_mid"]["interval"], 3)

    def test_flex_result_from_previous_account_is_ignored(self):
        ui = controller()
        ui.submit = Mock()
        old = ui.context()
        ui.session_epoch += 1
        ui.handle_result("flex_download", dict(reports=["x"], tz_name="UTC"), None, old)
        ui.submit.assert_not_called()

    def test_flex_completion_does_not_reset_active_worker_job(self):
        ui = controller()
        ui.submit = Mock()
        ui.handle_result("flex_download", dict(reports=["x"], tz_name="UTC"), None, ui.context())
        self.assertTrue(ui.busy)
        self.assertEqual(ui.active_request, 11)
        ui.submit.assert_called_once_with("import_flex_reports", reports=["x"], tz_name="UTC")


class BrokerAutoIntegrationTests(unittest.TestCase):
    """Real amendment implementation, mocked IB transport only."""
    def setUp(self):
        import test_order_amendments as fixtures
        self.fixture = fixtures.AmendmentsTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.b = self.fixture.b
        self.now = 0
        self.manager = AutoMidManager(self.b, lambda: self.now)

    def tick(self):
        self.now = 3
        self.manager.tick()

    def test_real_partial_order_amends_same_id_after_cash_and_whatif(self):
        self.manager.start(7)
        self.fixture.fill(3)
        self.tick()
        self.assertEqual(len(self.fixture.placements), 1)
        sent = self.fixture.placements[0]
        self.assertEqual((sent.orderId, sent.totalQuantity, sent.orderType, sent.lmtPrice), (7, 10, "LMT", .22))
        self.b.ib.cancelOrder.assert_not_called()
        self.b.ib.whatIfOrder.assert_called_once()
        self.assertEqual(self.manager.describe(7)["auto_mid_count"], 1)

    def test_stop_flag_set_during_whatif_prevents_placeorder(self):
        self.manager.start(7)
        def whatif(*args):
            self.manager.request_stop(7)
            from types import SimpleNamespace
            return SimpleNamespace(warningText="", commission=1, commissionCurrency="USD")
        self.b.ib.whatIfOrder.side_effect = whatif
        self.tick()
        self.b.ib.placeOrder.assert_not_called()

    def test_actual_fee_above_auto_budget_prevents_placeorder(self):
        self.manager.start(7, dict(budget_cap=231))
        # Preview reserves ten dollars; broker reports twelve.
        self.b.ib.whatIfOrder.return_value.commission = 12
        self.tick()
        self.b.ib.placeOrder.assert_not_called()
        self.assertIn("预算", self.manager.describe(7)["auto_mid_status"])

    def test_fill_during_whatif_cannot_duplicate_remaining(self):
        self.manager.start(7)
        def whatif(*args):
            self.fixture.fill(3)
            from types import SimpleNamespace
            return SimpleNamespace(warningText="", commission=1, commissionCurrency="USD")
        self.b.ib.whatIfOrder.side_effect = whatif
        self.tick()
        self.b.ib.placeOrder.assert_not_called()
        self.assertFalse(self.manager.describe(7)["auto_mid_active"])

    def test_account_history_accepts_other_client_ids_and_assets(self):
        from types import SimpleNamespace as NS
        from ib_async import Stock
        stock = Stock("SPY", "SMART", "USD", conId=999)
        def fill(client, exec_id, contract, account="DU123"):
            return NS(contract=contract, execution=NS(acctNumber=account, execId=exec_id, time=NOW,
                       side="BOT", shares=1, price=1., orderId=1, permId=2, clientId=client, exchange="TEST"))
        self.b.ib.reqExecutions.return_value = [fill(99, "external", stock), fill(31, "own", self.fixture.contract), fill(0, "otheraccount", stock, "DU999")]
        self.b._history_backfill()
        store = self.b._get_history_store()
        self.addCleanup(store.close)
        rows = store.history("DU123", "PAPER", now=NOW)["rows"]
        self.assertEqual({r["exec_id"] for r in rows}, {"own", "external"})
        requested_filter = self.b.ib.reqExecutions.call_args.args[0]
        self.assertEqual(requested_filter.acctCode, "DU123")
        self.assertEqual(requested_filter.clientId, 0)


if __name__ == "__main__":
    unittest.main()
