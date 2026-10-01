import json
import math
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from iv_rank_di import calculate  # noqa: E402
from update_iv_di_history import (  # noqa: E402
    constant_maturity_iv,
    curve_session_dates,
    existing_tickers,
    normalize_tickers,
    parse_instruments,
    parse_instruments_many,
    parse_price_report,
    retention_window,
    select_target_days,
)


class SourceParserTests(unittest.TestCase):
    def test_ticker_list_accepts_spaces_commas_and_removes_duplicates(self):
        self.assertEqual(
            normalize_tickers(["petr4, vale3", "PETR4", "itub4"]),
            ["PETR4", "VALE3", "ITUB4"],
        )

    def test_instruments_csv_filters_underlying(self):
        raw = (
            "Status do Arquivo: Final\n"
            "TckrSymb;OptnTp;UndrlygTckrSymb1;OptnStyle;ExrcPric;XprtnDt;ISIN\n"
            "PETRJ420;CALL;PETR4;EUROPEAN;42,00;20261016;BRPETRACNOR9\n"
            "VALEJ600;CALL;VALE3;EUROPEAN;60,00;20261016;BRVALEACNOR0\n"
        ).encode("iso-8859-1")
        result = parse_instruments(raw, "PETR4")
        self.assertEqual(list(result), ["PETRJ420"])
        self.assertEqual(result["PETRJ420"]["strike"], 42.0)
        self.assertEqual(result["PETRJ420"]["style"], "Europeu")

    def test_instruments_csv_groups_multiple_underlyings_in_one_pass(self):
        raw = (
            "Status do Arquivo: Final\n"
            "TckrSymb;OptnTp;UndrlygTckrSymb1;OptnStyle;ExrcPric;XprtnDt;ISIN\n"
            "PETRJ420;CALL;PETR4;EUROPEAN;42,00;20261016;BRPETRACNOR9\n"
            "VALEJ600;CALL;VALE3;EUROPEAN;60,00;20261016;BRVALEACNOR0\n"
            "ABEVJ150;CALL;ABEV3;EUROPEAN;15,00;20261016;BRABEVACNOR1\n"
        ).encode("iso-8859-1")
        result = parse_instruments_many(raw, {"PETR4", "VALE3"})
        self.assertEqual(list(result["PETR4"]), ["PETRJ420"])
        self.assertEqual(list(result["VALE3"]), ["VALEJ600"])

    def test_instruments_uses_asset_for_etf_underlying(self):
        raw = (
            "Status do Arquivo: Final\n"
            "TckrSymb;Asst;OptnTp;UndrlygTckrSymb1;OptnStyle;ExrcPric;XprtnDt;ISIN\n"
            "BOVAA1;BOVA11;CALL;;EUROPEAN;176,00;20280121;BRBOVA9A14O7\n"
        ).encode("iso-8859-1")
        result = parse_instruments(raw, "BOVA11")
        self.assertEqual(list(result), ["BOVAA1"])

    def test_price_report_prefers_last_price(self):
        xml = b"""<?xml version='1.0'?>
        <BizData xmlns='urn:test'><PricRpt><TradDt><Dt>2026-09-30</Dt></TradDt>
        <SctyId><TckrSymb>PETRJ420</TckrSymb></SctyId><FinInstrmAttrbts>
        <TradAvrgPric>2.15</TradAvrgPric><LastPric>2.10</LastPric><RglrTxsQty>12</RglrTxsQty>
        </FinInstrmAttrbts></PricRpt></BizData>"""
        result = parse_price_report(xml)
        self.assertEqual(result["PETRJ420"]["price"], 2.10)
        self.assertEqual(result["PETRJ420"]["trades"], 12)


class AggregationTests(unittest.TestCase):
    def test_latest_only_selects_just_newest_di_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "history.json"
            path.write_text(json.dumps({
                "historico": [
                    {"data": "2026-09-29"},
                    {"data": "2026-09-30"},
                ]
            }), encoding="utf-8")
            result = select_target_days(
                path, date(2026, 9, 30), 250, latest_only=True
            )
        self.assertEqual(result, [date(2026, 9, 30)])

    def test_all_existing_discovers_history_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "VALE3.json").touch()
            (root / "PETR4.json").touch()
            self.assertEqual(existing_tickers(root), ["PETR4", "VALE3"])

    def test_small_test_window_does_not_shrink_existing_history(self):
        self.assertEqual(retention_window(1, 250), 250)
        self.assertEqual(retention_window(250, 35), 250)

    def test_curve_dates_limit_collection_to_available_di_sessions(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "history.json"
            path.write_text(json.dumps({
                "historico": [
                    {"data": "2025-09-30"},
                    {"data": "2025-10-01"},
                    {"data": "2025-10-02"},
                ]
            }), encoding="utf-8")
            result = curve_session_dates(
                path, end=date.fromisoformat("2025-10-01"), sessions=250
            )
        self.assertEqual(result, [
            date.fromisoformat("2025-09-30"),
            date.fromisoformat("2025-10-01"),
        ])

    def test_constant_maturity_interpolates_total_variance(self):
        rows = [
            {"expiry": "2026-10-16", "du": 10, "iv": 0.20, "options": 4},
            {"expiry": "2026-11-20", "du": 30, "iv": 0.30, "options": 5},
        ]
        iv, method, used = constant_maturity_iv(rows, 21)
        weight = 11 / 20
        variance = (0.20**2 * 10 / 252) + weight * (
            0.30**2 * 30 / 252 - 0.20**2 * 10 / 252
        )
        self.assertAlmostEqual(iv, math.sqrt(variance / (21 / 252)))
        self.assertEqual(method, "variancia_total_21du")
        self.assertEqual(len(used), 2)

    def test_rank_and_percentile(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "PETR4.json"
            series = [
                {"date": f"2026-01-{day:02d}", "iv": 0.20 + day / 1000}
                for day in range(1, 21)
            ]
            path.write_text(json.dumps({
                "ativo": "PETR4",
                "metodologia": "teste",
                "serie_iv_diaria": series,
            }), encoding="utf-8")
            result = calculate(path)
        self.assertEqual(result["iv_rank"], 100.0)
        self.assertEqual(result["iv_percentil"], 95.0)

    def test_rank_supports_call_and_put_series(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "PETR4.json"
            series = [
                {
                    "date": f"2026-01-{day:02d}",
                    "iv": 0.30,
                    "iv_call": 0.20 + day / 1000,
                    "iv_put": 0.40 - day / 1000,
                }
                for day in range(1, 21)
            ]
            path.write_text(json.dumps({
                "ativo": "PETR4",
                "metodologia": "teste",
                "serie_iv_diaria": series,
            }), encoding="utf-8")
            call = calculate(path, series_type="call")
            put = calculate(path, series_type="put")
        self.assertEqual(call["iv_rank"], 100.0)
        self.assertEqual(put["iv_rank"], 0.0)


if __name__ == "__main__":
    unittest.main()
