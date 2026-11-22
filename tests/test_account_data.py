"""No-network account summary parsing and request-lifecycle regressions."""
import asyncio
from datetime import date, datetime
from importlib.util import find_spec
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from account_data import AccountData, SUMMARY_TAGS, financial_view


def usd_rows():
    return [("NetLiquidation", "1500", "USD"),
            ("AvailableFunds", "900", "USD"),
            ("SettledCash", "800", "USD"),
            ("CashBalance", "1000", "USD")]


class FinancialViewTests(unittest.TestCase):
    def test_usd_summary_uses_minimum_not_net_liquidation(self):
        view = financial_view(usd_rows())
        self.assertTrue(view["ready"])
        self.assertEqual(view["cash"], 800)
        self.assertEqual(view["net_liquidation"], 1500)
        self.assertEqual(view["usd_cash"], 1000)

    def test_account_total_cash_for_display_is_independent_of_usd_order_budget(self):
        for report, expected in ((("1200", "USD"), 1200), (("1100", "EUR"), None), (("NaN", "USD"), None)):
            with self.subTest(report=report):
                view = financial_view(usd_rows() + [("TotalCashValue", *report)])
                self.assertEqual(view["total_cash_value"], expected)
                self.assertEqual(view["usd_cash"], 1000)
                self.assertEqual(view["cash"], 800)
                self.assertTrue(view["ready"])

    def test_real_screenshot_ledger_currency_base_is_not_account_currency(self):
        rows = usd_rows()[:-1] + [("$LEDGER-CashBalance", "1000", "USD"),
                                  ("$LEDGER-CashBalance", "1000", "BASE"),
                                  ("$LEDGER-Currency", "BASE", "BASE"),
                                  ("$LEDGER-Currency", "USD", "USD")]
        view = financial_view(rows)
        self.assertTrue(view["ready"])
        self.assertEqual(view["base_currency"], "USD")

    def test_eur_base_stays_displayable_but_usd_order_budget_unknown(self):
        rows = [(tag, value, "EUR") for tag, value, _ in usd_rows()[:-1]]
        rows.append(("CashBalance", "1000", "USD"))
        view = financial_view(rows)
        self.assertFalse(view["ready"])
        self.assertEqual(view["net_liquidation"], 1500)
        self.assertEqual(view["net_liquidation_currency"], "EUR")
        self.assertEqual(view["usd_cash"], 1000)
        self.assertIsNone(view["cash"])
        self.assertIsNone(view["settled_cash"])
        self.assertEqual(view["aggregate_settled_cash"], 800)

    def test_missing_settled_cash_does_not_hide_other_fresh_values(self):
        view = financial_view([row for row in usd_rows() if row[0] != "SettledCash"])
        self.assertFalse(view["ready"])
        self.assertEqual(view["usd_cash"], 1000)
        self.assertIsNone(view["settled_cash"])
        self.assertIsNone(view["cash"])

    def test_ledger_settled_cannot_replace_account_settled(self):
        rows = [row for row in usd_rows() if row[0] != "SettledCash"]
        rows.append(("$LEDGER-SettledCash", "800", "USD"))
        self.assertFalse(financial_view(rows)["ready"])

    def test_ledger_settled_is_additional_lower_cap(self):
        view = financial_view(usd_rows() + [("$LEDGER-SettledCash", "300", "USD")])
        self.assertEqual(view["cash"], 300)

    def test_cash_aliases_never_sum(self):
        view = financial_view(usd_rows() + [("$LEDGER-CashBalance", "200", "USD")])
        self.assertEqual(view["usd_cash"], 200)
        self.assertEqual(view["cash"], 200)

    def test_invalid_cash_alias_blocks_even_with_valid_old_alias(self):
        view = financial_view(usd_rows() + [("$LEDGER-CashBalance", "nan", "USD")])
        self.assertFalse(view["ready"])
        self.assertIsNone(view["cash"])

    def test_base_and_other_currency_cash_are_never_usd(self):
        rows = usd_rows()[:-1] + [("CashBalance", "1000", "BASE"), ("CashBalance", "2000", "EUR")]
        view = financial_view(rows)
        self.assertFalse(view["ready"])
        self.assertIsNone(view["usd_cash"])

    def test_conflicting_aggregate_units_block_sizing(self):
        view = financial_view(usd_rows() + [("AvailableFunds", "900", "EUR")])
        self.assertFalse(view["ready"])
        self.assertIsNone(view["base_currency"])
        self.assertEqual(view["net_liquidation_currency"], "USD")

    def test_negative_funds_show_actual_value_and_zero_order_budget(self):
        rows = [(tag, "-10" if tag == "AvailableFunds" else value, unit) for tag, value, unit in usd_rows()]
        view = financial_view(rows)
        self.assertTrue(view["ready"])
        self.assertEqual(view["available_funds"], -10)
        self.assertEqual(view["cash"], 0)

    def test_partial_download_never_enables_buy_even_if_fields_arrived(self):
        view = financial_view(usd_rows(), complete=False, request_error="timeout")
        self.assertFalse(view["ready"])
        self.assertEqual(view["net_liquidation"], 1500)
        self.assertIsNone(view["cash"])
        self.assertIn("timeout", view["error"])


