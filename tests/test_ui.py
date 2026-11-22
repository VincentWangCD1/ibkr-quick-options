"""Headless worker integration. Imports Tk module but NEVER creates a window."""
import queue
import asyncio
import threading
import time
import unittest
from app import Worker


class WorkerIntegrationTests(unittest.TestCase):
    def test_delayed_interrupt_for_old_read_cannot_cancel_new_read(self):
        started = threading.Event()
        disconnected = []

        class WaitingBroker:
            def chain(self):
                async def wait():
                    started.set()
                    await asyncio.sleep(.1)
                    return {"current_read": True}
                return asyncio.get_event_loop().run_until_complete(wait())

            def disconnect(self):
                disconnected.append(threading.get_ident())

            def pump(self):
                pass

        self.worker.broker = WaitingBroker()
        self.inbox.put(("chain", {}, {"id": 82}))
        self.assertTrue(started.wait(2))
        self.worker.interrupt_read(81)
        name, result, error, context = self.outbox.get(timeout=2)
        self.assertEqual((name, result, error, context), ("chain", {"current_read": True}, None, {"id": 82}))
        self.assertEqual(disconnected, [])

    def test_disconnect_interrupt_runs_on_worker_and_cancels_only_read_wait(self):
        started = threading.Event()
        disconnected = []

        class WaitingBroker:
            def chain(self):
                async def wait():
                    started.set()
                    await asyncio.sleep(30)
                asyncio.get_event_loop().run_until_complete(wait())

            def disconnect(self):
                disconnected.append(threading.get_ident())

            def pump(self):
                pass

        self.worker.broker = WaitingBroker()
        self.inbox.put(("chain", {}, {"id": 81}))
        self.assertTrue(started.wait(2))
        self.worker.interrupt_read(81)
        name, result, error, context = self.outbox.get(timeout=2)
        self.assertEqual((name, context), ("chain", {"id": 81}))
        self.assertIsNone(result)
        self.assertIn("取消", error)
        self.assertEqual(disconnected, [self.worker.ident])

    def test_interrupt_does_not_cancel_fake_trade_or_newer_request(self):
        started = threading.Event()
        disconnected = []

        class WaitingBroker:
            def buy(self):
                async def wait():
                    started.set()
                    await asyncio.sleep(.15)
                    return {"fake_only": True}
                return asyncio.get_event_loop().run_until_complete(wait())

            def disconnect(self):
                disconnected.append(threading.get_ident())

            def pump(self):
                pass

        self.worker.broker = WaitingBroker()
        self.inbox.put(("buy", {}, {"id": 82}))
        self.assertTrue(started.wait(2))
        self.worker.interrupt_read(81)
        self.worker.interrupt_read(82)
        name, result, error, context = self.outbox.get(timeout=2)
        self.assertIsNone(error)
        self.assertEqual(result, {"fake_only": True})
        self.assertEqual(disconnected, [])

    def setUp(self):
        self.inbox, self.outbox = queue.Queue(), queue.Queue()
        self.worker = Worker(self.inbox, self.outbox)
        self.worker.start()

    def tearDown(self):
        self.inbox.put(("shutdown", {}, None))
        self.worker.join(timeout=3)
        self.assertFalse(self.worker.is_alive())

    def call(self, name, context=None, **args):
        self.inbox.put((name, args, context))
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            result = self.outbox.get(timeout=max(.01, deadline-time.monotonic()))
            if result[0] == "log":
                continue
            self.assertEqual(result[0], name)
            self.assertEqual(result[3], context)
            return result[1], result[2]
        self.fail("worker did not return")

    def connect(self):
        data, error = self.call("connect", mode="DEMO")
        self.assertIsNone(error)
        self.assertTrue(data["connected"])
        self.assertTrue(data["financial"]["ready"])
        return data

    def preview(self, symbol="SPY", pct=50):
        expiries, error = self.call("expirations", symbol=symbol)
        self.assertIsNone(error)
        classes, error = self.call("trading_classes", symbol=symbol, expiry=expiries[0])
        self.assertIsNone(error)
        settings = dict(symbol=symbol, expiry=expiries[0], trading_class=classes[0], right="C", moneyness="OTM", depth=4,
                        percent=pct, slippage_pct=5, commission_per_contract=1, min_commission=1, max_contracts=100)
        preview, error = self.call("preview", context={"session":1,"symbol":symbol}, settings=settings)
        self.assertIsNone(error)
        return preview, settings

    def test_full_demo_workflow_with_chain_context_and_double_buy_guard(self):
        self.connect()
        preview, settings = self.preview()
        chain, error = self.call("chain", context={"symbol":"SPY"}, symbol="SPY", expiry=settings["expiry"], trading_class=settings["trading_class"])
        self.assertIsNone(error)
        self.assertEqual(len(chain["rows"]), 40)
        self.assertIn("call", chain["rows"][0])
        self.assertIn("put", chain["rows"][0])
        order, error = self.call("buy", token=preview["token"])
        self.assertIsNone(error)
        self.assertEqual(order["filled"], preview["quantity"])
        _, error = self.call("buy", token=preview["token"])
        self.assertIsNotNone(error)
        result, error = self.call("close", conid=preview["contract"]["conid"], percent=50)
        self.assertIsNone(error)
        self.assertEqual(result["quantity"], preview["quantity"]//2)
        _, error = self.call("close_all", symbol="SPY")
        self.assertIsNone(error)
        data, error = self.call("refresh_funds")
        self.assertIsNone(error)
        self.assertEqual(data["positions"], [])

    def test_current_symbol_flatten_does_not_close_other_symbols(self):
        self.connect()
        for symbol in ("SPY", "QQQ"):
            preview, _ = self.preview(symbol, pct=20)
            _, error = self.call("buy", token=preview["token"])
            self.assertIsNone(error)
        _, error = self.call("close_all", symbol="QQQ")
        self.assertIsNone(error)
        data, error = self.call("snapshot")
        self.assertIsNone(error)
        self.assertEqual({p["symbol"] for p in data["positions"]}, {"SPY"})

    def test_disconnect_snapshot_has_no_old_funds_or_quotes(self):
        self.connect()
        self.preview()
        self.call("disconnect")
        data, error = self.call("snapshot")
        self.assertIsNone(error)
        self.assertFalse(data["connected"])
        self.assertIsNone(data["cash"])
        self.assertIsNone(data["financial"]["usd_cash"])
        self.assertEqual(data["positions"], [])

    def test_failed_command_returns_context_and_never_retries(self):
        self.connect()
        _, error = self.call("preview", context={"test":"bad"}, settings={"symbol":"INVALID!"})
        self.assertIsNotNone(error)
        data, error = self.call("snapshot")
        self.assertIsNone(error)
        self.assertEqual(data["orders"], [])


if __name__ == "__main__":
    unittest.main()
