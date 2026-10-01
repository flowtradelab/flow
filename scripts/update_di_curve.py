"""Baixa e mantém o histórico da curva DI1 a partir do BVBG.187.01 da B3.

Uso local recomendado:

    python scripts/update_di_curve.py --sessions 5
    python scripts/update_di_curve.py --sessions 250

O programa salva cada pregão assim que ele é validado. Uma nova execução retoma
o histórico existente e reutiliza o cache bruto em ``.b3-cache/di1``.

Fonte: BVBG.187.01 - DerivativesSimplifiedPriceReport. Os campos usados são
TckrSymb, TradDt/Dt, AdjstdQt, AdjstdQtTax, PrvsAdjstdQt,
PrvsAdjstdQtTax, OpnIntrst, FinInstrmQty e RglrTxsQty.
"""

from __future__ import annotations

import argparse
import http.cookiejar
import io
import json
import math
import os
import re
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "data" / "di_curve" / "history.json"
STATE = ROOT / "data" / "di_curve" / "state.json"
DEFAULT_CACHE = ROOT / ".b3-cache" / "di1"
HOLIDAYS_FILE = ROOT / "feriado-b3" / "feriados_b3.json"

B3_API_BASE = "https://arquivos.b3.com.br"
B3_TOKEN_URL = f"{B3_API_BASE}/api/download/requestname"
B3_DOWNLOAD_URL = f"{B3_API_BASE}/api/download/"
B3_SEARCH_PAGE = (
    "https://www.b3.com.br/pt_br/market-data-e-indices/servicos-de-dados/"
    "market-data/historico/boletins-diarios/pesquisa-por-pregao/"
    "pesquisa-por-pregao/"
)
B3_LEGACY_DOWNLOAD_URL = "https://www.b3.com.br/pesquisapregao/download"
FILE_NAMES = ("DerivativesSimplifiedPriceReport", "BVBG.187.01")
# O Windows não distribui a base IANA usada por ZoneInfo. O Brasil não adota
# horário de verão desde 2019, portanto UTC-3 cobre a janela histórica usada.
TIMEZONE = timezone(timedelta(hours=-3), name="America/Sao_Paulo")
WINDOW = 250
MAX_RETRIES = 3
REQUEST_TIMEOUT = 90
DI_TICKER = re.compile(r"^DI1([FGHJKMNQUVXZ])(\d{2})$")
MONTH_CODES = {
    "F": 1, "G": 2, "H": 3, "J": 4, "K": 5, "M": 6,
    "N": 7, "Q": 8, "U": 9, "V": 10, "X": 11, "Z": 12,
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127 Safari/537.36"
    ),
    "Accept": "application/json, application/zip, application/xml, */*",
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
    "Origin": B3_API_BASE,
    "Referer": f"{B3_API_BASE}/",
}

_COOKIE_JAR = http.cookiejar.CookieJar()
_B3_OPENER = urllib.request.build_opener(
    urllib.request.HTTPCookieProcessor(_COOKIE_JAR)
)
_PORTAL_WARMED = False


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_number(value: str | None) -> float | None:
    if value is None:
        return None
    raw = value.strip()
    if not raw:
        return None
    if "," in raw:
        raw = raw.replace(".", "").replace(",", ".")
    try:
        parsed = float(raw)
    except ValueError:
        return None
    return parsed if math.isfinite(parsed) else None


def parse_integer(value: str | None) -> int | None:
    parsed = parse_number(value)
    return None if parsed is None else int(round(parsed))


