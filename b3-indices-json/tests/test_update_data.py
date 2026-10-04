import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "update_data.py"
SPEC = importlib.util.spec_from_file_location("update_data", MODULE_PATH)
update_data = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
sys.modules[SPEC.name] = update_data
SPEC.loader.exec_module(update_data)


class ParsingTests(unittest.TestCase):
    def test_b3_numbers(self):
        self.assertEqual(update_data.parse_b3_number("4.043.349.180", integer=True), 4043349180)
        self.assertEqual(update_data.parse_b3_number("2,450"), 2.45)
        self.assertAlmostEqual(update_data.parse_b3_number("14.030.907,03256302"), 14030907.03256302)

    def test_classification(self):
        value = "Petróleo. Gás e Biocombustíveis / Petróleo. Gás e Biocombustíveis / Exploração. Refino e Distribuição"
        self.assertEqual(
            update_data.split_classification(value),
            {
                "sector": "Petróleo. Gás e Biocombustíveis",
                "subsector": "Petróleo. Gás e Biocombustíveis",
                "segment": "Exploração. Refino e Distribuição",
            },
        )

    def test_atomic_json_write(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "nested" / "data.json"
            update_data.write_json(path, {"á": 1})
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"á": 1})


if __name__ == "__main__":
    unittest.main()