class DatedSettlementTests(unittest.TestCase):
    TODAY = date(2026, 11, 11)

    def view(self, updates, rows=None):
        if rows is None:
            rows = [row for row in usd_rows() if row[0] != "SettledCash"]
        return financial_view(rows, updates, today=self.TODAY)

    def test_current_usd_schedule_replaces_missing_legacy_settled(self):
        view = self.view([("$LEDGER-SettledCashByDate", "20261110:400;20261111:500;20261112:900", "USD")])
        self.assertTrue(view["ready"])
        self.assertEqual(view["cash"], 500)
        self.assertEqual(view["settled_cash"], 500)
        self.assertEqual(view["settled_cash_current"], 500)
        self.assertEqual(view["settled_as_of"], "2026-11-11")
        self.assertEqual(view["settled_source"], "SettledCashByDate/USD")

    def test_latest_past_day_allowed_when_no_settlement_today(self):
        view = self.view([("SettledCashByDate", "20261109:300;20261110:600", "USD")])
        self.assertTrue(view["ready"])
        self.assertEqual(view["cash"], 600)
        self.assertEqual(view["settled_as_of"], "2026-11-10")

    def test_future_debits_reduce_cap_without_changing_display(self):
        view = self.view([("SettledCashByDate", "20261111:500;20261112:800;20261113:600;20261114:900;20261115:850", "USD")])
        self.assertTrue(view["ready"])
        self.assertEqual(view["settled_cash"], 500)
        self.assertEqual(view["settlement_cash_cap"], 250)
        self.assertEqual(view["cash"], 250)

    def test_schedule_order_is_dates_not_wire_order(self):
        view = self.view([("SettledCashByDate", "20261112:100;20261110:300;20261111:500", "USD")])
        self.assertEqual(view["cash"], 100)
        self.assertEqual(view["settled_cash_current"], 500)

    def test_only_future_dates_cannot_enable_buy(self):
        view = self.view([("SettledCashByDate", "20261112:500", "USD")])
        self.assertFalse(view["ready"])
        self.assertIsNone(view["cash"])
        self.assertIn("只有未来", view["error"])

    def test_base_other_currency_and_nonsecurity_segments_cannot_substitute(self):
        for tag, unit in [("SettledCashByDate", "BASE"), ("SettledCashByDate", "EUR"),
                          ("SettledCashByDate-C", "USD"), ("$LEDGER-SettledCashByDate-P", "USD")]:
            with self.subTest(tag=tag, unit=unit):
                view = self.view([(tag, "20261111:500", unit)])
                self.assertFalse(view["ready"])
                self.assertIsNone(view["settled_cash"])

    def test_securities_and_total_are_caps_not_added_or_declared_conflicts(self):
        view = self.view([("SettledCashByDate", "20261111:700", "USD"),
                          ("$LEDGER-SettledCashByDate-S", "20261111:400", "USD")])
        self.assertTrue(view["ready"])
        self.assertEqual(view["settled_cash"], 700)
        self.assertEqual(view["cash"], 400)
        only_securities = self.view([("$LEDGER-SettledCashByDate-S", "20261111:400", "USD")])
        self.assertTrue(only_securities["ready"])
        self.assertEqual(only_securities["settled_cash"], 400)

    def test_same_day_conflicting_duplicate_across_aliases_blocks(self):
        view = self.view([("SettledCashByDate", "20261111:400", "USD"),
                          ("$LEDGER-SettledCashByDate", "20261111:500", "USD")])
        self.assertFalse(view["ready"])
        self.assertIsNone(view["cash"])
        self.assertIn("冲突", view["error"])

    def test_identical_duplicates_do_not_double_cash(self):
        view = self.view([("SettledCashByDate", "20261111:400;20261111:400", "USD"),
                          ("$LEDGER-SettledCashByDate", "20261111:400.0", "USD")])
        self.assertTrue(view["ready"])
        self.assertEqual(view["cash"], 400)

    def test_malformed_date_amount_or_duplicate_blocks_even_with_legacy(self):
        bad = ["", "20260229:10", "2026111:10", "20261111:nan", "20261111:inf",
               "20261111:1e309", "20261111:1_000", "20261111:20;", "20261111:20;20261111:30"]
        for value in bad:
            with self.subTest(value=value):
                view = self.view([("SettledCashByDate", value, "USD")], usd_rows())
                self.assertFalse(view["ready"])
                self.assertIsNone(view["cash"])

    def test_negative_current_settled_is_visible_and_caps_budget_zero(self):
        view = self.view([("SettledCashByDate", "20261111:-20", "USD")])
        self.assertTrue(view["ready"])
        self.assertEqual(view["settled_cash"], -20)
        self.assertEqual(view["cash"], 0)

    def test_legacy_summary_and_dated_funds_both_constrain_budget(self):
        view = self.view([("SettledCashByDate", "20261111:300", "USD")], usd_rows())
        self.assertTrue(view["ready"])
        self.assertEqual(view["settled_cash"], 300)
        self.assertEqual(view["settled_cash_current"], 300)
        self.assertEqual(view["cash"], 300)

    def test_account_type_original_text_is_not_guessed_as_margin(self):
        view = self.view([("SettledCashByDate", "20261111:500", "USD"),
                          ("AccountType", "INDIVIDUAL", ""), ("TradingType-S", "CASH", "")])
        self.assertTrue(view["ready"])
        self.assertEqual(view["account_type_raw"], "INDIVIDUAL")
        self.assertEqual(view["trading_type"], "CASH")
        self.assertIn("账户结构 INDIVIDUAL", view["account_type_label"])

    def test_missing_account_type_does_not_change_budget_rule(self):
        view = self.view([("SettledCashByDate", "20261111:500", "USD")])
        self.assertTrue(view["ready"])
        self.assertEqual(view["account_type_raw"], "")
        self.assertEqual(view["trading_type"], "")

    def test_stknopt_label_is_reg_t_but_does_not_bypass_settlement(self):
        view = self.view([("AccountType", "INDIVIDUAL", ""), ("TradingType-S", "STKNOPT", "")])
        self.assertIn("Reg T 保证金", view["account_type_label"])
        self.assertFalse(view["ready"])
        self.assertIsNone(view["cash"])

    def test_empty_currency_date_string_stays_unknown_and_masked(self):
        view = self.view([("SettledCashByDate", "20261111:146.57;20261112:1000.25", "")])
        self.assertFalse(view["ready"])
        self.assertIsNone(view["settled_cash"])
        self.assertEqual(view["settlement_shapes"], ["SettledCashByDate/<empty>: <DATE>:<N>;<DATE>:<N>"])

    def test_diagnostic_preserves_json_structure_but_hides_amount_date_and_identifiers(self):
        raw = '{"USD":{"20261111":146.57},"account":"U1234567","name":"Private Person"}'
        view = self.view([("SettledCashByDate", raw, "")])
        shape = " ".join(view["settlement_shapes"])
        self.assertIn('"USD":', shape)
        self.assertIn('"<DATE>":<N>', shape)
        for private in ("20261111", "146.57", "U1234567", "Private", "Person"):
            self.assertNotIn(private, shape)
        self.assertFalse(view["ready"])

    def test_shape_output_is_bounded_and_multiline_text_is_masked(self):
        view = self.view([("SettledCashByDate", '\n'.join(['账户 U1234567=123456.78'] * 1000), "")])
        shape = " ".join(view["settlement_shapes"])
        self.assertLess(len(shape), 300)
        self.assertNotIn("\n", shape)
        self.assertNotIn("U1234567", shape)
        self.assertNotIn("123456.78", shape)

    def test_account_ready_false_or_unknown_invalidates_fresh_balances(self):
        for status in ("false", "0", "unknown", ""):
            with self.subTest(status=status):
                view = self.view([("SettledCashByDate", "20261111:500", "USD"), ("AccountReady", status, "")])
                self.assertFalse(view["ready"])
                self.assertIsNone(view["cash"])
                self.assertEqual(view["usd_cash"], 1000)

    def test_account_ready_true_does_not_block(self):
        view = self.view([("SettledCashByDate", "20261111:500", "USD"), ("$LEDGER-AccountReady", "true", "")])
        self.assertTrue(view["ready"])

    def test_incomplete_dated_download_is_display_only(self):
        view = financial_view([row for row in usd_rows() if row[0] != "SettledCash"],
                              [("SettledCashByDate", "20261111:500", "USD")], complete=False, today=self.TODAY)
        self.assertFalse(view["ready"])
        self.assertIsNone(view["cash"])

    def test_new_york_date_used_instead_of_local_calendar(self):
        with patch("account_data.datetime") as clock:
            clock.now.return_value = datetime(2026, 11, 10, 23, 30)
            clock.strptime.side_effect = datetime.strptime
            view = financial_view([row for row in usd_rows() if row[0] != "SettledCash"],
                                  [("SettledCashByDate", "20261110:300;20261111:500", "USD")])
            self.assertEqual(str(clock.now.call_args.args[0]), "America/New_York")
            self.assertEqual(view["cash"], 300)


