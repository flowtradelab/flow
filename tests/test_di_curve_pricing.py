import json
import math
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from di_curve import DiCurve  # noqa: E402
from option_pricing import black_scholes, implied_volatility  # noqa: E402


class CurveTests(unittest.TestCase):
    def setUp(self):
        self.curve = DiCurve({
            "data": "2026-09-30",
            "vertices": [
                {"du": 21, "fator_desconto": 0.99},
                {"du": 42, "fator_desconto": 0.97},
                {"du": 63, "fator_desconto": 0.95},
            ],
        })

    def test_exact_node(self):
        point = self.curve.point(42)
        self.assertEqual(point.interpolation, "vertice")
        self.assertAlmostEqual(point.discount_factor, 0.97)

    def test_interpolates_log_discount_factor(self):
        point = self.curve.point(31)
        weight = (31 - 21) / (42 - 21)
        expected = math.exp(math.log(0.99) + weight * (math.log(0.97) - math.log(0.99)))
        self.assertAlmostEqual(point.discount_factor, expected)

    def test_history_selects_latest_on_or_before(self):
        payload = {
            "historico": [
                {"data": "2026-09-29", "vertices": [
                    {"du": 1, "fator_desconto": 0.99}, {"du": 2, "fator_desconto": 0.98}
                ]},
                {"data": "2026-09-30", "vertices": [
                    {"du": 1, "fator_desconto": 0.98}, {"du": 2, "fator_desconto": 0.97}
                ]},
            ]
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "history.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            curve = DiCurve.from_history(path, date(2026, 9, 29))
        self.assertEqual(curve.session_date, date(2026, 9, 29))


class PricingTests(unittest.TestCase):
    def test_standard_black_scholes_call(self):
        result = black_scholes(100, 100, 252, 0.20, math.exp(-0.05), "C")
        self.assertAlmostEqual(result["preco"], 10.4506, places=4)

    def test_put_call_parity(self):
        df = math.exp(-0.05)
        call = black_scholes(100, 105, 252, 0.25, df, "C")["preco"]
        put = black_scholes(100, 105, 252, 0.25, df, "P")["preco"]
        self.assertAlmostEqual(call - put, 100 - 105 * df, places=10)

    def test_implied_volatility_round_trip(self):
        df = math.exp(-0.12 * 63 / 252)
        premium = black_scholes(40, 42, 63, 0.31, df, "C")["preco"]
        iv = implied_volatility(premium, 40, 42, 63, df, "C")
        self.assertAlmostEqual(iv, 0.31, places=7)


if __name__ == "__main__":
    unittest.main()

