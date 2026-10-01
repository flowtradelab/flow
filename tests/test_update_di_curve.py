import importlib.util
import io
import json
import tempfile
import unittest
import zipfile
from datetime import date
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "update_di_curve.py"
SPEC = importlib.util.spec_from_file_location("update_di_curve", MODULE_PATH)
di = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(di)


def xml_payload(include_three=True):
    extra = """
      <PricRpt>
        <TradDt><Dt>2026-09-29</Dt></TradDt>
        <SctyId><TckrSymb>DI1J27</TckrSymb></SctyId>
        <FinInstrmAttrbts><AdjstdQt>93500</AdjstdQt><AdjstdQtTax>13.650</AdjstdQtTax>
          <OpnIntrst>250000</OpnIntrst><FinInstrmQty>12000</FinInstrmQty></FinInstrmAttrbts>
      </PricRpt>
      <PricRpt>
        <TradDt><Dt>2026-09-29</Dt></TradDt>
        <SctyId><TckrSymb>DI1N27</TckrSymb></SctyId>
        <FinInstrmAttrbts><AdjstdQt>90000</AdjstdQt><AdjstdQtTax>13.400</AdjstdQtTax></FinInstrmAttrbts>
      </PricRpt>
    """ if include_three else ""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
    <BizData xmlns="urn:bvmf.217.01.xsd">
      <PricRpt>
        <TradDt><Dt>2026-09-29</Dt></TradDt>
        <SctyId><TckrSymb>DI1F27</TckrSymb></SctyId>
        <FinInstrmAttrbts><AdjstdQt>97000</AdjstdQt><AdjstdQtTax>13.720</AdjstdQtTax>
          <PrvsAdjstdQt>96950</PrvsAdjstdQt><PrvsAdjstdQtTax>13.710</PrvsAdjstdQtTax>
          <OpnIntrst>100000</OpnIntrst><RglrTxsQty>1234</RglrTxsQty></FinInstrmAttrbts>
      </PricRpt>
      {extra}
      <PricRpt>
        <TradDt><Dt>2026-09-29</Dt></TradDt>
        <SctyId><TckrSymb>DOLZ26</TckrSymb></SctyId>
        <FinInstrmAttrbts><AdjstdQt>5.2</AdjstdQt></FinInstrmAttrbts>
      </PricRpt>
    </BizData>""".encode()


class CalendarTests(unittest.TestCase):
    def setUp(self):
        self.holidays = di.load_holidays()

    def test_contract_maturity_is_first_trading_session(self):
        self.assertEqual(di.contract_maturity("DI1F27", self.holidays), date(2027, 1, 4))
        self.assertEqual(di.contract_maturity("DI1K26", self.holidays), date(2026, 5, 4))

    def test_business_day_convention_is_start_inclusive_end_exclusive(self):
        self.assertEqual(
            di.business_days(date(2026, 9, 28), date(2026, 10, 1), self.holidays),
            3,
        )

    def test_calendar_fallback_covers_2025(self):
        self.assertIn(date(2025, 3, 4), self.holidays)
        self.assertIn(date(2025, 11, 20), self.holidays)


class ParserTests(unittest.TestCase):
    def setUp(self):
        self.holidays = di.load_holidays()

    def test_namespaced_xml_filters_and_orders_di1(self):
        sessions = di.parse_payload(xml_payload(), self.holidays)
        self.assertEqual(len(sessions), 1)
        tickers = [row["ticker"] for row in sessions[0]["vertices"]]
        self.assertEqual(tickers, ["DI1F27", "DI1J27", "DI1N27"])
        first = sessions[0]["vertices"][0]
        self.assertEqual(first["taxa_ajuste"], 13.72)
        self.assertEqual(first["open_interest"], 100000)
        self.assertGreater(first["du"], 0)
        self.assertGreater(first["fator_desconto"], 0)

    def test_zip_payload_is_supported(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("BVBG.187.01.xml", xml_payload())
        sessions = di.parse_payload(buffer.getvalue(), self.holidays)
        self.assertEqual(len(sessions[0]["vertices"]), 3)

    def test_validation_rejects_partial_curve(self):
        session = di.parse_payload(xml_payload(include_three=False), self.holidays)[0]
        with self.assertRaisesRegex(ValueError, "Apenas 1"):
            di.validate_session(session)

    def test_rate_can_be_derived_from_pu(self):
        du = 252
        pu = 90_000
        rate = di.rate_from_pu(pu, du)
        self.assertAlmostEqual(rate, 11.111111, places=5)
        self.assertAlmostEqual(100_000 * di.discount_factor(rate, du), pu, places=5)

    def test_public_archive_name(self):
        self.assertEqual(di.legacy_archive_name(date(2026, 9, 30)), "SPRD260930.zip")


class StorageTests(unittest.TestCase):
    def test_history_is_sorted_and_trimmed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "history.json"
            history = {
                "2026-09-29": {"data": "2026-09-29", "vertices": []},
                "2026-09-26": {"data": "2026-09-26", "vertices": []},
                "2026-09-28": {"data": "2026-09-28", "vertices": []},
            }
            di.save_history(history, 2, path)
            payload = json.loads(path.read_text())
            self.assertEqual(payload["total_pregoes"], 2)
            self.assertEqual(
                [row["data"] for row in payload["historico"]],
                ["2026-09-28", "2026-09-29"],
            )

    def test_full_window_accepts_new_session_and_discards_oldest(self):
        old_history = {
            "2026-09-29": {"data": "2026-09-29", "vertices": []},
            "2026-09-30": {"data": "2026-09-30", "vertices": []},
        }
        newest = {"data": "2026-10-01", "vertices": [{}, {}, {}]}
        with (
            mock.patch.object(di, "read_history", return_value=old_history),
            mock.patch.object(di, "load_holidays", return_value=set()),
            mock.patch.object(di, "fetch_session", return_value=(newest, "BVBG.187.01")) as fetch,
            mock.patch.object(di, "save_history"),
            mock.patch.object(di, "atomic_json"),
        ):
            result = di.update(
                end=date(2026, 10, 1),
                sessions=2,
                lookback_days=4,
                cache_dir=Path("cache"),
                delay=0,
            )
        self.assertEqual(sorted(result), ["2026-09-30", "2026-10-01"])
        fetch.assert_called_once()

    def test_fresh_history_stops_when_window_is_complete(self):
        def fake_fetch(day, _cache_dir, _holidays, _refresh):
            return ({"data": day.isoformat(), "vertices": [{}, {}, {}]}, "SPRD")

        with (
            mock.patch.object(di, "read_history", return_value={}),
            mock.patch.object(di, "load_holidays", return_value=set()),
            mock.patch.object(di, "fetch_session", side_effect=fake_fetch) as fetch,
            mock.patch.object(di, "save_history"),
            mock.patch.object(di, "atomic_json"),
        ):
            result = di.update(
                end=date(2026, 9, 30),
                sessions=2,
                lookback_days=30,
                cache_dir=Path("cache"),
                delay=0,
            )
        self.assertEqual(sorted(result), ["2026-09-29", "2026-09-30"])
        self.assertEqual(fetch.call_count, 2)

    def test_update_skips_b3_holiday_without_request(self):
        holiday = date(2025, 11, 20)

        with (
            mock.patch.object(di, "read_history", return_value={}),
            mock.patch.object(di, "load_holidays", return_value={holiday}),
            mock.patch.object(di, "fetch_session") as fetch,
            mock.patch.object(di, "save_history"),
        ):
            with self.assertRaisesRegex(RuntimeError, "Nenhum pregão"):
                di.update(
                    end=holiday,
                    sessions=1,
                    lookback_days=1,
                    cache_dir=Path("cache"),
                    delay=0,
                )
        fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
