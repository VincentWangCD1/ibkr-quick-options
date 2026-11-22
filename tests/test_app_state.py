"""Controller regression tests using fake variables/widgets; never creates Tk."""
import unittest
import queue
import time
import json
from unittest.mock import Mock, patch

from app import App, CONTROL_REQUESTS, ORDER_REQUESTS, CLOSE_TYPES


class Variable:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class Widget:
    def __init__(self, **options):
        self.options = options

    def configure(self, **options):
        self.options.update(options)

    def grid(self):
        self.options["hidden"] = False

    def grid_remove(self):
        self.options["hidden"] = True

    def grid_configure(self, **options):
        self.options.update(options)


class Tree:
    def __init__(self):
        self.rows = {"old-demo-row": {"cash": 1000}}
        self.view = (0.0, 1.0)
        self.scroll_calls = []
        self.deleted = []
        self.selected = ()

    def get_children(self):
        return tuple(self.rows)

    def delete(self, *keys):
        for key in keys:
            self.deleted.append(key)
            self.rows.pop(key)

    def insert(self, parent, index, iid=None, **options):
        iid = iid or f"row-{len(self.rows)}"
        self.rows[iid] = options
        self.move(iid, parent, index)

    def item(self, iid, **options):
        self.rows[iid].update(options)

    def move(self, iid, parent, index):
        value = self.rows.pop(iid)
        items = list(self.rows.items())
        items.insert(len(items) if index == "end" else index, (iid, value))
        self.rows = dict(items)

    def yview(self):
        return self.view

    def yview_moveto(self, fraction):
        self.scroll_calls.append(fraction)
        self.view = (fraction, min(1.0, fraction + .2))

    def winfo_height(self):
        return 256

    def selection(self):
        return self.selected

    def selection_set(self, iid):
        self.selected = (iid,)


