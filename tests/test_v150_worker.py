"""Headless V1.5 queue/Worker integration; only the in-memory demo is reachable."""
from pathlib import Path
import queue
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import Worker


class V150WorkerOrderFlowTests(unittest.TestCase):
    def setUp(self):
        # These guards fail the test if this demo path creates a Tk window or
        # tries to import the real broker. No socket or IB session is opened.
        self.tk_guard = patch("app.tk.Tk", side_effect=AssertionError("No Tk window in worker test"))
        self.tk_guard.start()
        self.ib_guard = patch.dict(sys.modules, {"ib_broker": None})
        self.ib_guard.start()
        self.inbox, self.outbox = queue.Queue(), queue.Queue()
        self.worker = Worker(self.inbox, self.outbox)
        self.sequence = 0
        self.messages = []
        self.worker.start()

    def tearDown(self):
        self.inbox.put(("shutdown", {}))
        self.worker.join(timeout=3)
        try:
            self.assertFalse(self.worker.is_alive(), "Demo worker did not shut down")
            self.assertIsNotNone(self.worker.loop)
            self.assertTrue(self.worker.loop.is_closed())
        finally:
            self.ib_guard.stop()
            self.tk_guard.stop()

    def call(self, name, **args):
        self.sequence += 1
        context = {"id": self.sequence, "session": 1, "source": "headless-v150-test"}
        self.inbox.put((name, args, context))
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                actual_name, result, error, actual_context = self.outbox.get(
                    timeout=max(.01, deadline - time.monotonic()))
            except queue.Empty:
                break
            if actual_name == "log":
                self.messages.append(result)
                continue
            self.assertEqual(actual_name, name)
            self.assertEqual(actual_context, context)
            self.assertIsNone(error, f"Worker {name} returned: {error}")
            return result
        self.fail(f"Worker response timed out for {name}")

    def pending_amend_cancel_flow(self, mode, limit_price=None):
        connected = self.call("connect", mode="DEMO", host="", port=0,
                              client_id=31, account="", live_ack="")
        self.assertTrue(connected["connected"])
        self.assertEqual((connected["mode"], connected["cash"]), ("DEMO", 1000))
        expiry = self.call("expirations", symbol="SPY")[0]
        settings = dict(symbol="SPY", expiry=expiry, trading_class="SPY", right="C",
                        moneyness="OTM", depth=4, percent=50, slippage_pct=5,
                        commission_per_contract=1, min_commission=1, max_contracts=10000,
                        rounding_mode="nearest", order_mode=mode, limit_price=limit_price)
        preview = self.call("preview", settings=settings)
        self.assertEqual((preview["order_type"], preview["price_mode"]), ("LMT", mode))
        self.assertEqual(preview["rounding_mode"], "nearest")
        self.assertLess(preview["limit_price"], preview["ask"])
        opened = self.call("buy", token=preview["token"])
        self.assertIn("Submitted", opened["status"])
        self.assertEqual((opened["filled"], opened["remaining"]), (0, preview["quantity"]))
        before = self.call("snapshot")
        self.assertEqual(before["positions"], [])
        self.assertEqual(before["financial"]["usd_cash"], 1000)
        self.assertLess(before["cash"], 1000)
        self.assertEqual(before["orders"][0]["limit_price"], preview["limit_price"])

        amend = self.call("preview_modify", order_id=opened["order_id"],
                          order_mode="LMT", limit_price=.18)
        self.assertEqual(amend["method"], "amend")
        self.assertEqual(amend["old_limit_price"], preview["limit_price"])
        self.assertEqual(amend["limit_price"], .18)
        self.assertEqual(amend["remaining"], opened["remaining"])
        changed = self.call("modify_order", token=amend["token"])
        self.assertEqual(changed["order_id"], opened["order_id"])
        self.assertEqual(changed["quantity"], opened["quantity"])
        self.assertEqual(changed["limit_price"], .18)
        self.assertEqual(changed["filled"], 0)
        self.assertIn("Submitted", changed["status"])
        after_amend = self.call("snapshot")
        self.assertGreater(after_amend["cash"], before["cash"])
        self.assertEqual(len(after_amend["orders"]), 1)

        cancelled = self.call("cancel_order", order_id=changed["order_id"])
        self.assertIn("Cancelled", cancelled["status"])
        self.assertEqual(cancelled["filled"], 0)
        final = self.call("snapshot")
        self.assertEqual(final["cash"], 1000)
        self.assertEqual(final["financial"]["usd_cash"], 1000)
        self.assertEqual(final["positions"], [])
        self.assertEqual(len(final["orders"]), 1)
        self.assertIn("Cancelled", final["orders"][0]["status"])

    def test_limit_entry_amend_and_cancel_through_worker_queue(self):
        self.pending_amend_cancel_flow("LMT", .19)

    def test_fixed_midpoint_entry_amend_and_cancel_through_worker_queue(self):
        self.pending_amend_cancel_flow("MID")


if __name__ == "__main__":
    unittest.main()
