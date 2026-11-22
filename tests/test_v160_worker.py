"""V1.6 paired UI/Worker/Demo flow; real broker import and Tk are prohibited."""
import queue
import sys
import time
import unittest
from unittest.mock import patch

from app import Worker
from tests.test_v160_ui import entry_controller


class V160WorkerFlowTests(unittest.TestCase):
    def test_paired_limit_seed_cancel_and_short_cover(self):
        inbox, outbox = queue.Queue(), queue.Queue()
        sequence = 0
        def call(name, **kwargs):
            nonlocal sequence
            sequence += 1
            context = {"id": sequence}
            inbox.put((name, kwargs, context))
            end = time.monotonic() + 3
            while time.monotonic() < end:
                actual, result, error, received = outbox.get(timeout=max(.01, end - time.monotonic()))
                if actual == "log":
                    continue
                self.assertEqual((actual, received), (name, context))
                self.assertIsNone(error, error)
                return result
            self.fail("offline worker timeout")

        with patch("app.tk.Tk", side_effect=AssertionError("No window")), patch.dict(sys.modules, {"ib_broker": None}):
            worker = Worker(inbox, outbox)
            worker.start()
            try:
                call("connect", mode="DEMO", host="", port=0, client_id=31, account="", live_ack="")
                ui = entry_controller()
                ui.expiry.set(call("expirations", symbol="SPY")[0])
                ui.entry_mode.set("限价")
                ui.budget_mode.set("金额 USD")
                ui.cash_amount.set("99")
                pair = call("preview_pair", settings=ui.settings())
                ui.render_pair(pair, {})
                self.assertEqual(set(ui.side_previews), {"C", "P"})
                self.assertEqual(set(ui.limit_conids), {"C", "P"})
                self.assertTrue(all(value.get("limit_seeded") for value in pair.values()))
                self.assertTrue(all(value["buffered_cost"] <= 99 for value in pair.values()))
                order = call("buy", token=pair["P"]["token"])
                self.assertEqual(order["conid"], pair["P"]["contract"]["conid"])
                self.assertGreater(order["remaining"], 0)
                call("cancel_order", order_id=order["order_id"])

                ui.open_side.set("卖出期权（卖方）")
                ui.entry_mode.set("市价")
                ui.budget_mode.set("比例 %")
                ui.percent_input.set("100")
                pair = call("preview_pair", settings=ui.settings())
                ui.render_pair(pair, {})
                self.assertTrue(pair["P"]["opens_short"])
                self.assertIn("预计收入", ui.side_vars["P"]["amount"].get())
                short = call("buy", token=pair["P"]["token"])
                self.assertEqual(short["action"], "SELL")
                snapshot = call("snapshot")
                self.assertEqual(snapshot["positions"][0]["quantity"], -1)
                cover = call("close", conid=short["conid"], percent=100)
                self.assertEqual(cover["action"], "BUY")
                self.assertEqual(call("snapshot")["positions"], [])
            finally:
                inbox.put(("shutdown", {}, None))
                worker.join(timeout=3)
                self.assertFalse(worker.is_alive())


if __name__ == "__main__":
    unittest.main()
