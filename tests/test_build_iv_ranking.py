import json
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from build_iv_ranking import build_ranking, coverage_label  # noqa: E402


class RankingTests(unittest.TestCase):
    def test_coverage_labels(self):
        self.assertEqual(coverage_label(250, 250), "completo")
        self.assertEqual(coverage_label(220, 250), "alto")
        self.assertEqual(coverage_label(120, 250), "parcial")
        self.assertEqual(coverage_label(50, 250), "curto")

    def test_builds_and_sorts_consolidated_ranking(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for ticker, final_iv in (("ALTA3", 0.50), ("BAIX3", 0.25)):
                series = []
                for index in range(20):
                    iv = 0.20 + index * (final_iv - 0.20) / 19
                    series.append({
                        "date": f"2026-01-{index + 1:02d}",
                        "iv": iv,
                        "iv_call": iv + 0.01,
                        "iv_put": iv - 0.01,
                    })
                (root / f"{ticker}.json").write_text(json.dumps({
                    "ativo": ticker,
                    "metodologia": "teste",
                    "serie_iv_diaria": series,
                }), encoding="utf-8")
            result = build_ranking(root, 250)
        self.assertEqual(result["total_ativos"], 2)
        self.assertEqual(result["ativos"][0]["ticker"], "ALTA3")
        self.assertEqual(result["resumo_cobertura"]["curto"], 2)


if __name__ == "__main__":
    unittest.main()
