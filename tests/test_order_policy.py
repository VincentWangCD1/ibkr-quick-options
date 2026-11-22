import unittest
from core import ValidationError
from order_policy import protected_limit


class LimitGridTests(unittest.TestCase):
    def test_buy_rounds_up_and_sell_down_to_exchange_grid(self):
        tiers = [(0, .05), (3, .10)]
        self.assertEqual(protected_limit("BUY", 2.95, 5, tiers), 3.1)
        self.assertEqual(protected_limit("SELL", 3.1, 5, tiers), 2.9)
        self.assertEqual(protected_limit("BUY", .23, 0, [(0, .01)]), .23)

    def test_no_zero_or_invalid_limit(self):
        for action, price, slip, grid in [
                ("SELL", .01, 5, [(0, .05)]), ("SELL", 1, 100, [(0, .01)]),
                ("BUY", float("nan"), 5, [(0, .01)]), ("BUY", 1, 5, []),
                ("BUY", 1, 5, [(0, 0)]), ("BUY", 1, 5, [(1, .01)]),
                ("BUY", 1, 5, [(0, .01), (0, .05)])]:
            with self.subTest(action=action, price=price, slip=slip, grid=grid):
                with self.assertRaises(ValidationError):
                    protected_limit(action, price, slip, grid)


if __name__ == "__main__":
    unittest.main()
