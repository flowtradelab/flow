"""Reconstrói IV ATM histórica usando preços B3 e a curva DI1 do mesmo dia.

Primeiro teste recomendado:
    python scripts/update_iv_di_history.py --ticker PETR4 --sessions 1 --end-date 2026-09-30

Depois:
    python scripts/update_iv_di_history.py --ticker PETR4 --sessions 250 --end-date 2026-09-30

Vários ativos, reaproveitando o mesmo cache:
    python scripts/update_iv_di_history.py --ticker PETR4 VALE3 ITUB4 --sessions 250 --end-date 2026-09-30
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from xml.etree import ElementTree as ET

from di_curve import DEFAULT_HISTORY, DiCurve
from option_pricing import implied_volatility
from update_di_curve import (
    B3_DOWNLOAD_URL,
    B3_LEGACY_DOWNLOAD_URL,
    B3_SEARCH_PAGE,
    B3_TOKEN_URL,
    HEADERS,
    TIMEZONE,
    _B3_OPENER,
    atomic_json,
    documents_from_payload,
    element_values,
    load_holidays,
    parse_integer,
    parse_number,
)


ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = ROOT / "data" / "iv_di_history"
CACHE_DIR = ROOT / ".b3-cache" / "iv-di"
INSTRUMENTS_FILE = "InstrumentsConsolidated"
TARGET_DU = 21
ATM_BAND = 0.05
MAX_RETRIES = 3
METHODOLOGY = "IV_ATM_fechamento_ponderada_negocios_com_curva_DI1"


def http_get(url: str, params: dict[str, str], timeout: int = 90) -> bytes:
    request = urllib.request.Request(
        f"{url}?{urllib.parse.urlencode(params)}", headers=HEADERS
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def download_token_file(file_name: str, day: date) -> bytes | None:
    try:
        token_raw = http_get(
            B3_TOKEN_URL,
            {"fileName": file_name, "date": day.isoformat()},
            30,
        )
        token = json.loads(token_raw.decode("utf-8")).get("token")
        if not token:
            return None
        return http_get(B3_DOWNLOAD_URL, {"token": token}, 180)
    except urllib.error.HTTPError as exc:
        if exc.code in (400, 404):
            return None
        raise


def download_public_archive(day: date, prefix: str) -> bytes | None:
    warmup = urllib.request.Request(B3_SEARCH_PAGE, headers=HEADERS)
    with _B3_OPENER.open(warmup, timeout=30) as response:
        response.read(1)
    name = f"{prefix}{day:%y%m%d}.zip"
    url = f"{B3_LEGACY_DOWNLOAD_URL}?{urllib.parse.urlencode({'filelist': name})}"
    headers = dict(HEADERS)
    headers["Referer"] = B3_SEARCH_PAGE
    try:
        with _B3_OPENER.open(
            urllib.request.Request(url, headers=headers), timeout=120
        ) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        if exc.code in (400, 404):
            return None
        raise
    if len(raw) <= 64 or raw.lstrip().lower().startswith(b"<html"):
        return None
    if not zipfile.is_zipfile(io.BytesIO(raw)):
        raise ValueError(f"{name} não é um ZIP válido")
    return raw


def retry_transient(loader, retries: int = MAX_RETRIES) -> bytes | None:
    for attempt in range(retries):
        try:
            return loader()
        except urllib.error.HTTPError as exc:
            if exc.code not in (500, 502, 503, 504) or attempt + 1 == retries:
                raise
        except (urllib.error.URLError, TimeoutError):
            if attempt + 1 == retries:
                raise
        delay = 2 ** attempt
        print(f"  B3 temporariamente indisponível; nova tentativa em {delay}s...", flush=True)
        time.sleep(delay)
    return None


def cached_download(path: Path, loader, refresh: bool) -> bytes | None:
    if path.exists() and not refresh:
        return path.read_bytes()
    raw = retry_transient(loader)
    if raw is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
    return raw


def parse_b3_csv(raw: bytes) -> list[dict[str, str]]:
    text = raw.decode("iso-8859-1", errors="replace")
    lines = text.strip().splitlines()
    if lines and "Status do Arquivo" in lines[0]:
        if "Parcial" in lines[0]:
            raise ValueError("Cadastro de instrumentos ainda parcial")
        lines = lines[1:]
    if not lines:
        return []
    return [
        {(key or "").strip(): (value or "").strip() for key, value in row.items()}
        for row in csv.DictReader(io.StringIO("\n".join(lines)), delimiter=";")
        if row
    ]


def normalize_date(value: str) -> str:
    raw = (value or "").strip()
    if len(raw) == 8 and raw.isdigit():
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
    return raw[:10]


def infer_underlying(row: dict[str, str], ticker: str) -> str:
    direct = row.get("UndrlygTckrSymb1", "").strip().upper()
    if direct:
        return direct
    asset = row.get("Asst", "").strip().upper()
    if asset and asset != ticker:
        return asset
    isin = row.get("ISIN", "").strip().upper()
    base = ticker[:4]
    if isin.startswith("BR" + base):
        suffix = isin[6:8] if isin[6:8] == "11" else isin[6:7]
        if suffix.isdigit():
            return base + suffix
    return ""


def parse_instruments_many(
    raw: bytes,
    underlying_filters: set[str],
) -> dict[str, dict[str, dict]]:
    results: dict[str, dict[str, dict]] = {
        ticker: {} for ticker in underlying_filters
    }
    for row in parse_b3_csv(raw):
        ticker = row.get("TckrSymb", "").strip().upper()
        option_type = row.get("OptnTp", "").strip().upper()
        if option_type.startswith("CALL") or option_type == "C":
            option_type = "C"
        elif option_type.startswith("PUT") or option_type == "P":
            option_type = "P"
        else:
            continue
        underlying = infer_underlying(row, ticker)
        if underlying not in underlying_filters:
            continue
        style_raw = row.get("OptnStyle", "").strip().upper()
        style = "Europeu" if style_raw.startswith("E") else "Americano" if style_raw.startswith("A") else style_raw
        strike = parse_number(row.get("ExrcPric"))
        expiry_raw = normalize_date(row.get("XprtnDt", ""))
        if not ticker or not strike or not expiry_raw:
            continue
        results[underlying][ticker] = {
            "ticker": ticker,
            "underlying": underlying,
            "type": option_type,
            "style": style,
            "strike": strike,
            "expiry": date.fromisoformat(expiry_raw),
        }
    return results


def parse_instruments(raw: bytes, underlying_filter: str) -> dict[str, dict]:
    return parse_instruments_many(raw, {underlying_filter})[underlying_filter]


def parse_price_report(raw: bytes) -> dict[str, dict]:
    records: dict[str, dict] = {}
    for document in documents_from_payload(raw):
        root = ET.fromstring(document)
        parents = {child: parent for parent in root.iter() for child in parent}
        for ticker_element in root.iter():
            if ticker_element.tag.rsplit("}", 1)[-1] != "TckrSymb":
                continue
            ticker = (ticker_element.text or "").strip().upper()
            if not ticker:
                continue
            record = parents.get(ticker_element)
            while record is not None and record.tag.rsplit("}", 1)[-1] != "PricRpt":
                record = parents.get(record)
            if record is None:
                continue
            values = element_values(record)
            last = parse_number(values.get("LastPric"))
            average = parse_number(values.get("TradAvrgPric"))
            price = last if last and last > 0 else average
            if price is None or price <= 0:
                continue
            candidate = {
                "price": price,
                "average_price": average,
                "last_price": last,
                "trades": parse_integer(values.get("RglrTxsQty")) or 0,
            }
            previous = records.get(ticker)
            if previous is None or candidate["trades"] > previous["trades"]:
                records[ticker] = candidate
    return records


def constant_maturity_iv(expiry_rows: list[dict], target_du: int = TARGET_DU) -> tuple[float, str, list[dict]]:
    rows = sorted(expiry_rows, key=lambda row: row["du"])
    before = [row for row in rows if row["du"] <= target_du]
    after = [row for row in rows if row["du"] >= target_du]
    if before and after:
        left, right = before[-1], after[0]
        if left["du"] == right["du"]:
            return left["iv"], "vencimento_exato", [left]
        weight = (target_du - left["du"]) / (right["du"] - left["du"])
        left_var = left["iv"] ** 2 * left["du"] / 252.0
        right_var = right["iv"] ** 2 * right["du"] / 252.0
        target_var = left_var + weight * (right_var - left_var)
        return math.sqrt(target_var / (target_du / 252.0)), "variancia_total_21du", [left, right]
    nearest = min(rows, key=lambda row: abs(row["du"] - target_du))
    return nearest["iv"], "vencimento_mais_proximo", [nearest]


def calculate_day(
    day: date,
    ticker: str,
    prices: dict[str, dict],
    instruments: dict[str, dict],
    curve: DiCurve,
    holidays: set[date] | None = None,
) -> dict:
    spot_row = prices.get(ticker)
    if spot_row is None:
        raise ValueError(f"Preço à vista de {ticker} não encontrado")
    spot = spot_row["price"]
    by_type: dict[str, list[dict]] = defaultdict(list)
    expiry_points = {}

    for option_ticker, meta in instruments.items():
        if meta["expiry"] <= day:
            continue
        if abs(meta["strike"] / spot - 1.0) > ATM_BAND:
            continue
        quote = prices.get(option_ticker)
        if quote is None or quote["trades"] <= 0:
            continue
        if meta["expiry"] not in expiry_points:
            expiry_points[meta["expiry"]] = curve.for_expiry(meta["expiry"], holidays)
        point = expiry_points[meta["expiry"]]
        if not 3 <= point.du <= 126:
            continue
        try:
            iv = implied_volatility(
                quote["price"], spot, meta["strike"], point.du,
                point.discount_factor, meta["type"], dividend_yield=0.0,
            )
        except ValueError:
            continue
        if 0.01 <= iv <= 3.0:
            by_type[meta["type"]].append({
                "ticker": option_ticker,
                "iv": iv,
                "type": meta["type"],
                "strike": meta["strike"],
                "premium": quote["price"],
                "trades": quote["trades"],
                "du": point.du,
            })

    if len(by_type["C"]) < 5 or len(by_type["P"]) < 5:
        raise ValueError("São necessárias ao menos cinco Calls e cinco Puts ATM negociadas")

    def weighted_iv(rows: list[dict]) -> float:
        total_weight = sum(row["trades"] for row in rows)
        return sum(row["iv"] * row["trades"] for row in rows) / total_weight

    iv_call = weighted_iv(by_type["C"])
    iv_put = weighted_iv(by_type["P"])
    iv = (iv_call + iv_put) / 2.0
    rate_point = curve.point(TARGET_DU)
    return {
        "date": day.isoformat(),
        "iv": round(iv, 8),
        "spot": spot,
        "target_du": TARGET_DU,
        "taxa_di": round(rate_point.annual_rate, 8),
        "iv_call": round(iv_call, 8),
        "iv_put": round(iv_put, 8),
        "diferencial_call_put": round(iv_call - iv_put, 8),
        "metodo": METHODOLOGY,
        "dividend_yield_assumido": 0.0,
        "calls_validas": len(by_type["C"]),
        "puts_validas": len(by_type["P"]),
        "negocios_calls": sum(row["trades"] for row in by_type["C"]),
        "negocios_puts": sum(row["trades"] for row in by_type["P"]),
        "opcoes_validas": len(by_type["C"]) + len(by_type["P"]),
        "fonte": "B3_SPRE+B3_InstrumentsConsolidated+B3_DI1",
    }


def load_existing(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("metodologia") != METHODOLOGY:
        return {}
    return {row["date"]: row for row in payload.get("serie_iv_diaria", [])}


def curve_session_dates(
    path: Path = DEFAULT_HISTORY,
    end: date | None = None,
    sessions: int = 250,
) -> list[date]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    available = sorted({
        date.fromisoformat(row["data"])
        for row in payload.get("historico", [])
        if end is None or date.fromisoformat(row["data"]) <= end
    })
    if not available:
        raise ValueError("Não há sessões DI1 disponíveis até a data solicitada")
    return available[-sessions:]


def select_target_days(
    path: Path,
    end: date,
    sessions: int,
    latest_only: bool,
) -> list[date]:
    days = curve_session_dates(path, end, sessions)
    return days[-1:] if latest_only else days


def normalize_tickers(values: list[str]) -> list[str]:
    tickers: list[str] = []
    for value in values:
        for ticker in value.split(","):
            ticker = ticker.strip().upper()
            if ticker and ticker not in tickers:
                tickers.append(ticker)
    if not tickers:
        raise ValueError("Informe ao menos um ticker")
    return tickers


def existing_tickers(history_dir: Path = OUTPUT_DIR) -> list[str]:
    tickers = sorted(path.stem.upper() for path in history_dir.glob("*.json"))
    if not tickers:
        raise ValueError("Nenhum histórico de ativo foi encontrado")
    return tickers


def save_history(path: Path, ticker: str, history: dict[str, dict], sessions: int) -> None:
    rows = [history[key] for key in sorted(history)[-sessions:]]
    atomic_json(path, {
        "schema_version": 1,
        "ativo": ticker,
        "ultima_atualizacao": rows[-1]["date"] if rows else None,
        "metodologia": METHODOLOGY,
        "serie_iv_diaria": rows,
    })


def retention_window(requested_sessions: int, initial_count: int) -> int:
    """Nunca reduz um histórico existente durante um teste com janela menor."""
    return max(requested_sessions, initial_count)


def update_many(
    tickers: list[str],
    sessions: int,
    end: date,
    refresh: bool = False,
    latest_only: bool = False,
) -> dict[str, dict[str, dict]]:
    tickers = normalize_tickers(tickers)
    outputs = {ticker: OUTPUT_DIR / f"{ticker}.json" for ticker in tickers}
    histories = {ticker: load_existing(outputs[ticker]) for ticker in tickers}
    retentions = {
        ticker: retention_window(sessions, len(histories[ticker]))
        for ticker in tickers
    }
    target_days = select_target_days(
        DEFAULT_HISTORY, end, sessions, latest_only
    )
    target_keys = {day.isoformat() for day in target_days}
    holidays = load_holidays()

    if not latest_only and len(target_days) < sessions:
        print(
            f"Aviso: a base DI1 possui somente {len(target_days)} pregões até {end}.",
            flush=True,
        )

    for day in reversed(target_days):
        day_key = day.isoformat()
        pending = [
            ticker for ticker in tickers
            if refresh or day_key not in histories[ticker]
        ]
        if not pending:
            continue
        progress = ", ".join(
            f"{ticker}={len(target_keys.intersection(histories[ticker]))}"
            for ticker in pending
        )
        print(
            f"[{day}] pendentes: {progress}/{len(target_days)}",
            flush=True,
        )
        spre_path = CACHE_DIR / "spre" / f"{day}.bin"
        inst_path = CACHE_DIR / "instruments" / f"{day}.csv"
        try:
            spre = cached_download(
                spre_path, lambda: download_public_archive(day, "SPRE"), refresh
            )
            instruments_raw = cached_download(
                inst_path,
                lambda: download_token_file(INSTRUMENTS_FILE, day),
                refresh,
            )
            if spre is None or instruments_raw is None:
                print("  fontes não publicadas")
                continue
            print(f"  processando SPRE ({len(spre) / 1_048_576:.2f} MB)...", flush=True)
            prices = parse_price_report(spre)
            print(f"  {len(prices):,} preços encontrados", flush=True)
            print(
                f"  processando instrumentos ({len(instruments_raw) / 1_048_576:.2f} MB)...",
                flush=True,
            )
            instruments_by_ticker = parse_instruments_many(instruments_raw, set(pending))
            curve = DiCurve.from_history(DEFAULT_HISTORY, day)
            if curve.session_date != day:
                raise ValueError("Curva DI1 exata do pregão não encontrada")
            for ticker in pending:
                instruments = instruments_by_ticker[ticker]
                print(f"  {ticker}: {len(instruments):,} opções", flush=True)
                try:
                    row = calculate_day(
                        day, ticker, prices, instruments, curve, holidays
                    )
                    history = histories[ticker]
                    history[day_key] = row
                    histories[ticker] = {
                        key: history[key]
                        for key in sorted(history)[-retentions[ticker]:]
                    }
                    save_history(
                        outputs[ticker], ticker, histories[ticker], retentions[ticker]
                    )
                    print(
                        f"    OK: Call={row['iv_call'] * 100:.2f}% | "
                        f"Put={row['iv_put'] * 100:.2f}% | "
                        f"{row['opcoes_validas']} opções"
                    )
                except Exception as exc:
                    print(f"    ignorado: {exc}")
        except Exception as exc:
            print(f"  pregão ignorado para {', '.join(pending)}: {exc}")

    for ticker in tickers:
        history = histories[ticker]
        if not history:
            print(f"Histórico IV-DI de {ticker}: nenhum pregão calculado")
            continue
        save_history(outputs[ticker], ticker, history, retentions[ticker])
        print(
            f"Histórico IV-DI de {ticker}: {min(history)} a {max(history)} "
            f"({len(history)} pregões)"
        )
    return histories


def update(
    ticker: str,
    sessions: int,
    end: date,
    refresh: bool = False,
    latest_only: bool = False,
) -> dict[str, dict]:
    history = update_many(
        [ticker], sessions, end, refresh, latest_only
    )[ticker]
    if not history:
        raise RuntimeError("Nenhuma IV diária foi calculada")
    return history


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--ticker",
        nargs="+",
        help="Um ou mais ativos, separados por espaço ou vírgula",
    )
    source.add_argument(
        "--all-existing",
        action="store_true",
        help="Atualiza todos os ativos que já possuem JSON histórico",
    )
    parser.add_argument("--sessions", type=int, default=1)
    parser.add_argument("--end-date", type=date.fromisoformat,
                        default=datetime.now(TIMEZONE).date() - timedelta(days=1))
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument(
        "--latest-only",
        action="store_true",
        help="Processa somente a sessão DI1 mais recente até --end-date",
    )
    args = parser.parse_args()
    if not 1 <= args.sessions <= 250:
        parser.error("--sessions deve estar entre 1 e 250")
    try:
        tickers = (
            existing_tickers() if args.all_existing
            else normalize_tickers(args.ticker)
        )
    except ValueError as exc:
        parser.error(str(exc))
    update_many(
        tickers,
        args.sessions,
        args.end_date,
        args.refresh,
        args.latest_only,
    )


if __name__ == "__main__":
    main()