def controller():
    # Bypass App.__init__: no root, worker, event loop, window or broker.
    ui = App.__new__(App)
    ui.init_entry_state()
    ui.init_position_ui_state()
    ui.init_history_ui_state()
    ui.last_expiry_refresh = 99.5
    ui.root = Mock()
    ui.update_enabled = Mock()
    ui.log = Mock()
    ui.render_preview = Mock()
    ui.session_epoch = 5
    ui.active_request = 11
    ui.active_job = None
    ui.pending_requests = {}
    ui.request_generations = {}
    ui.request_sequence = 11
    ui.inbox = queue.Queue()
    ui.worker = Mock()
    ui.busy = True
    ui.connected = ui.financial_ready = True
    ui.locked = False
    ui.session_mode = "DEMO"
    ui.active_symbol = "SPY"
    ui.close_confirmation_open = False
    ui.modify_confirmation_open = False
    ui.orders_by_id = {}
    ui.edit_order_key = None
    ui.order_caption = Variable("")
    ui.modify_mode, ui.modify_limit = Variable("限价"), Variable("1.5")
    ui.entry_mode, ui.entry_limit = Variable("市价"), Variable("")
    ui.entry_limits = {"C": ui.entry_limit, "P": Variable("")}
    ui.open_side = Variable("买入期权（买方）")
    ui.budget_mode, ui.cash_amount, ui.percent_input = Variable("比例 %"), Variable(""), Variable(50)
    ui.selection_mode, ui.exact_strike, ui.seller_quantity = Variable("按档位"), Variable(""), Variable(1)
    ui.side_buttons, ui.side_vars, ui.side_frames = {}, {}, {}
    ui.rounding_mode = Variable("四舍五入")
    for name in ("entry_mode_box", "entry_limit_entry", "modify_mode_box", "modify_limit_entry", "modify_btn", "cancel_selected_btn"):
        setattr(ui, name, Widget())
    ui.entry_limit_entries = {"C": ui.entry_limit_entry, "P": Widget()}
    ui.exact_strike_entry, ui.seller_quantity_entry, ui.budget_input = Widget(), Widget(), Widget()
    ui.close_type = Variable(next(iter(CLOSE_TYPES)))
    ui.close_percent, ui.close_tif = Variable(100), Variable("DAY")
    ui.close_spec_values = {key: Variable(value) for key, value in
                            (("limit_price", "1.5"), ("stop_price", ""), ("trail_mode", "百分比 %"),
                             ("trail_value", ""), ("limit_offset", ""))}
    ui.close_field_widgets = {key: (Widget(), Widget()) for key in ui.close_spec_values}
    ui.close_order_btn, ui.close_type_box, ui.close_percent_entry, ui.close_tif_box = Widget(), Widget(), Widget(), Widget()
    ui.close_stop_label = Widget()
    ui.expiry_symbol = "SPY"
    ui.pending_symbol = ui.suppress_change = False
    ui.need_expiries = ui.need_classes = ui.preview_dirty = ui.chain_dirty = True
    ui.chain_offset = 0
    ui.chain_width = Variable(40)
    ui.chain_center_pending = True
    ui.chain_render_key = None
    ui.chain_row_height = 26
    ui.reference_price = None
    ui.reference_pending = False
    ui.reference_input = Variable("")
    ui.chain_has_prev = ui.chain_has_next = True
    ui.last_snapshot = ui.last_funds = ui.last_chain = ui.last_preview = 10.0
    ui.mode = Variable("IBKR 实盘")
    ui.expiry = Variable("20261111")
    ui.trading_class = Variable("SPY")
    ui.moneyness = Variable("虚值 OTM")
    ui.previous_moneyness = "虚值 OTM"
    ui.depth = Variable(4)
    ui.expiry_box = Widget(values=("20261111",))
    ui.class_box = Widget(values=("SPY",))
    ui.snapshot_data = {"mode": "DEMO", "cash": 1000}
    ui.positions_by_id = {"old-demo-row": {"quantity": 21}}
    ui.preview_data = {"token": "demo-token", "cash": 1000, "quantity": 21}
    ui.preview_fingerprint = "old-demo-form"
    ui.match_text = Variable("$1,000.00 [演示]")
    ui.buy_btn = Widget(text="市价买入 21 张")
    ui.chain_tree, ui.pos_tree, ui.order_tree = Tree(), Tree(), Tree()
    ui.chain_caption = Variable("SPY [演示]")
    ui.chain_status = Variable("旧期权链状态")
    ui.position_note = Variable("旧持仓")
    ui.fund_vars = {key: Variable("$1,000.00（演示虚构）") for key in ("equity", "usd_cash", "settled_cash", "order_cash")}
    ui.fund_detail = Variable("演示虚构")
    ui.banner = Variable("DEMO 已连接")
    ui.status = Variable("当前状态")
    ui.fingerprint = Mock(return_value="new-form")
    return ui


def chain_data(spot=100.5, **changes):
    quote = dict(bid=1.2, ask=1.3, last=1.25, quote_type="实时订阅", bid_age=2, ask_age=12)
    data = dict(symbol="SPY", expiry="20261111", trading_class="SPY", spot=spot,
                rows=[dict(strike=strike, call=quote.copy(), put=quote.copy()) for strike in range(81, 121)],
                offset=0, has_prev=True, has_next=True, spot_ref={"kind": "实时标的", "source": "IBKR"})
    data.update(changes)
    return data