class LegacyAggregateSettlementTests(unittest.TestCase):
    TODAY = date(2026, 11, 11)

    def rows(self):
        return [("AccountReady", "true", ""),
                ("$LEDGER-CashBalance", "146.01", "USD"),
                ("$LEDGER-CashBalance", "146.01", "BASE"),
                ("SettledCashByDate", "20261111:146.01", ""),
                ("SettledCashByDate-S", "20261111:146.01", ""),
                ("SettledCashByDate-C", "20261111:0", "")]

    def summary(self):
        return [("NetLiquidation", "146.01", "USD"),
                ("AvailableFunds", "146.01", "USD"),
                ("TotalCashValue", "146.01", "USD")]

    def view(self, updates=None, summary=None, **kwargs):
        return financial_view(self.summary() if summary is None else summary,
                              self.rows() if updates is None else updates, today=self.TODAY, **kwargs)

    def test_observed_server_178_plain_empty_units_are_verified_aggregate_usd(self):
        view = self.view()
        self.assertTrue(view["ready"])
        self.assertTrue(view["legacy_settlement_currency_verified"])
        self.assertEqual(view["cash"], 146.01)
        self.assertEqual(view["settled_cash"], 146.01)
        self.assertIn("旧版汇总字段", view["settled_source"])
        self.assertIn("核验", view["settled_source"])
        self.assertIn("SettledCashByDate/", view["field_names"])
        self.assertNotIn("SettledCashByDate/USD", view["field_names"])

    def test_both_summary_base_fields_are_required_and_must_be_valid_usd(self):
        for changed_tag in ("NetLiquidation", "AvailableFunds"):
            for change in ("missing", "EUR", "BASE", "", "bad_amount"):
                with self.subTest(tag=changed_tag, change=change):
                    summary = [row for row in self.summary() if row[0] != changed_tag]
                    if change != "missing":
                        summary.append((changed_tag, "nan" if change == "bad_amount" else "146.01",
                                        "USD" if change == "bad_amount" else change))
                    view = self.view(summary=summary)
                    self.assertFalse(view["ready"])
                    self.assertFalse(view["legacy_settlement_currency_verified"])
                    self.assertIsNone(view["cash"])

    def test_same_summary_tag_with_mixed_currency_does_not_prove_usd_base(self):
        view = self.view(summary=self.summary() + [("NetLiquidation", "123", "EUR")])
        self.assertFalse(view["ready"])
        self.assertFalse(view["legacy_settlement_currency_verified"])

    def test_fresh_explicit_account_ready_true_is_mandatory(self):
        for status in (None, "false", "0", ""):
            with self.subTest(status=status):
                rows = [row for row in self.rows() if row[0] != "AccountReady"]
                if status is not None:
                    rows.append(("AccountReady", status, ""))
                view = self.view(rows)
                self.assertFalse(view["ready"])
                self.assertFalse(view["legacy_settlement_currency_verified"])

    def test_fresh_prefixed_usd_cash_is_required_not_summary_or_plain_cash(self):
        rows = [("CashBalance" if tag == "$LEDGER-CashBalance" else tag, value, unit)
                for tag, value, unit in self.rows()]
        view = self.view(rows, self.summary() + [("$LEDGER-CashBalance", "146.01", "USD")])
        self.assertFalse(view["ready"])
        self.assertFalse(view["legacy_settlement_currency_verified"])
        self.assertEqual(view["usd_cash"], 146.01)

    def test_foreign_or_unknown_cash_currency_blocks_legacy_inference_even_if_zero(self):
        for currency in ("EUR", "HKD", "", "UNKNOWN"):
            with self.subTest(currency=currency):
                view = self.view(self.rows() + [("$LEDGER-CashBalance", "0", currency)])
                self.assertFalse(view["ready"])
                self.assertFalse(view["legacy_settlement_currency_verified"])

    def test_prefixed_empty_settlement_unit_is_not_inferred(self):
        rows = [("$LEDGER-" + tag if tag in ("SettledCashByDate", "SettledCashByDate-S") else tag, value, unit)
                for tag, value, unit in self.rows()]
        view = self.view(rows)
        self.assertFalse(view["ready"])
        self.assertFalse(view["legacy_settlement_currency_verified"])
        self.assertIsNone(view["settled_cash"])

    def test_ambiguous_prefixed_empty_or_foreign_schedule_blocks_plain_inference(self):
        for row in [("$LEDGER-SettledCashByDate", "20261111:146.01", ""),
                    ("SettledCashByDate", "20261111:146.01", "BASE"),
                    ("SettledCashByDate-S", "20261111:146.01", "EUR")]:
            with self.subTest(row=row):
                view = self.view(self.rows() + [row])
                self.assertFalse(view["ready"])
                self.assertFalse(view["legacy_settlement_currency_verified"])

    def test_summary_only_empty_schedule_cannot_use_fresh_ledger_as_proof_of_its_scope(self):
        updates = [row for row in self.rows() if not row[0].startswith("SettledCashByDate")]
        view = self.view(updates, self.summary() + [("SettledCashByDate", "20261111:146.01", "")])
        self.assertFalse(view["ready"])
        self.assertFalse(view["legacy_settlement_currency_verified"])

    def test_unfinished_fresh_download_cannot_infer_legacy_units(self):
        view = self.view(complete=False, request_error="timeout")
        self.assertFalse(view["ready"])
        self.assertFalse(view["legacy_settlement_currency_verified"])
        self.assertIsNone(view["cash"])

    def test_normalized_and_explicit_schedule_conflicts_are_still_rejected(self):
        view = self.view(self.rows() + [("$LEDGER-SettledCashByDate", "20261111:999", "USD")])
        self.assertFalse(view["ready"])
        self.assertIsNone(view["cash"])
        self.assertIn("冲突", view["error"])

    def test_legacy_units_do_not_relax_date_validation_or_future_credit_rules(self):
        rows = [(tag, "20261111:146.01;20261112:1000;20261113:900" if tag in ("SettledCashByDate", "SettledCashByDate-S") else value, unit)
                for tag, value, unit in self.rows()]
        view = self.view(rows)
        self.assertTrue(view["ready"])
        self.assertEqual(view["settled_cash"], 146.01)
        self.assertAlmostEqual(view["cash"], 46.01)
        rows.append(("SettledCashByDate", "invalid", ""))
        self.assertFalse(self.view(rows)["ready"])

    def test_inferred_schedule_is_cap_and_does_not_create_additional_cash(self):
        rows = [(tag, "50" if tag == "$LEDGER-CashBalance" and unit == "USD" else value, unit)
                for tag, value, unit in self.rows()]
        self.assertEqual(self.view(rows)["cash"], 50)


