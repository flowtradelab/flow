import importlib.util
import json
import sys
import tempfile
import types
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
# Collection and Yahoo are mocked; tests need only the standard library.
try:
    import requests
except ImportError:
    sys.modules["requests"] = types.ModuleType("requests")
import update_open_interest_hist_30 as hist


def option(underlying="PETR4", expiry="2026-10-16", kind="C", strike=30, oi=10, naked=3):
    return dict(ativo_objeto=underlying, vencimento=expiry, tipo=kind,
                strike=strike, open_interest=oi, qtd_descoberta=naked)


class HistoryTests(unittest.TestCase):
    def test_aggregation_preserves_contract_dimensions(self):
        rows = hist.aggregate([option(), option(oi=20), option("PETR3"),
                               option(kind="P"), option(expiry="2026-11-19")])
        self.assertEqual(len(rows), 4)
        self.assertEqual(sum(x["open_interest"] for x in rows), 60)
        self.assertEqual(next(x["open_interest"] for x in rows
                             if x["ativo_objeto"] == "PETR4" and x["tipo"] == "C"
                             and x["vencimento"] == "2026-10-16"), 30)

    def test_uncovered_sum_and_missing_are_distinct_from_zero(self):
        rows = hist.aggregate([option(naked=4), option(naked=6),
                               option(kind="P", naked=0)])
        self.assertEqual([x["qtd_descoberta"] for x in rows], [10, 0])
        row = option()
        del row["qtd_descoberta"]
        self.assertIsNone(hist.aggregate([row, option()])[0]["qtd_descoberta"])
        with self.assertRaises(ValueError):
            hist.aggregate([option(naked=-1)])

    def test_legacy_history_preserves_unknown_uncovered(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "PETR/oi-hist-30.json"
            item = option()
            del item["qtd_descoberta"]
            hist.save_history(path, "PETR", {"2026-09-17": dict(
                data="2026-09-17", strikes=[item], spot_fechamento={})})
            days = hist.read_histories(Path(tmp))["PETR"]
            self.assertIsNone(days["2026-09-17"]["strikes"][0]["qtd_descoberta"])

    def test_rollover_idempotency_and_latest_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "PETR").mkdir()
            latest = root / "PETR/latest.json"
            latest.write_bytes(b'{"sentinel":true}')
            fetch = lambda day: {"PETR": hist.aggregate([option()])}
            with patch.object(hist, "fetch_day", side_effect=fetch), patch.object(
                hist, "fetch_closes", return_value={}):
                hist.update(root, date(2026, 9, 18))
                path = root / "PETR/oi-hist-30.json"
                first = path.read_bytes()
                self.assertEqual(hist.update(root, date(2026, 9, 18)), 0)
                self.assertEqual(first, path.read_bytes())
                hist.update(root, date(2026, 9, 21))
            payload = json.loads(path.read_text())
            self.assertEqual(payload["total_pregoes"], 30)
            self.assertEqual(payload["historico"][-1]["data"], "2026-09-21")
            self.assertEqual(latest.read_bytes(), b'{"sentinel":true}')

    def test_unpublished_day_not_counted_as_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            def fetch(day):
                return None if day == date(2026, 9, 7) else {"PETR": [option()]}
            with patch.object(hist, "fetch_day", side_effect=fetch), patch.object(
                hist, "fetch_closes", return_value={}):
                hist.update(Path(tmp), date(2026, 9, 18))
            rows = json.loads((Path(tmp) / "PETR/oi-hist-30.json").read_text())["historico"]
            self.assertEqual(len(rows), 30)
            self.assertNotIn("2026-09-07", [x["data"] for x in rows])

    def test_quotes_same_date_missing_and_preservation(self):
        days = {"2026-09-17": dict(strikes=[option()], spot_fechamento={"PETR4": 29}),
                "2026-09-18": dict(strikes=[option(), option("PETR3")])}
        with patch.object(hist, "fetch_closes", return_value={"2026-09-18": 31}):
            hist.enrich({"PETR": days})
        self.assertEqual(days["2026-09-17"]["spot_fechamento"]["PETR4"], 29)
        self.assertEqual(days["2026-09-18"]["spot_fechamento"]["PETR3"], 31)
        with patch.object(hist, "fetch_closes", side_effect=RuntimeError("offline")):
            hist.enrich({"PETR": {"2026-09-16": dict(strikes=[option()])}})
        empty = {"PETR": {"2026-09-16": dict(strikes=[option()])}}
        with patch.object(hist, "fetch_closes", return_value={"2026-09-18": 31}):
            hist.enrich(empty)
        self.assertIsNone(empty["PETR"]["2026-09-16"]["spot_fechamento"]["PETR4"])

    def test_invalid_source_preserves_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "PETR/oi-hist-30.json"
            hist.save_history(path, "PETR", {"2026-09-17": dict(
                data="2026-09-17", strikes=[option()], spot_fechamento={})})
            before = path.read_bytes()
            with patch.object(hist, "fetch_day", side_effect=ValueError("bad source")):
                with self.assertRaises(ValueError):
                    hist.update(Path(tmp), date(2026, 9, 18))
            self.assertEqual(before, path.read_bytes())

    def test_yahoo_parameters_and_date_matching(self):
        class Stamp:
            def date(self):
                return date(2026, 9, 18)
        class Column:
            def items(self):
                return [(Stamp(), 32.5)]
        class Frame:
            empty = False
            def __getitem__(self, key):
                self_key = key
                assert self_key == "Close"
                return Column()
        from unittest.mock import MagicMock
        yf = MagicMock()
        yf.Ticker.return_value.history.return_value = Frame()
        with patch.dict(sys.modules, {"yfinance": yf}):
            result = hist.fetch_closes("PETR4", "2026-09-17", "2026-09-18")
        yf.Ticker.assert_called_once_with("PETR4.SA")
        kwargs = yf.Ticker.return_value.history.call_args.kwargs
        self.assertFalse(kwargs["auto_adjust"])
        self.assertEqual(kwargs["end"], "2026-09-19")
        self.assertEqual(result, {"2026-09-18": 32.5})

if __name__ == "__main__":
    unittest.main()