class AppStateTests(unittest.TestCase):
    def selected_close_controller(self):
        ui = controller()
        ui.positions_by_id = {"42": dict(conid=42, local_symbol="SPXW TEST", quantity=7)}
        ui.pos_tree.selection = Mock(return_value=("42",))
        ui.close_percent.set("50")
        ui.close_spec_values["limit_price"].set("2.5")
        ui.close_spec_values["stop_price"].set("irrelevant hidden value")
        ui.snapshot_data = {"account": "U123456"}
        ui.session_mode = "LIVE"
        return ui

    def test_custom_close_requires_confirmation_and_queues_exact_spec_once(self):
        ui = self.selected_close_controller()
        with patch("app.messagebox.askyesno", return_value=True) as ask:
            ui.submit_selected_close_order()
            ui.submit_selected_close_order()
        ask.assert_called_once()
        confirmation = ask.call_args.args[1]
        for expected in ("IBKR 实盘", "U123456", "SPXW TEST", "3 张", "50%", "LMT", "2.5"):
            self.assertIn(expected, confirmation)
        self.assertEqual(ui.pending_requests["close"]["args"],
                         {"conid": 42, "percent": 50.0, "order_spec": {"type": "LMT", "tif": "DAY", "limit_price": 2.5}})
        self.assertFalse(ui.close_confirmation_open)

    def test_canceling_sell_confirmation_creates_no_broker_job(self):
        ui = self.selected_close_controller()
        with patch("app.messagebox.askyesno", return_value=False):
            ui.submit_selected_close_order()
        self.assertEqual(ui.pending_requests, {})
        self.assertTrue(ui.inbox.empty())
        self.assertIn("未提交", ui.status.get())

    def test_changed_account_or_position_during_confirmation_requires_reconfirm(self):
        for change in ("account", "quantity"):
            ui = self.selected_close_controller()
            def confirm(*args, **kwargs):
                if change == "account":
                    ui.clear_session_view("已断开。")
                else:
                    ui.positions_by_id["42"]["quantity"] = 8
                return True
            with self.subTest(change=change), patch("app.messagebox.askyesno", side_effect=confirm):
                ui.submit_selected_close_order()
                self.assertEqual(ui.pending_requests, {})
                self.assertTrue(ui.inbox.empty())
                self.assertIn("重新", ui.status.get())

    def test_close_type_controls_show_only_required_fields_and_initial_stop_label(self):
        ui = self.selected_close_controller()
        ui.close_type.set("移动限价止损（TRAIL LIMIT）")
        ui.update_close_order_fields()
        self.assertEqual(ui.close_stop_label.options["text"], "初始止损价")
        self.assertTrue(ui.close_field_widgets["limit_price"][0].options["hidden"])
        for key in ("stop_price", "trail_mode", "trail_value", "limit_offset"):
            self.assertFalse(ui.close_field_widgets[key][0].options["hidden"])
        ui.close_type.set("限价止盈（LMT）")
        ui.update_close_order_fields()
        self.assertFalse(ui.close_field_widgets["limit_price"][0].options["hidden"])
        for key in ("stop_price", "trail_mode", "trail_value", "limit_offset"):
            self.assertTrue(ui.close_field_widgets[key][0].options["hidden"])

    def test_quick_market_close_never_uses_other_sell_method_fields(self):
        ui = self.selected_close_controller()
        ui.close_type.set("移动限价止损（TRAIL LIMIT）")
        ui.mutate = Mock()
        ui.close_selected(100)
        ui.mutate.assert_called_once_with("close", conid=42, percent=100)

    def test_order_table_includes_price_trigger_trail_and_tif(self):
        ui = controller()
        data = dict(connected=True, mode="LIVE", account="U123456", positions=[],
                    financial=dict(ready=True, cash=800), orders=[dict(order_id=12, local_symbol="SPXW test", action="SELL",
                    order_type="TRAIL LIMIT", limit_price=None, stop_price=1.2, trail_mode="percent", trail_value=5,
                    limit_offset=.1, tif="GTC", quantity=3, filled=0, remaining=3, status="Submitted", avg_fill_price=0)])
        ui.render_snapshot(data)
        row = next(iter(ui.order_tree.rows.values()))["values"]
        self.assertEqual(row[3:5], ("TRAIL LIMIT", "—"))
        self.assertIn("触发 1.2", row[5])
        self.assertIn("移动 5%", row[5])
        self.assertIn("限价偏移 0.1", row[5])
        self.assertEqual(row[6], "GTC")

    def test_queued_preview_is_dropped_if_cash_percentage_changes_before_dispatch(self):
        ui = controller()
        ui.busy = ui.need_expiries = ui.need_classes = False
        ui.right, ui.percent = Variable("C"), Variable(50)
        ui.slippage, ui.fee, ui.min_fee, ui.cap = Variable(5), Variable(1), Variable(1), Variable(10000)
        del ui.fingerprint  # Exercise the real settings fingerprint.
        ui.submit("chain", symbol="SPY", expiry=ui.expiry.get(), trading_class="SPY", offset=0, limit=40)
        running = ui.active_job["context"]
        ui.request_preview()
        self.assertEqual(ui.pending_requests["preview_pair"]["args"]["settings"]["percent"], 50)
        ui.percent_input.set(25)
        ui.changed()
        ui.handle_result("chain", None, "无行情", running)
        ui.dispatch_pending()
        self.assertEqual(ui.pending_requests, {})
        self.assertFalse(ui.busy)
        self.assertEqual(ui.inbox.qsize(), 1)  # Old preview was never sent to the broker.
        self.assertTrue(ui.preview_dirty)

    def test_buy_queued_during_preview_uses_exact_clicked_token_not_new_preview(self):
        ui = controller()
        ui.busy = ui.need_expiries = ui.need_classes = False
        settings = {"symbol": "SPY", "percent": 50}
        ui.fingerprint.return_value = json.dumps(settings, sort_keys=True)
        ui.preview_fingerprint = ui.fingerprint()
        ui.preview_data.update(token="clicked-old-token", created_at=time.time(), contract={"local_symbol": "SPY clicked"})
        ui.submit("preview", background=True, settings=settings)
        running = ui.active_job["context"]
        ui.buy()
        self.assertEqual(ui.pending_requests["buy"]["args"], {"token": "clicked-old-token"})
        ui.handle_result("preview", {"token": "new-background-token", "quantity": 19}, None, running)
        self.assertIsNone(ui.preview_data)
        ui.render_preview.assert_not_called()
        ui.dispatch_pending()
        self.assertEqual(ui.active_job["name"], "buy")
        self.assertEqual(ui.active_job["args"], {"token": "clicked-old-token"})
        ui.inbox.get_nowait()  # The already-running background preview.
        self.assertEqual(ui.inbox.get_nowait()[:2], ("buy", {"token": "clicked-old-token"}))

    def test_successful_background_funds_refresh_keeps_existing_preview_token(self):
        ui = controller()
        ui.busy = False
        ui.session_mode = "LIVE"
        ui.preview_data["token"] = "current-session-token"
        clicked_preview = ui.preview_data
        ui.submit("refresh_funds", background=True)
        running = ui.active_job["context"]
        self.assertIs(ui.preview_data, clicked_preview)
        data = dict(connected=True, mode="LIVE", account="U123456", positions=[], orders=[],
                    financial=dict(ready=True, cash=800, usd_cash=1000, settled_cash=800,
                                   net_liquidation=1200, net_liquidation_currency="USD"))
        ui.handle_result("refresh_funds", data, None, running)
        self.assertIs(ui.preview_data, clicked_preview)
        self.assertEqual(ui.preview_data["token"], "current-session-token")
        self.assertTrue(ui.financial_ready)
        self.assertFalse(ui.busy)

    def test_extended_session_preview_shows_limit_and_estimated_reference(self):
        ui = controller()
        preview = dict(contract=dict(local_symbol="SPXW test", expiry="20261111", right="C", strike=6000, multiplier=100),
                       spot=6000.1, bid=1, ask=1.1, quote_type="实时", budget=500, quantity=4,
                       estimated_premium=440, fee_reserve=4, buffered_cost=466, created_at=time.time(),
                       order_type="LMT", limit_price=1.15, session_label="延长交易时段", outside_rth=True,
                       spot_source="SPY × 已校准比例", spot_estimated=True)
        App.render_preview(ui, preview)
        self.assertIn("限价 ≤ 1.15", ui.buy_btn.options["text"])
        self.assertNotIn("市价", ui.buy_btn.options["text"])
        self.assertIn("估算参考，仅用于选档", ui.match_text.get())
        self.assertIn("延长交易时段", ui.match_text.get())
        preview.update(order_type="MKT", limit_price=None, session_label="常规交易时段", spot_estimated=False)
        App.render_preview(ui, preview)
        self.assertIn("市价买入", ui.buy_btn.options["text"])

    def test_background_query_does_not_disable_read_controls_or_valid_buy(self):
        ui = controller()
        ui.active_job = {"name": "chain"}
        ui.connect_controls = []
        ui.preview_data["created_at"] = time.time()
        for name in ("connect_btn", "disconnect_btn", "funds_btn", "depth_box", "preview_btn", "flatten_btn",
                     "cancel_btn", "chain_refresh_btn", "chain_width_box", "chain_atm_btn", "chain_prev_btn", "chain_next_btn"):
            setattr(ui, name, Widget())
        ui.close_buttons = [Widget()]
        App.update_enabled(ui)
        for name in ("disconnect_btn", "funds_btn", "preview_btn", "chain_refresh_btn", "chain_prev_btn", "chain_next_btn", "buy_btn"):
            self.assertEqual(getattr(ui, name).options["state"], "normal", name)
        ui.pending_requests["buy"] = {"name": "buy"}
        App.update_enabled(ui)
        self.assertEqual(ui.buy_btn.options["state"], "disabled")
        self.assertEqual(ui.close_buttons[0].options["state"], "disabled")
        self.assertEqual(ui.disconnect_btn.options["state"], "normal")
        self.assertEqual(ui.chain_next_btn.options["state"], "normal")

    def test_manual_queries_coalesce_while_worker_remains_serial(self):
        ui = controller()
        ui.busy = False
        args = dict(symbol="SPY", expiry=ui.expiry.get(), trading_class="SPY", offset=0, limit=40, reference_price=None)
        self.assertTrue(ui.submit("chain", background=True, **args))
        first = ui.active_job
        self.assertTrue(ui.submit("refresh_funds"))
        self.assertTrue(ui.submit("refresh_funds"))
        ui.chain_offset = 40
        ui.submit("chain", **{**args, "offset": 40})
        ui.chain_offset = 80
        ui.submit("chain", **{**args, "offset": 80})
        self.assertEqual(ui.inbox.qsize(), 1)
        self.assertEqual(set(ui.pending_requests), {"chain", "refresh_funds"})
        self.assertEqual(ui.pending_requests["chain"]["args"]["offset"], 80)
        self.assertFalse(ui.context_is_current("chain", first["context"]))

    def test_returning_to_inflight_target_replaces_newer_queued_target(self):
        ui = controller()
        ui.busy = False
        args = dict(symbol="SPY", expiry=ui.expiry.get(), trading_class="SPY", offset=0, limit=40)
        ui.submit("chain", **args)
        original = ui.active_job["context"]
        ui.chain_offset = 40
        ui.submit("chain", **{**args, "offset": 40})
        ui.chain_offset = 0
        ui.submit("chain", **args)
        self.assertEqual(ui.pending_requests["chain"]["args"]["offset"], 0)
        self.assertFalse(ui.context_is_current("chain", original))

    def test_trade_is_queued_once_and_runs_before_waiting_read(self):
        ui = controller()
        ui.busy = False
        ui.submit("chain", symbol="SPY", expiry=ui.expiry.get(), trading_class="SPY", offset=0, limit=40)
        first = ui.active_job
        ui.submit("refresh_funds")
        self.assertTrue(ui.submit("buy", token="clicked-token"))
        self.assertFalse(ui.submit("buy", token="clicked-token"))
        self.assertFalse(ui.submit("close_all", symbol="SPY"))
        self.assertEqual(ui.inbox.qsize(), 1)
        ui.handle_result("chain", None, "暂缺行情", first["context"])
        ui.dispatch_pending()
        self.assertEqual(ui.active_job["name"], "buy")
        self.assertEqual(set(ui.pending_requests), {"refresh_funds"})
        self.assertEqual(ui.inbox.qsize(), 2)

    def test_disconnect_clears_pending_actions_and_invalidates_active_read_immediately(self):
        ui = controller()
        ui.busy = False
        ui.submit("chain", symbol="SPY", expiry=ui.expiry.get(), trading_class="SPY", offset=0, limit=40)
        original = ui.active_job["context"]
        ui.submit("refresh_funds")
        ui.submit("buy", token="not-yet-submitted")
        ui.disconnect()
        self.assertFalse(ui.connected)
        self.assertIsNone(ui.preview_data)
        self.assertEqual(set(ui.pending_requests), {"disconnect"})
        self.assertFalse(ui.context_is_current("chain", original))
        ui.worker.interrupt_read.assert_called_once_with(original["id"])
        ui.handle_result("chain", None, "已取消", original)
        ui.dispatch_pending()
        self.assertEqual(ui.active_job["name"], "disconnect")
        self.assertNotIn("buy", ui.pending_requests)

    def test_one_second_chain_poll_and_due_preview_both_get_scheduled(self):
        ui = controller()
        ui.outbox = queue.Queue()
        ui.busy = ui.need_expiries = ui.need_classes = ui.preview_dirty = ui.chain_dirty = False
        ui.last_snapshot = ui.last_funds = ui.last_preview = 99.5
        ui.last_chain = 99.0
        ui.auto = Variable(True)
        ui.chain_tab = "chain"
        ui.notebook = Mock()
        ui.notebook.select.return_value = "chain"
        ui.request_chain, ui.request_preview = Mock(), Mock()
        with patch("app.time.monotonic", return_value=100.0):
            ui.poll()
        ui.request_chain.assert_called_once()
        ui.last_preview = 94.0
        with patch("app.time.monotonic", return_value=100.0):
            ui.poll()
        ui.request_preview.assert_called_once()  # A continuously due chain cannot starve preview.
        self.assertEqual(ui.request_chain.call_count, 1)

    def test_initial_chain_centers_reference_but_quote_updates_keep_manual_scroll(self):
        ui = controller()
        ui.preview_data = None
        ui.chain_tree.rows.clear()
        ui.render_chain(chain_data())
        ids = ui.chain_tree.get_children()
        self.assertEqual(ids.index("reference-price"), 20)
        self.assertAlmostEqual(ui.chain_tree.scroll_calls[-1], 16 / 41)
        ui.chain_tree.view = (.1, .3)  # User manually scrolls away from ATM.
        ui.chain_tree.scroll_calls.clear()
        ui.chain_tree.deleted.clear()
        updated = chain_data()
        updated["rows"][0]["call"]["bid"] = 2.1
        ui.render_chain(updated)
        self.assertEqual(ui.chain_tree.scroll_calls, [])
        self.assertEqual(ui.chain_tree.deleted, [])
        self.assertEqual(ui.chain_tree.rows["strike-81"]["values"][0], "2.1")
        self.assertIn("买 2s / 卖 12s", ui.chain_tree.rows["strike-81"]["values"][3])
        self.assertNotIn("过期", ui.chain_tree.rows["strike-81"]["values"][3])

    def test_reference_crossing_strike_preserves_top_contract_instead_of_recentering(self):
        ui = controller()
        ui.preview_data = None
        ui.chain_tree.rows.clear()
        ui.render_chain(chain_data())
        ui.chain_tree.view = (25 / 41, 33 / 41)
        top_contract = ui.chain_tree.get_children()[25]
        ui.chain_tree.scroll_calls.clear()
        ui.render_chain(chain_data(102.5))
        expected = ui.chain_tree.get_children().index(top_contract) / 41
        self.assertAlmostEqual(ui.chain_tree.scroll_calls[-1], expected)
        self.assertEqual(ui.chain_tree.get_children().index("reference-price"), 22)

    def test_missing_reference_clears_prices_and_preserves_actionable_reason(self):
        ui = controller()
        ui.preview_data = None
        ui.render_chain(chain_data(None, awaiting_reference=True,
                                   spot_ref={"error": "SPY 无实时或历史权限；可填备用参考价"}))
        self.assertEqual(ui.chain_tree.rows, {})
        self.assertTrue(ui.chain_center_pending)
        self.assertIn("无实时或历史权限", ui.chain_status.get())
        self.assertNotIn("100.5", ui.chain_caption.get())

    def test_width_or_manual_reference_change_discards_inflight_chain(self):
        for change in ("width", "reference", "pending_reference"):
            ui = controller()
            context = {**ui.context(), "offset": 0, "limit": 40}
            if change == "width":
                ui.chain_width.set(20)
                ui.chain_width_changed()
            elif change == "reference":
                ui.reference_price = 99.5
            else:
                ui.reference_pending = True
            with self.subTest(change=change):
                self.assertFalse(ui.context_is_current("chain", context))

    def test_chain_request_uses_visible_width_and_optional_reference_only(self):
        ui = controller()
        ui.busy = ui.need_expiries = ui.need_classes = False
        ui.submit = Mock()
        ui.request_chain()
        self.assertEqual(ui.submit.call_args.kwargs["limit"], 40)
        self.assertIsNone(ui.submit.call_args.kwargs["reference_price"])
        ui.chain_width.set(20)
        ui.chain_width_changed()
        ui.reference_price = 605.25
        ui.request_chain()
        self.assertEqual(ui.submit.call_args.kwargs["limit"], 20)
        self.assertEqual(ui.submit.call_args.kwargs["reference_price"], 605.25)
        ui.right, ui.percent = Variable("C"), Variable(50)
        ui.slippage, ui.fee, ui.min_fee, ui.cap = Variable(5), Variable(1), Variable(1), Variable(10000)
        self.assertNotIn("reference_price", ui.settings())

    def test_manual_reference_validation_and_symbol_change_clear(self):
        ui = controller()
        ui.request_chain = Mock()
        for invalid in ("NaN", "inf", "-1", "0", "hello"):
            ui.reference_input.set(invalid)
            ui.commit_reference()
            self.assertIsNone(ui.reference_price)
            self.assertTrue(ui.reference_pending)
        ui.request_chain.assert_not_called()
        ui.reference_input.set("605.25")
        ui.commit_reference()
        self.assertEqual(ui.reference_price, 605.25)
        self.assertFalse(ui.reference_pending)
        ui.request_chain.assert_called_once()
        ui.symbol, ui.flatten_btn = Variable("XSP"), Widget()
        ui.commit_symbol()
        self.assertEqual(ui.reference_input.get(), "")
        self.assertIsNone(ui.reference_price)

    def test_chain_error_keeps_last_snapshot_and_its_status_survives_preview(self):
        ui = controller()
        context = {**ui.context(), "id": 11, "offset": 0, "limit": 40}
        ui.handle_result("chain", None, "网络暂时中断", context)
        self.assertTrue(ui.chain_tree.rows)
        self.assertIn("上次快照", ui.chain_status.get())
        ui.handle_result("preview", {"token": "current", "quantity": 5}, None,
                         {**ui.context(), "form": "new-form"})
        self.assertIn("网络暂时中断", ui.chain_status.get())

    def test_connected_session_waiting_for_expiries_does_not_say_disconnected(self):
        ui = controller()
        ui.render_snapshot = Mock()
        ui.financial_ready = False
        ui.handle_result("connect", {"connected": True}, None, {**ui.context(), "id": 11})
        self.assertTrue(ui.need_expiries)
        self.assertIn("账户已连接", ui.chain_caption.get())
        self.assertNotIn("未连接", ui.chain_caption.get())

    def test_contract_load_error_remains_visible_in_chain_panel(self):
        ui = controller()
        ui.handle_result("expirations", None, "返回的合约数据不匹配", {**ui.context(), "id": 11})
        self.assertTrue(ui.connected)
        self.assertFalse(ui.need_expiries)
        self.assertIn("载入失败", ui.chain_caption.get())
        self.assertIn("返回的合约数据不匹配", ui.chain_caption.get())
        self.assertEqual(ui.chain_tree.rows, {})

    def test_connection_attempt_clears_demo_amounts_quotes_and_selected_contract(self):
        ui = controller()
        ui.clear_session_view("正在连接真实账户。")
        self.assertEqual(ui.session_epoch, 6)
        self.assertFalse(ui.connected)
        self.assertFalse(ui.financial_ready)
        self.assertIsNone(ui.session_mode)
        self.assertIsNone(ui.preview_data)
        self.assertIsNone(ui.preview_fingerprint)
        self.assertEqual(ui.snapshot_data, {})
        self.assertEqual(ui.positions_by_id, {})
        self.assertEqual((ui.expiry.get(), ui.trading_class.get()), ("", ""))
        self.assertEqual(ui.expiry_box.options["values"], ())
        self.assertEqual(ui.class_box.options["values"], ())
        for tree in (ui.chain_tree, ui.pos_tree, ui.order_tree):
            self.assertEqual(tree.rows, {})
        for variable in ui.fund_vars.values():
            self.assertEqual(variable.get(), "—")
        self.assertNotIn("1,000", ui.match_text.get())
        self.assertNotIn("21", ui.buy_btn.options["text"])
        self.assertIn("未连接", ui.banner.get())
        self.assertNotIn("演示", ui.fund_detail.get())
        self.assertFalse(ui.need_expiries or ui.need_classes or ui.preview_dirty or ui.chain_dirty)

    def test_late_demo_preview_after_disconnect_cannot_restore_amount_or_order_token(self):
        ui = controller()
        old_context = {**ui.context(), "id": 11, "form": "new-form"}
        ui.clear_session_view("已断开。")
        ui.handle_result("preview", {"token": "late-demo", "cash": 1000}, None, old_context)
        self.assertIsNone(ui.preview_data)
        self.assertEqual(ui.fund_vars["usd_cash"].get(), "—")
        self.assertEqual(ui.status.get(), "已断开。")
        ui.render_preview.assert_not_called()

    def test_old_session_completion_does_not_release_a_new_request_busy_lock(self):
        ui = controller()
        context = {**ui.context(), "session": 4, "id": 10}
        ui.handle_result("connect", {"connected": True, "mode": "DEMO"}, None, context)
        self.assertTrue(ui.busy)
        self.assertEqual(ui.active_request, 11)
        self.assertEqual(ui.status.get(), "当前状态")

    def test_parameter_change_discards_inflight_preview_and_keeps_current_status(self):
        ui = controller()
        context = {**ui.context(), "id": 11, "form": "old-form"}
        ui.preview_data = None
        ui.status.set("已改为 25%，等待重新计算。")
        ui.handle_result("preview", {"token": "obsolete-50-percent", "quantity": 21}, None, context)
        self.assertFalse(ui.busy)  # The completed job can release only its own lock.
        self.assertIsNone(ui.preview_data)
        self.assertEqual(ui.status.get(), "已改为 25%，等待重新计算。")
        ui.render_preview.assert_not_called()

    def test_old_chain_error_cannot_erase_new_symbol_chain(self):
        ui = controller()
        context = {**ui.context(), "id": 11, "offset": 0}
        ui.active_symbol = "XSP"
        ui.chain_caption.set("XSP 20261111 实时")
        ui.handle_result("chain", None, "SPY 行情无权限", context)
        self.assertEqual(ui.chain_caption.get(), "XSP 20261111 实时")
        self.assertEqual(ui.status.get(), "当前状态")
        self.assertTrue(ui.chain_tree.rows)
        ui.log.assert_not_called()

    def test_expiry_class_page_or_uncommitted_symbol_invalidates_old_result(self):
        for change in ("expiry", "trading_class", "chain_offset", "pending_symbol"):
            with self.subTest(change=change):
                ui = controller()
                context = {**ui.context(), "offset": 0}
                if change == "expiry":
                    ui.expiry.set("20261112")
                elif change == "trading_class":
                    ui.trading_class.set("SPXW")
                elif change == "chain_offset":
                    ui.chain_offset = 12
                else:
                    ui.pending_symbol = True
                self.assertFalse(ui.context_is_current("chain", context))

    def test_matching_preview_result_can_be_rendered(self):
        ui = controller()
        context = {**ui.context(), "id": 11, "form": "new-form"}
        result = {"token": "current", "quantity": 5}
        ui.handle_result("preview", result, None, context)
        self.assertIs(ui.preview_data, result)
        self.assertEqual(ui.preview_fingerprint, "new-form")
        self.assertFalse(ui.busy)
        ui.render_preview.assert_called_once_with(result)

    def test_category_change_sets_default_without_resetting_later_manual_depth(self):
        ui = controller()
        ui.changed = Mock()
        ui.moneyness.set("实值 ITM")
        ui.moneyness_changed()
        self.assertEqual(ui.depth.get(), 1)
        ui.depth.set(3)  # A deliberate edit after choosing the ITM category.
        ui.moneyness_changed()
        self.assertEqual(ui.depth.get(), 3)
        ui.moneyness.set("虚值 OTM")
        ui.moneyness_changed()
        self.assertEqual(ui.depth.get(), 4)
        ui.moneyness.set("平值 ATM")
        ui.moneyness_changed()
        self.assertEqual(ui.depth.get(), 1)
        self.assertFalse(ui.suppress_change)


if __name__ == "__main__":
    unittest.main()
