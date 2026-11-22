"""Bounded, single-worker same-order midpoint amendments, explicitly opted in."""
import math
import time
import threading


def policy(values=None):
    values = values or {}
    result = {}
    for key, default, low, high in (("interval", 3, 2, 60), ("duration", 300, 5, 3600), ("drift_pct", 10, 0, 100)):
        try:
            value = float(values.get(key, default))
        except (ValueError, TypeError):
            raise ValueError("自动中间价参数必须为有效数字。") from None
        if not math.isfinite(value) or not low <= value <= high:
            raise ValueError(f"自动中间价 {key} 必须在 {low}–{high} 之间。")
        result[key] = value
    cap = values.get("budget_cap")
    if cap is not None:
        cap = float(cap)
        if not math.isfinite(cap) or cap <= 0:
            raise ValueError("自动追价的总支出上限必须为有效正数。")
        result["budget_cap"] = cap
    return result


class AutoMidManager:
    def __init__(self, broker, clock=time.monotonic):
        self.broker, self.clock = broker, clock
        self.states = {}

    def orders(self):
        method = getattr(self.broker, "orders", None)
        return method() if callable(method) else self.broker.snapshot().get("orders", [])

    @staticmethod
    def identity(order):
        return tuple(order.get(key) for key in ("account", "client_id", "order_id", "order_ref", "conid", "action", "quantity"))

    def start(self, order_id, config=None, order=None, expected=None):
        if self.states.get(int(order_id), {}).get("active"):
            raise ValueError("该订单的自动中间价已在运行，请先停止后重新设置。")
        config = policy(config)
        matches = [o for o in self.orders() if o["order_id"] == int(order_id)] if order is None else [order]
        if len(matches) != 1:
            raise ValueError("无法唯一核对自动中间价订单。")
        order = matches[0]
        if expected is not None and (tuple(expected["identity"]) != self.identity(order)
                                     or expected["limit_price"] != order.get("limit_price")):
            raise ValueError("订单在确认期间已改变，请重新选择并确认追价边界。")
        if order.get("order_type") != "LMT" or order.get("opens_short") or order.get("position_effect") in ("OPEN_SHORT", "CLOSE_SHORT"):
            raise ValueError("自动中间价支持本工具买入开仓/卖出平多头的独立限价单。")
        if order.get("owned") is False or order.get("parent_id") or order.get("oca_group"):
            raise ValueError("不能自动修改其他客户端、组合或联动订单。")
        price = float(order.get("limit_price") or 0)
        if not math.isfinite(price) or price <= 0:
            raise ValueError("原单限价无效，未启用自动改价。")
        now = self.clock()
        bound = price * (1 + config["drift_pct"] / 100 if order["action"] == "BUY" else 1 - config["drift_pct"] / 100)
        state = dict(config=config, identity=self.identity(order), active=True, order_id=int(order_id),
                     started=now, due=now + config["interval"], deadline=now + config["duration"],
                     initial_price=price, last_price=price, bound=bound, amendments=0, message="自动中间价等待下一次检查")
        state["stop_event"] = threading.Event()
        self.states[int(order_id)] = state
        if str(order.get("status", "")).split(" (")[0] in ("Filled", "Cancelled", "ApiCancelled", "Inactive"):
            self.stop(order_id, "订单已结束：" + order["status"])
        return self.describe(order_id)

    def stop(self, order_id=None, reason="用户停止；原限价单继续保留"):
        for key, state in self.states.items():
            if order_id is None or key == int(order_id):
                state["active"] = False
                state["stop_event"].set()
                state["message"] = reason
        return self.describe(order_id) if order_id is not None else {}

    def request_stop(self, order_id=None):
        """UI thread only sets flags, never calls the broker or changes orders."""
        for key, state in list(self.states.items()):
            if order_id is None or key == int(order_id):
                state["stop_event"].set()

    def describe(self, order_id):
        state = self.states.get(int(order_id), {})
        if not state:
            return {}
        return dict(auto_mid_active=state["active"] and not state["stop_event"].is_set(), auto_mid_status=state["message"],
                    auto_mid_count=state["amendments"], auto_mid_bound=state["bound"])

    def decorate(self, result):
        if not isinstance(result, dict):
            return result
        if "orders" in result:
            return {**result, "orders": [{**o, **self.describe(o["order_id"])} if o.get("owned", True) else o for o in result["orders"]]}
        if "order_id" in result:
            return {**result, **self.describe(result["order_id"])}
        return result

    def _budget_ok(self, order, preview, cap):
        if cap is None or order["action"] != "BUY":
            return True
        if hasattr(self.broker, "_filled_debit"):
            paid = self.broker._filled_debit(self.broker._managed_order(order["order_id"], refresh=False))
        else:  # Offline demo uses one dollar per filled contract.
            paid = float(order.get("filled", 0)) * (float(order.get("avg_fill_price", 0)) * 100 + 1)
        fee = max(float(preview.get("min_commission", 1)), float(preview["remaining"]) * float(preview.get("commission_per_contract", 1)))
        return paid + float(preview["remaining"]) * float(preview["limit_price"]) * 100 + fee <= cap + 1e-9

    def tick(self):
        due = [s for s in self.states.values() if s["active"] and self.clock() >= s["due"]]
        if not due:
            return
        try:
            orders = {o["order_id"]: o for o in self.orders()}
        except Exception:
            self.stop(reason="无法核对订单，自动改价已停止；原单请到 TWS 核对")
            return
        for state in due:
            oid = state["order_id"]
            state["due"] = self.clock() + state["config"]["interval"]
            try:
                if state["stop_event"].is_set():
                    self.stop(oid)
                    continue
                if self.clock() >= state["deadline"]:
                    self.stop(oid, "运行时间已到，停止追价；原限价单保留")
                    continue
                order = orders.get(oid)
                if order is None or self.identity(order) != state["identity"]:
                    self.stop(oid, "订单身份/数量改变或连接已丢失，停止追价")
                    continue
                status = str(order.get("status", "")).split(" (")[0]
                if status in ("Filled", "Cancelled", "ApiCancelled", "Inactive"):
                    self.stop(oid, "订单已结束：" + status)
                    continue
                if status not in ("Submitted", "PreSubmitted"):
                    if self.clock() - state["started"] > 15:
                        self.stop(oid, "订单仍未确认，停止追价；请核对原单")
                    continue
                if float(order.get("remaining", 0)) <= 0:
                    if self.clock() - state["started"] > 15:
                        self.stop(oid, "未成交余量仍待确认，停止追价；请核对原单")
                    continue
                if (order.get("order_type") != "LMT" or not order.get("modifiable", True)
                        or abs(float(order["limit_price"]) - state["last_price"]) > 1e-9):
                    self.stop(oid, "原单已手动修改或不可改单，停止追价")
                    continue
                preview = self.broker.preview_modify(oid, "MID")
                price = float(preview["limit_price"])
                if not math.isfinite(price) or price <= 0:
                    raise ValueError("最新中间价无效，未改单")
                if preview["method"] != "amend" or preview["order_type"] != "LMT":
                    raise ValueError("自动模式只允许原订单号限价修改")
                if abs(price - state["last_price"]) < 1e-9:
                    state["message"] = f'自动中间价运行中 · 价格未变 {price:g}'
                    continue
                if (order["action"] == "BUY" and price > state["bound"] + 1e-9
                        or order["action"] == "SELL" and price < state["bound"] - 1e-9):
                    self.stop(oid, f'最新中间价 {price:g} 超出追价边界 {state["bound"]:g}；保留原单')
                    continue
                if not self._budget_ok(order, preview, state["config"].get("budget_cap")):
                    self.stop(oid, "最新中间价超出本次开仓预算，停止追价；保留原单")
                    continue
                if self.clock() >= state["deadline"] or state["stop_event"].is_set():
                    self.stop(oid, "核验期间已停止或到时；未改单")
                    continue
                self.broker._auto_mid_guard = lambda: not state["stop_event"].is_set() and self.clock() < state["deadline"]
                self.broker._auto_mid_budget_cap = state["config"].get("budget_cap")
                try:
                    result = self.broker.modify_order(preview["token"])
                finally:
                    self.broker._auto_mid_guard = None
                    self.broker._auto_mid_budget_cap = None
                state["amendments"] += 1
                state["last_price"] = price
                state["due"] = self.clock() + state["config"]["interval"]
                state["message"] = f'自动中间价 {price:g} · 已改 {state["amendments"]} 次'
                if result.get("modification") == "amend_requested":
                    self.stop(oid, "改单确认未返回，停止自动重试；请核对原单")
                elif str(result.get("status", "")).startswith(("Filled", "Cancelled", "Inactive")):
                    self.stop(oid, "订单已结束：" + result["status"])
            except Exception as exc:
                self.stop(oid, "自动改价停止：" + str(exc))
                self.broker.log(f"订单 {oid} {state['message']}")