def easter_sunday(year: int) -> date:
    """Algoritmo gregoriano de Meeus/Jones/Butcher."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    ell = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * ell) // 451
    month = (h + ell - 7 * m + 114) // 31
    day = (h + ell - 7 * m + 114) % 31 + 1
    return date(year, month, day)


def fallback_b3_holidays(year: int) -> set[date]:
    """Calendário B3 usado quando o JSON versionado não cobre o ano."""
    easter = easter_sunday(year)
    fixed = {
        (1, 1), (4, 21), (5, 1), (9, 7), (10, 12),
        (11, 2), (11, 15), (11, 20), (12, 24), (12, 25), (12, 31),
    }
    holidays = {date(year, month, day) for month, day in fixed}
    holidays.update({
        easter - timedelta(days=48),  # segunda de Carnaval
        easter - timedelta(days=47),  # terça de Carnaval
        easter - timedelta(days=2),   # sexta-feira da Paixão
        easter + timedelta(days=60),  # Corpus Christi
    })
    return holidays


def load_holidays(path: Path = HOLIDAYS_FILE) -> set[date]:
    holidays: set[date] = set()
    if path.exists():
        payload = json.loads(path.read_text(encoding="utf-8"))
        holidays.update(date.fromisoformat(item) for item in payload["feriados"])
    years = set(range(1995, datetime.now(TIMEZONE).year + 20))
    years.difference_update({item.year for item in holidays})
    for year in years:
        holidays.update(fallback_b3_holidays(year))
    return holidays


def is_business_day(day: date, holidays: set[date]) -> bool:
    return day.weekday() < 5 and day not in holidays


def business_days(start: date, end: date, holidays: set[date]) -> int:
    """Conta [start, end), convenção do contrato DI1."""
    if end < start:
        raise ValueError("end must not precede start")
    count = 0
    cursor = start
    while cursor < end:
        count += is_business_day(cursor, holidays)
        cursor += timedelta(days=1)
    return count


def contract_maturity(ticker: str, holidays: set[date]) -> date:
    match = DI_TICKER.fullmatch(ticker)
    if not match:
        raise ValueError(f"Ticker DI1 inválido: {ticker}")
    month_code, short_year = match.groups()
    cursor = date(2000 + int(short_year), MONTH_CODES[month_code], 1)
    while not is_business_day(cursor, holidays):
        cursor += timedelta(days=1)
    return cursor


def discount_factor(rate_pct: float, du: int) -> float:
    if du < 0 or rate_pct <= -100:
        raise ValueError("Taxa ou DU inválido")
    return (1.0 + rate_pct / 100.0) ** (-du / 252.0)


def rate_from_pu(pu: float, du: int) -> float | None:
    if pu <= 0 or du <= 0:
        return None
    return ((100_000.0 / pu) ** (252.0 / du) - 1.0) * 100.0


def element_values(element: ET.Element) -> dict[str, str]:
    values: dict[str, str] = {}
    for child in element.iter():
        text = (child.text or "").strip()
        if text:
            values.setdefault(local_name(child.tag), text)
    return values


def record_element(ticker_element: ET.Element, parents: dict[ET.Element, ET.Element]) -> ET.Element:
    node = parents[ticker_element]
    while node in parents:
        values = element_values(node)
        tickers = [
            child for child in node.iter()
            if local_name(child.tag) == "TckrSymb" and (child.text or "").strip()
        ]
        if len(tickers) == 1 and (
            "AdjstdQtTax" in values or "AdjstdQt" in values
        ):
            return node
        node = parents[node]
    return node


def parse_xml_document(raw: bytes, holidays: set[date]) -> list[dict]:
    root = ET.fromstring(raw)
    parents = {child: parent for parent in root.iter() for child in parent}
    records: dict[tuple[str, str], dict] = {}

    for ticker_element in root.iter():
        if local_name(ticker_element.tag) != "TckrSymb":
            continue
        ticker = (ticker_element.text or "").strip().upper()
        if not DI_TICKER.fullmatch(ticker):
            continue

        values = element_values(record_element(ticker_element, parents))
        trade_date_raw = values.get("Dt")
        if not trade_date_raw:
            continue
        trade_date = date.fromisoformat(trade_date_raw[:10])
        maturity = contract_maturity(ticker, holidays)
        du = business_days(trade_date, maturity, holidays)
        if du <= 0:
            continue

        pu = parse_number(values.get("AdjstdQt"))
        rate = parse_number(values.get("AdjstdQtTax"))
        rate_source = "AdjstdQtTax"
        if rate is None and pu is not None:
            rate = rate_from_pu(pu, du)
            rate_source = "derivada_do_pu"
        if rate is None:
            continue
        if pu is None:
            pu = 100_000.0 * discount_factor(rate, du)
            pu_source = "derivado_da_taxa"
        else:
            pu_source = "AdjstdQt"

        record = {
            "ticker": ticker,
            "vencimento": maturity.isoformat(),
            "du": du,
            "taxa_ajuste": round(rate, 6),
            "pu_ajuste": round(pu, 6),
            "fator_desconto": round(discount_factor(rate, du), 12),
            "taxa_fonte": rate_source,
            "pu_fonte": pu_source,
            "taxa_ajuste_anterior": parse_number(values.get("PrvsAdjstdQtTax")),
            "pu_ajuste_anterior": parse_number(values.get("PrvsAdjstdQt")),
            "open_interest": parse_integer(values.get("OpnIntrst")),
            "contratos_negociados": parse_integer(values.get("FinInstrmQty")),
            "negocios": parse_integer(values.get("RglrTxsQty") or values.get("TradQty")),
        }
        key = (trade_date.isoformat(), ticker)
        previous = records.get(key)
        if previous is None or (record["open_interest"] or -1) > (previous["open_interest"] or -1):
            records[key] = record

    return [
        {"data": day, "vertices": sorted(
            (record for (record_day, _), record in records.items() if record_day == day),
            key=lambda item: (item["du"], item["ticker"]),
        )}
        for day in sorted({key[0] for key in records})
    ]


def documents_from_payload(raw: bytes) -> Iterable[bytes]:
    if zipfile.is_zipfile(io.BytesIO(raw)):
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            for name in archive.namelist():
                if name.lower().endswith((".xml", ".zip")):
                    yield from documents_from_payload(archive.read(name))
        return
    head = raw.lstrip()[:100].lower()
    if head.startswith(b"<?xml") or head.startswith(b"<"):
        yield raw


def parse_payload(raw: bytes, holidays: set[date]) -> list[dict]:
    sessions: dict[str, dict] = {}
    for document in documents_from_payload(raw):
        for session in parse_xml_document(document, holidays):
            target = sessions.setdefault(session["data"], {"data": session["data"], "vertices": []})
            by_ticker = {item["ticker"]: item for item in target["vertices"]}
            by_ticker.update({item["ticker"]: item for item in session["vertices"]})
            target["vertices"] = sorted(by_ticker.values(), key=lambda item: (item["du"], item["ticker"]))
    return [sessions[key] for key in sorted(sessions)]


def validate_session(session: dict, expected_date: date | None = None) -> None:
    actual = date.fromisoformat(session["data"])
    if expected_date is not None and actual != expected_date:
        raise ValueError(f"Data B3 {actual} difere da solicitada {expected_date}")
    vertices = session.get("vertices", [])
    if len(vertices) < 3:
        raise ValueError(f"Apenas {len(vertices)} vértices DI1 encontrados")
    tickers = [item["ticker"] for item in vertices]
    if len(tickers) != len(set(tickers)):
        raise ValueError("Tickers DI1 duplicados")
    for item in vertices:
        if not 0 < item["du"] < 10_000:
            raise ValueError(f"DU inválido em {item['ticker']}")
        if not -20 < item["taxa_ajuste"] < 100:
            raise ValueError(f"Taxa inválida em {item['ticker']}")
        if not 0 < item["pu_ajuste"] < 200_000:
            raise ValueError(f"PU inválido em {item['ticker']}")


def atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(
        payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    ).encode("utf-8") + b"\n"
    temp: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            temp = Path(stream.name)
            stream.write(encoded)
        os.replace(temp, path)
    finally:
        if temp is not None and temp.exists():
            temp.unlink()


def read_history(path: Path = OUTPUT) -> dict[str, dict]:
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1:
        raise ValueError("Schema inesperado no histórico DI1")
    return {item["data"]: item for item in payload.get("historico", [])}


def save_history(history: dict[str, dict], sessions: int, path: Path = OUTPUT) -> None:
    selected = [history[key] for key in sorted(history)[-sessions:]]
    atomic_json(path, {
        "schema_version": 1,
        "produto": "DI1",
        "fonte": "B3 BVBG.187.01 DerivativesSimplifiedPriceReport",
        "convencao_du": "data_negociacao_inclusiva_vencimento_exclusivo_base_252",
        "janela_pregoes": sessions,
        "total_pregoes": len(selected),
        "atualizado_em": datetime.now(TIMEZONE).isoformat(timespec="seconds"),
        "historico": selected,
    })


def http_get(url: str, params: dict[str, str], timeout: int) -> bytes:
    query = urllib.parse.urlencode(params)
    request = urllib.request.Request(f"{url}?{query}", headers=HEADERS)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def legacy_archive_name(day: date) -> str:
    """Nome público do BVBG.187 na página Pesquisa por pregão."""
    return f"SPRD{day:%y%m%d}.zip"


def download_from_trading_session_page(day: date) -> bytes | None:
    global _PORTAL_WARMED
    try:
        if not _PORTAL_WARMED:
            warmup = urllib.request.Request(B3_SEARCH_PAGE, headers=HEADERS)
            with _B3_OPENER.open(warmup, timeout=30) as response:
                response.read(1)
            _PORTAL_WARMED = True

        archive_name = legacy_archive_name(day)
        url = (
            f"{B3_LEGACY_DOWNLOAD_URL}?"
            + urllib.parse.urlencode({"filelist": archive_name})
        )
        headers = dict(HEADERS)
        headers["Referer"] = B3_SEARCH_PAGE
        request = urllib.request.Request(url, headers=headers)
        with _B3_OPENER.open(request, timeout=REQUEST_TIMEOUT) as response:
            raw = response.read()
        if len(raw) <= 64 or raw.lstrip().lower().startswith(b"<html"):
            return None
        if not zipfile.is_zipfile(io.BytesIO(raw)):
            raise ValueError(f"{archive_name} não é um ZIP válido")
        return raw
    except urllib.error.HTTPError as exc:
        if exc.code in (400, 404):
            return None
        print(f"  portal B3: HTTP {exc.code}; tentando API")
    except (OSError, ValueError) as exc:
        print(f"  portal B3: {exc}; tentando API")
    return None


def get_token(day: date) -> tuple[str, str] | None:
    for file_name in FILE_NAMES:
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                raw = http_get(
                    B3_TOKEN_URL,
                    {"fileName": file_name, "date": day.isoformat()},
                    30,
                )
                token = json.loads(raw.decode("utf-8")).get("token")
                if token:
                    return file_name, token
            except urllib.error.HTTPError as exc:
                if exc.code == 400:
                    break
                if attempt == MAX_RETRIES:
                    print(f"  token {file_name}: HTTP {exc.code}")
                else:
                    time.sleep(attempt)
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                if attempt == MAX_RETRIES:
                    print(f"  token {file_name}: {exc}")
                else:
                    time.sleep(attempt)
    return None


def download(day: date) -> tuple[str, bytes] | None:
    direct = download_from_trading_session_page(day)
    if direct is not None:
        return "SPRD/BVBG.187.01", direct

    token_result = get_token(day)
    if token_result is None:
        return None
    file_name, token = token_result
    error: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            raw = http_get(
                B3_DOWNLOAD_URL,
                {"token": token},
                REQUEST_TIMEOUT,
            )
            if not raw or raw.lstrip().lower().startswith(b"<html"):
                raise ValueError("B3 retornou conteúdo vazio/HTML")
            return file_name, raw
        except (OSError, urllib.error.HTTPError, ValueError) as exc:
            error = exc
            if attempt < MAX_RETRIES:
                time.sleep(2 * attempt)
    raise RuntimeError(f"Falha ao baixar {day}: {error}")


def fetch_session(day: date, cache_dir: Path, holidays: set[date], refresh: bool) -> tuple[dict, str] | None:
    cache_file = cache_dir / f"{day.isoformat()}.bin"
    source_name = FILE_NAMES[0]
    if cache_file.exists() and not refresh:
        raw = cache_file.read_bytes()
    else:
        downloaded = download(day)
        if downloaded is None:
            return None
        source_name, raw = downloaded
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_bytes(raw)
    sessions = parse_payload(raw, holidays)
    matches = [item for item in sessions if item["data"] == day.isoformat()]
    if len(matches) != 1:
        raise ValueError(f"Esperava uma sessão {day}; encontrei {len(matches)}")
    validate_session(matches[0], day)
    return matches[0], source_name


def update(
    end: date,
    sessions: int,
    lookback_days: int,
    cache_dir: Path,
    refresh: bool = False,
    delay: float = 1.0,
) -> dict[str, dict]:
    holidays = load_holidays()
    history = read_history()
    latest_existing_day = max((date.fromisoformat(key) for key in history), default=None)
    sources: dict[str, str] = {}
    attempted = 0

    for offset in range(lookback_days):
        day = end - timedelta(days=offset)
        if not is_business_day(day, holidays):
            continue
        if (
            not refresh
            and len(history) >= sessions
            and (latest_existing_day is None or day <= latest_existing_day)
        ):
            break
        if day.isoformat() in history and not refresh:
            continue
        attempted += 1
        print(f"[{len(history):>3}/{sessions}] {day}...", flush=True)
        result = fetch_session(day, cache_dir, holidays, refresh)
        if result is None:
            print("  não publicado")
            continue
        session, source_name = result
        history[day.isoformat()] = session
        history = {
            key: history[key]
            for key in sorted(history)[-sessions:]
        }
        sources[day.isoformat()] = source_name
        save_history(history, sessions)
        atomic_json(STATE, {
            "schema_version": 1,
            "ultima_execucao": datetime.now(TIMEZONE).isoformat(timespec="seconds"),
            "fontes": sources,
            "cache_dir": str(cache_dir),
        })
        print(f"  OK: {len(session['vertices'])} vértices")
        if delay:
            time.sleep(delay)

    if not history:
        raise RuntimeError("Nenhum pregão DI1 foi obtido")
    if not OUTPUT.exists():
        save_history(history, sessions)
    print(f"Histórico: {min(history)} a {max(history)} ({len(history)} pregões; {attempted} tentativas)")
    return history


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sessions", type=int, default=WINDOW)
    parser.add_argument("--end-date", type=date.fromisoformat,
                        default=datetime.now(TIMEZONE).date() - timedelta(days=1))
    parser.add_argument("--lookback-days", type=int)
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--delay", type=float, default=1.0)
    parser.add_argument("--parse-file", type=Path,
                        help="Valida um BVBG.187 local sem acessar a internet")
    args = parser.parse_args()
    if not 1 <= args.sessions <= 5_000:
        parser.error("--sessions deve estar entre 1 e 5000")
    if args.lookback_days is None:
        args.lookback_days = max(args.sessions * 2, 30)
    if args.lookback_days < args.sessions:
        parser.error("--lookback-days deve ser >= --sessions")
    if args.delay < 0:
        parser.error("--delay não pode ser negativo")
    return args


def main() -> None:
    args = parse_args()
    if args.parse_file:
        sessions = parse_payload(args.parse_file.read_bytes(), load_holidays())
        for session in sessions:
            validate_session(session)
        print(json.dumps(sessions, ensure_ascii=False, indent=2))
        return
    update(
        end=args.end_date,
        sessions=args.sessions,
        lookback_days=args.lookback_days,
        cache_dir=args.cache_dir,
        refresh=args.refresh,
        delay=args.delay,
    )


if __name__ == "__main__":
    main()