class FakeWrapper:
    def __init__(self):
        self._futures = {}
        self._results = {}
        self.summary_cache = []
        self.account_cache = []

    def startReq(self, key):
        future = asyncio.Future()
        self._futures[key] = future
        self._results[key] = []
        return future

    def _endReq(self, key):
        future = self._futures.pop(key, None)
        results = self._results.pop(key, [])
        if future is not None and not future.done():
            future.set_result(results)

    def accountSummary(self, req_id, account, tag, value, currency):
        self.summary_cache.append((req_id, account, tag, value, currency))

    def accountSummaryEnd(self, req_id):
        self._endReq(req_id)

    def updateAccountValue(self, tag, value, currency, account):
        self.account_cache.append((tag, value, currency, account))

    def accountDownloadEnd(self, account):
        self._endReq("accountValues")


class FakeIB:
    def __init__(self, loop):
        self.wrapper = FakeWrapper()
        self.loop = loop
        self.connected = True
        self.next_id = 100
        self.cancelled = []
        self.requests = []
        self.cash_requests = []
        self.summary_script = self.default_summary
        self.cash_script = self.default_cash
        self.client = SimpleNamespace(getReqId=self.get_id, reqAccountSummary=self.request,
                                      cancelAccountSummary=self.cancelled.append,
                                      reqAccountUpdates=lambda active, account: self.cash_requests.append((active, account)))

    def isConnected(self):
        return self.connected

    def get_id(self):
        self.next_id += 1
        return self.next_id

    def request(self, req_id, group, tags):
        self.requests.append((req_id, group, tags))
        self.summary_script(req_id)

    def default_summary(self, req_id):
        for row in usd_rows():
            self.wrapper.accountSummary(req_id, "DU123", *row)
        self.wrapper.accountSummaryEnd(req_id)

    def default_cash(self):
        self.wrapper.accountDownloadEnd("DU123")

    def reqAccountUpdatesAsync(self, account):
        future = self.wrapper.startReq("accountValues")
        self.cash_requests.append((True, account))
        self.cash_script()
        return future

    def _run(self, coroutine):
        return self.loop.run_until_complete(coroutine)


class RequestLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.ib = FakeIB(self.loop)
        self.logs = []
        self.service = AccountData(self.ib, self.logs.append, timeout=.005)

    def tearDown(self):
        self.loop.close()
        asyncio.set_event_loop(None)

    def test_complete_request_is_cancelled_and_hooks_restored(self):
        result = self.service.refresh("DU123")
        self.assertTrue(result["ready"])
        self.assertEqual(self.ib.requests, [(101, "All", SUMMARY_TAGS)])
        self.assertEqual(self.ib.cancelled, [101])
        self.assertEqual(self.ib.wrapper._futures, {})
        self.assertNotIn("accountSummary", vars(self.ib.wrapper))
        self.assertNotIn("accountSummaryEnd", vars(self.ib.wrapper))
        self.assertEqual(len(self.ib.wrapper.summary_cache), 4)

    def test_wrong_request_and_account_are_not_used(self):
        def script(req_id):
            for row in usd_rows():
                self.ib.wrapper.accountSummary(req_id - 1, "DU123", *row)
                self.ib.wrapper.accountSummary(req_id, "U999", *row)
            self.ib.wrapper.accountSummaryEnd(req_id)
        self.ib.summary_script = script
        result = self.service.refresh("DU123")
        self.assertFalse(result["ready"])
        self.assertIsNone(result["net_liquidation"])
        self.assertIsNone(result["cash"])

    def test_wrong_end_marker_times_out_even_if_all_values_present(self):
        def script(req_id):
            for row in usd_rows():
                self.ib.wrapper.accountSummary(req_id, "DU123", *row)
            self.ib.wrapper.accountSummaryEnd(req_id + 1)
        self.ib.summary_script = script
        result = self.service.refresh("DU123")
        self.assertFalse(result["ready"])
        self.assertFalse(result["complete"])
        self.assertEqual(result["usd_cash"], 1000)
        self.assertEqual(self.ib.cancelled, [101])
        self.assertEqual(self.ib.wrapper._futures, {})
        self.assertEqual(self.ib.wrapper._results, {})
        self.assertNotIn("accountSummary", vars(self.ib.wrapper))

    def test_summary_rows_after_matching_end_cannot_change_snapshot(self):
        def script(req_id):
            for row in usd_rows():
                self.ib.wrapper.accountSummary(req_id, "DU123", *row)
            self.ib.wrapper.accountSummaryEnd(req_id)
            self.ib.wrapper.accountSummary(req_id, "DU123", "CashBalance", "100", "USD")
        self.ib.summary_script = script
        result = self.service.refresh("DU123")
        self.assertTrue(result["ready"])
        self.assertEqual(result["usd_cash"], 1000)

    def test_next_download_cannot_reuse_prior_summary_cache(self):
        self.assertTrue(self.service.refresh("DU123")["ready"])
        self.ib.summary_script = lambda req_id: self.ib.wrapper.accountSummaryEnd(req_id)
        result = self.service.refresh("DU123")
        self.assertFalse(result["ready"])
        self.assertIsNone(result["usd_cash"])
        self.assertIsNone(result["net_liquidation"])
        self.assertEqual(self.ib.cancelled, [101, 102])

    def test_fresh_fallback_uses_only_target_account_usd_cash(self):
        def summary(req_id):
            for row in usd_rows()[:-1]:
                self.ib.wrapper.accountSummary(req_id, "DU123", *row)
            self.ib.wrapper.accountSummaryEnd(req_id)
        def cash():
            self.ib.wrapper.updateAccountValue("$LEDGER-CashBalance", "9000", "USD", "U999")
            self.ib.wrapper.updateAccountValue("$LEDGER-CashBalance", "5000", "BASE", "DU123")
            self.ib.wrapper.updateAccountValue("$LEDGER-CashBalance", "700", "USD", "DU123")
            self.ib.wrapper.accountDownloadEnd("DU123")
        self.ib.summary_script, self.ib.cash_script = summary, cash
        result = self.service.refresh("DU123")
        self.assertTrue(result["ready"])
        self.assertEqual(result["cash"], 700)
        self.assertEqual(self.ib.cash_requests, [(False, "DU123"), (True, "DU123")])
        self.assertNotIn("updateAccountValue", vars(self.ib.wrapper))
        self.assertNotIn("accountDownloadEnd", vars(self.ib.wrapper))

    def test_fallback_missing_end_cannot_enable_buy(self):
        def summary(req_id):
            for row in usd_rows()[:-1]:
                self.ib.wrapper.accountSummary(req_id, "DU123", *row)
            self.ib.wrapper.accountSummaryEnd(req_id)
        self.ib.summary_script = summary
        self.ib.cash_script = lambda: self.ib.wrapper.updateAccountValue("CashBalance", "700", "USD", "DU123")
        result = self.service.refresh("DU123")
        self.assertFalse(result["ready"])
        self.assertEqual(result["usd_cash"], 700)
        self.assertIsNone(result["cash"])
        self.assertEqual(self.ib.wrapper._futures, {})

    def summary_without_legacy_settled(self, req_id):
        for row in usd_rows():
            if row[0] != "SettledCash":
                self.ib.wrapper.accountSummary(req_id, "DU123", *row)
        self.ib.wrapper.accountSummaryEnd(req_id)

    def test_missing_settled_downloads_fresh_dates_even_if_usd_cash_already_present(self):
        self.ib.summary_script = self.summary_without_legacy_settled
        def updates():
            self.ib.wrapper.updateAccountValue("$LEDGER-SettledCashByDate", "20000101:450", "USD", "DU123")
            self.ib.wrapper.updateAccountValue("TradingType-S", "CASH", "", "DU123")
            self.ib.wrapper.updateAccountValue("AccountType", "INDIVIDUAL", "", "DU123")
            self.ib.wrapper.updateAccountValue("AccountReady", "true", "", "DU123")
            self.ib.wrapper.accountDownloadEnd("DU123")
        self.ib.cash_script = updates
        result = self.service.refresh("DU123")
        self.assertTrue(result["ready"])
        self.assertEqual(result["cash"], 450)
        self.assertEqual(result["trading_type"], "CASH")
        self.assertEqual(result["account_type_raw"], "INDIVIDUAL")
        self.assertEqual(self.ib.cash_requests, [(False, "DU123"), (True, "DU123")])
        self.assertNotIn("updateAccountValue", vars(self.ib.wrapper))
        self.assertNotIn("accountDownloadEnd", vars(self.ib.wrapper))
        self.assertNotIn("450", "\n".join(self.logs))

    def test_dated_fallback_ignores_other_account_and_non_usd_rows(self):
        self.ib.summary_script = self.summary_without_legacy_settled
        def updates():
            self.ib.wrapper.updateAccountValue("SettledCashByDate", "20000101:450", "USD", "U999")
            self.ib.wrapper.updateAccountValue("SettledCashByDate", "20000101:450", "BASE", "DU123")
            self.ib.wrapper.updateAccountValue("SettledCashByDate-C", "20000101:450", "USD", "DU123")
            self.ib.wrapper.accountDownloadEnd("DU123")
        self.ib.cash_script = updates
        result = self.service.refresh("DU123")
        self.assertFalse(result["ready"])
        self.assertIsNone(result["settled_cash"])
        self.assertIn("SettledCashByDate-C/USD", result["field_names"])

    def test_dated_fallback_late_row_after_end_not_used(self):
        self.ib.summary_script = self.summary_without_legacy_settled
        def updates():
            self.ib.wrapper.accountDownloadEnd("DU123")
            self.ib.wrapper.updateAccountValue("SettledCashByDate", "20000101:450", "USD", "DU123")
        self.ib.cash_script = updates
        result = self.service.refresh("DU123")
        self.assertFalse(result["ready"])
        self.assertIsNone(result["settled_cash"])

    def test_dated_fallback_timeout_restores_callbacks_and_disables_budget(self):
        self.ib.summary_script = self.summary_without_legacy_settled
        self.ib.cash_script = lambda: self.ib.wrapper.updateAccountValue("SettledCashByDate", "20000101:450", "USD", "DU123")
        result = self.service.refresh("DU123")
        self.assertFalse(result["ready"])
        self.assertIsNone(result["cash"])
        self.assertEqual(result["settled_cash"], 450)
        self.assertNotIn("updateAccountValue", vars(self.ib.wrapper))
        self.assertNotIn("accountDownloadEnd", vars(self.ib.wrapper))
        self.assertEqual(self.ib.wrapper._futures, {})

    def test_missing_next_dated_download_cannot_reuse_prior_account_cache(self):
        self.ib.summary_script = self.summary_without_legacy_settled
        def updates():
            self.ib.wrapper.updateAccountValue("SettledCashByDate", "20000101:450", "USD", "DU123")
            self.ib.wrapper.accountDownloadEnd("DU123")
        self.ib.cash_script = updates
        self.assertTrue(self.service.refresh("DU123")["ready"])
        self.ib.cash_script = self.ib.default_cash
        result = self.service.refresh("DU123")
        self.assertFalse(result["ready"])
        self.assertIsNone(result["settled_cash"])
        self.assertIsNone(result["cash"])

    def test_fresh_account_ready_false_blocks_dated_budget(self):
        self.ib.summary_script = self.summary_without_legacy_settled
        def updates():
            self.ib.wrapper.updateAccountValue("SettledCashByDate", "20000101:450", "USD", "DU123")
            self.ib.wrapper.updateAccountValue("AccountReady", "false", "", "DU123")
            self.ib.wrapper.accountDownloadEnd("DU123")
        self.ib.cash_script = updates
        result = self.service.refresh("DU123")
        self.assertFalse(result["ready"])
        self.assertIsNone(result["cash"])
        self.assertIn("AccountReady", result["error"])

    def test_summary_account_type_returned_without_guessing_trading_type(self):
        def summary(req_id):
            for row in usd_rows() + [("AccountType", "INDIVIDUAL", "")]:
                self.ib.wrapper.accountSummary(req_id, "DU123", *row)
            self.ib.wrapper.accountSummaryEnd(req_id)
        self.ib.summary_script = summary
        result = self.service.refresh("DU123")
        self.assertTrue(result["ready"])
        self.assertEqual(result["account_type_raw"], "INDIVIDUAL")
        self.assertEqual(result["trading_type"], "")

    def test_settlement_shape_logged_once_when_only_amount_changes(self):
        self.ib.summary_script = self.summary_without_legacy_settled
        amounts = iter(("146.57", "1250.11"))
        def updates():
            self.ib.wrapper.updateAccountValue("SettledCashByDate", "20261111:" + next(amounts), "", "DU123")
            self.ib.wrapper.accountDownloadEnd("DU123")
        self.ib.cash_script = updates
        self.service.refresh("DU123")
        self.service.refresh("DU123")
        diagnostics = [text for text in self.logs if "结算字段格式" in text]
        self.assertEqual(len(diagnostics), 1)
        text = "\n".join(self.logs)
        for private in ("146.57", "1250.11", "20261111"):
            self.assertNotIn(private, text)
        self.assertIn("<DATE>:<N>", text)

    def test_fresh_callback_legacy_aggregate_with_usd_ledger_enables_cash_budget(self):
        self.ib.summary_script = self.summary_without_legacy_settled
        def updates():
            for row in [("AccountReady", "true", ""),
                        ("$LEDGER-CashBalance", "146.01", "USD"),
                        ("$LEDGER-CashBalance", "146.01", "BASE"),
                        ("SettledCashByDate", "20000101:146.01", ""),
                        ("SettledCashByDate-S", "20000101:146.01", ""),
                        ("SettledCashByDate-C", "20000101:0", "")]:
                self.ib.wrapper.updateAccountValue(*row, "DU123")
            self.ib.wrapper.accountDownloadEnd("DU123")
        self.ib.cash_script = updates
        result = self.service.refresh("DU123")
        self.assertTrue(result["ready"])
        self.assertTrue(result["legacy_settlement_currency_verified"])
        self.assertEqual(result["cash"], 146.01)
        self.assertNotIn("146.01", "\n".join(self.logs))
        self.assertNotIn("updateAccountValue", vars(self.ib.wrapper))

    def test_legacy_proof_is_account_scoped_and_cannot_reuse_ready_from_other_account(self):
        self.ib.summary_script = self.summary_without_legacy_settled
        def updates():
            self.ib.wrapper.updateAccountValue("AccountReady", "true", "", "U999")
            self.ib.wrapper.updateAccountValue("$LEDGER-CashBalance", "146.01", "USD", "DU123")
            self.ib.wrapper.updateAccountValue("SettledCashByDate", "20000101:146.01", "", "DU123")
            self.ib.wrapper.accountDownloadEnd("DU123")
        self.ib.cash_script = updates
        result = self.service.refresh("DU123")
        self.assertFalse(result["ready"])
        self.assertFalse(result["legacy_settlement_currency_verified"])
        self.assertIsNone(result["cash"])

    def test_empty_fallback_end_then_late_cash_cannot_enable_buy(self):
        def summary(req_id):
            for row in usd_rows()[:-1]:
                self.ib.wrapper.accountSummary(req_id, "DU123", *row)
            self.ib.wrapper.accountSummaryEnd(req_id)
        def cash():
            self.ib.wrapper.accountDownloadEnd("DU123")
            self.ib.wrapper.updateAccountValue("CashBalance", "700", "USD", "DU123")
        self.ib.summary_script, self.ib.cash_script = summary, cash
        result = self.service.refresh("DU123")
        self.assertFalse(result["ready"])
        self.assertIsNone(result["usd_cash"])

    def test_request_exception_still_cancels_and_restores(self):
        def fail(req_id):
            raise ConnectionError("synthetic socket error")
        self.ib.summary_script = fail
        result = self.service.refresh("DU123")
        self.assertFalse(result["ready"])
        self.assertIn("ConnectionError", result["error"])
        self.assertEqual(self.ib.cancelled, [101])
        self.assertNotIn("accountSummary", vars(self.ib.wrapper))

    def test_disconnected_returns_unknown_without_request(self):
        self.ib.connected = False
        result = self.service.refresh("DU123")
        self.assertFalse(result["ready"])
        self.assertIsNone(result["cash"])
        self.assertEqual(self.ib.requests, [])

    def test_diagnostics_contain_names_not_amounts(self):
        self.service.refresh("DU123")
        text = "\n".join(self.logs)
        self.assertIn("CashBalance/USD", text)
        self.assertNotIn("1500", text)
        self.assertNotIn("1000", text)

    @unittest.skipUnless(find_spec("ib_async"), "ib_async is optional for this wrapper compatibility test")
    def test_real_ib_async_wrapper_with_mocked_transport(self):
        from ib_async import IB
        ib = IB()
        ib.isConnected = lambda: True
        ib.client.getReqId = lambda: 301
        ib.client.cancelAccountSummary = Mock()
        def transmit_mock(req_id, group, tags):
            # Exercise actual decoder handlers, which must look up the patched
            # wrapper methods dynamically rather than retain an old callback.
            ib.client.decoder.handlers[63](["63", "1", str(req_id - 1), "DU123", "CashBalance", "999999", "USD"])
            for row in usd_rows():
                ib.client.decoder.handlers[63](["63", "1", str(req_id), "DU123", *row])
            ib.client.decoder.handlers[64](["64", "1", str(req_id)])
        ib.client.reqAccountSummary = Mock(side_effect=transmit_mock)
        view = AccountData(ib, timeout=.1).refresh("DU123")
        self.assertTrue(view["ready"])
        self.assertEqual(view["cash"], 800)
        ib.client.cancelAccountSummary.assert_called_once_with(301)
        self.assertNotIn("accountSummary", vars(ib.wrapper))
        self.assertNotIn("accountSummaryEnd", vars(ib.wrapper))
        self.assertEqual(ib.wrapper._futures, {})
        # Avoid ib_async destructor treating this synthetic connection as real.
        ib.isConnected = lambda: False

    @unittest.skipUnless(find_spec("ib_async"), "ib_async is optional for this wrapper compatibility test")
    def test_real_ib_async_dated_fallback_decoder_with_mocked_transport(self):
        from ib_async import IB
        ib = IB()
        ib.isConnected = lambda: True
        ib.client.getReqId = lambda: 302
        ib.client.cancelAccountSummary = Mock()
        def summary(req_id, group, tags):
            for row in usd_rows():
                if row[0] != "SettledCash":
                    ib.client.decoder.handlers[63](["63", "1", str(req_id), "DU123", *row])
            ib.client.decoder.handlers[64](["64", "1", str(req_id)])
        def account_updates(active, account):
            if active:
                ib.client.decoder.handlers[6](["6", "2", "$LEDGER-SettledCashByDate", "20000101:450", "USD", account])
                ib.client.decoder.handlers[6](["6", "2", "AccountReady", "true", "", account])
                ib.client.decoder.handlers[6](["6", "2", "TradingType-S", "CASH", "", account])
                ib.client.decoder.handlers[54](["54", "1", account])
        ib.client.reqAccountSummary = Mock(side_effect=summary)
        ib.client.reqAccountUpdates = Mock(side_effect=account_updates)
        try:
            view = AccountData(ib, timeout=.1).refresh("DU123")
            self.assertTrue(view["ready"])
            self.assertEqual(view["cash"], 450)
            self.assertEqual(view["trading_type"], "CASH")
            self.assertNotIn("updateAccountValue", vars(ib.wrapper))
            self.assertNotIn("accountDownloadEnd", vars(ib.wrapper))
            self.assertEqual(ib.wrapper._futures, {})
        finally:
            ib.isConnected = lambda: False


if __name__ == "__main__":
    unittest.main()
