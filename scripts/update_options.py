"""
scripts/update_options.py
==========================
1. Baixa o JSON diario de posicoes em aberto de opcoes de empresas da B3
2. Baixa InstrumentsConsolidated.csv para metadados e ativo-objeto
3. Faz JOIN pelo ticker individual da opcao
4. Gera grid-options/{TICKER}/latest.json
5. Commit via GitHub Actions

Fonte principal de posicoes:
  https://www.b3.com.br/json/YYYYMMDD/Posicoes/Empresa/SI_C_OPCPOSABEMP.json

Campos de posicao do JSON:
- ser      -> ticker da opcao
- prEx     -> strike
- poCob    -> quantidade coberta
- posTr    -> quantidade travada
- posDe    -> quantidade descoberta
- posTo    -> total / open interest
- qtdClTit -> clientes titulares
- qtdClLan -> clientes lancadores
- dtVen    -> vencimento
- tMerc    -> 70 CALL / 80 PUT

InstrumentsConsolidated continua sendo usado para estilo e ativo-objeto exato.
"""

import csv
import io
import json
import os
import re
import time
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests


# ── Constantes ────────────────────────────────────────────────────────────────
B3_API_BASE       = "https://arquivos.b3.com.br"
B3_SITE_BASE      = "https://www.b3.com.br"
B3_TOKEN_URL      = f"{B3_API_BASE}/api/download/requestname"
B3_DOWNLOAD_URL   = f"{B3_API_BASE}/api/download/"
INSTRUMENTS_FILE  = "InstrumentsConsolidated"
POSITIONS_JSON    = "SI_C_OPCPOSABEMP.json"

OUTPUT_FOLDER     = Path("grid-options")
TEMP_DIR          = Path("/tmp")
BRT               = timezone(timedelta(hours=-3))
MAX_RETRIES       = 3
REQUEST_TIMEOUT   = 300
CSV_ENCODING      = "iso-8859-1"

# Travas contra publicar dados incompletos/zerados.
MIN_POSITION_ROWS = 1000
MIN_MATCHED       = 1000
MIN_MATCH_RATIO   = 0.80
MIN_SANITY_RATIO  = 0.95

COMMON_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/127.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
}

FILE_HEADERS = {
    **COMMON_HEADERS,
    "Accept": "application/json, text/html, */*",
    "Origin": B3_API_BASE,
    "Referer": f"{B3_API_BASE}/",
}

JSON_HEADERS = {
    **COMMON_HEADERS,
    "Accept": "application/json,text/plain,*/*",
    "Referer": (
        "https://www.b3.com.br/pt_br/market-data-e-indices/"
        "servicos-de-dados/market-data/consultas/mercado-a-vista/"
        "opcoes/posicoes-em-aberto/"
    ),
}

# Fallback para ativos cujo ticker objeto nao possa ser inferido.
ATIVO_OBJETO_MAP = {
    "BOVA": "BOVA11",
    "VALE": "VALE3",
    "BBDC": "BBDC4",
}


def github_output(key, value):
    gh = os.environ.get("GITHUB_OUTPUT", "")
    if gh:
        with open(gh, "a", encoding="utf-8") as f:
            f.write(f"{key}={value}\n")
    print(f"[output] {key}={value}")


def dias_uteis_recentes(n=7):
    """Retorna os ultimos N dias uteis (seg-sex), mais recente primeiro."""
    hoje = datetime.now(BRT).date()
    dias = []
    delta = 0
    while len(dias) < n:
        d = hoje - timedelta(days=delta)
        if d.weekday() < 5:
            dias.append(d)
        delta += 1
    return dias


# ── Download InstrumentsConsolidated ─────────────────────────────────────────
def get_download_token(api_name: str, date_str: str) -> str | None:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = requests.get(
                B3_TOKEN_URL,
                params={"fileName": api_name, "date": date_str},
                headers=FILE_HEADERS,
                timeout=30,
            )
            if r.status_code == 200:
                payload = r.json()
                return payload.get("token") or None
            if r.status_code == 400:
                return None
            print(f"    {api_name} {date_str}: HTTP {r.status_code}")
        except Exception as e:
            print(f"    {api_name} {date_str}: erro token ({e})")
        if attempt < MAX_RETRIES:
            time.sleep(attempt)
    return None


def download_csv_from_token(token: str, api_name: str) -> bytes:
    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = requests.get(
                B3_DOWNLOAD_URL,
                params={"token": token},
                headers=FILE_HEADERS,
                timeout=REQUEST_TIMEOUT,
                allow_redirects=True,
            )
            if r.status_code != 200:
                raise RuntimeError(f"HTTP {r.status_code}")
            raw = r.content
            if not raw:
                raise RuntimeError("resposta vazia")
            head = raw[:200].lstrip().lower()
            if head.startswith(b"<!doctype html") or head.startswith(b"<html"):
                raise RuntimeError("B3 retornou HTML em vez de CSV")
            return raw
        except Exception as e:
            last_error = e
            print(f"    {api_name}: erro download tentativa {attempt} ({e})")
            if attempt < MAX_RETRIES:
                time.sleep(2 * attempt)
    raise RuntimeError(f"Falha ao baixar {api_name}: {last_error}")


def parse_b3_csv(raw: bytes) -> tuple[list[dict], str]:
    text = raw.decode(CSV_ENCODING, errors="replace")
    lines = text.strip().splitlines()
    if not lines:
        return [], ""

    status = ""
    header_idx = 0
    if "Status do Arquivo" in lines[0]:
        status = lines[0].split(":")[-1].strip() if ":" in lines[0] else ""
        header_idx = 1

    if header_idx >= len(lines):
        return [], status

    reader = csv.DictReader(io.StringIO("\n".join(lines[header_idx:])), delimiter=";")
    rows = []
    for row in reader:
        if not row:
            continue
        clean = {
            (k or "").strip(): (v or "").strip()
            for k, v in row.items()
            if k is not None
        }
        if any(clean.values()):
            rows.append(clean)
    return rows, status


# ── Download JSON de posicoes ─────────────────────────────────────────────────
def positions_json_url(data_yyyymmdd: str) -> str:
    return (
        f"{B3_SITE_BASE}/json/{data_yyyymmdd}/Posicoes/Empresa/"
        f"{POSITIONS_JSON}"
    )


def flatten_positions_payload(payload: dict) -> list[dict]:
    """
    A B3 entrega:
      {"Empresa": {"A": [...], "B": [...], ...}}
    A pagina filtra a letra/empresa no browser. Aqui lemos todas as letras.
    """
    empresa = payload.get("Empresa")
    if not isinstance(empresa, dict):
        raise RuntimeError("JSON de posicoes sem objeto 'Empresa'")

    rows = []
    for letter, items in empresa.items():
        if not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, dict):
                rows.append(item)

    return rows


def download_positions_json(data_yyyymmdd: str) -> tuple[list[dict], bytes] | None:
    url = positions_json_url(data_yyyymmdd)
    last_error = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = requests.get(
                url,
                headers=JSON_HEADERS,
                timeout=REQUEST_TIMEOUT,
                allow_redirects=True,
            )

            if r.status_code in (403, 404):
                return None
            if r.status_code != 200:
                raise RuntimeError(f"HTTP {r.status_code}")

            raw = r.content
            if not raw:
                raise RuntimeError("resposta vazia")

            head = raw[:200].lstrip().lower()
            if head.startswith(b"<!doctype html") or head.startswith(b"<html"):
                raise RuntimeError("B3 retornou HTML em vez de JSON")

            payload = r.json()
            rows = flatten_positions_payload(payload)
            if not rows:
                raise RuntimeError("JSON sem linhas de posicoes")

            return rows, raw
        except Exception as e:
            last_error = e
            print(
                f"    {POSITIONS_JSON} {data_yyyymmdd}: "
                f"erro tentativa {attempt} ({e})"
            )
            if attempt < MAX_RETRIES:
                time.sleep(2 * attempt)

    print(f"    {POSITIONS_JSON}: indisponivel ({last_error})")
    return None


def download_latest_sources() -> tuple[str, list[dict], list[dict], bytes, bytes]:
    """
    Procura a data mais recente em que:
      1) o JSON detalhado de posicoes por serie exista;
      2) InstrumentsConsolidated da mesma data esteja publicado.
    """
    for d in dias_uteis_recentes(7):
        date_str = d.strftime("%Y-%m-%d")
        data_yyyymmdd = d.strftime("%Y%m%d")
        print(f"  Tentando {date_str}...")

        inst_token = get_download_token(INSTRUMENTS_FILE, date_str)
        if not inst_token:
            print("    InstrumentsConsolidated indisponivel")
            continue

        positions_result = download_positions_json(data_yyyymmdd)
        if not positions_result:
            print(f"    {POSITIONS_JSON} indisponivel")
            continue

        pos_rows, pos_raw = positions_result

        print(f"    Baixando {INSTRUMENTS_FILE}...")
        inst_raw = download_csv_from_token(inst_token, INSTRUMENTS_FILE)
        print(f"      Instruments: {len(inst_raw)/1024/1024:.1f} MB")
        print(f"      Posicoes JSON: {len(pos_raw)/1024/1024:.1f} MB")

        instruments, inst_status = parse_b3_csv(inst_raw)
        if inst_status and inst_status.lower() != "final":
            print(
                f"    InstrumentsConsolidated status={inst_status}; "
                "ignorando snapshot parcial"
            )
            continue

        if not instruments or not pos_rows:
            print("    Fonte vazia; tentando data anterior")
            continue

        print(
            f"    OK: {len(instruments):,} instrumentos | "
            f"{len(pos_rows):,} posicoes detalhadas"
        )
        return date_str, instruments, pos_rows, inst_raw, pos_raw

    raise RuntimeError(
        "Nao foi encontrada uma data recente com "
        "InstrumentsConsolidated e SI_C_OPCPOSABEMP.json publicados."
    )


# ── Conversores ───────────────────────────────────────────────────────────────
def parse_int(value) -> int:
    if isinstance(value, (int, float)):
        return int(round(value))

    s = str(value or "").strip()
    if not s or s in ("-", "N/A", "NA"):
        return 0

    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    try:
        return int(round(float(s)))
    except Exception:
        return 0


def parse_float(value) -> float:
    if isinstance(value, (int, float)):
        return round(float(value), 6)

    s = str(value or "").strip()
    if not s or s in ("-", "N/A", "NA"):
        return 0.0
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    try:
        return round(float(s), 6)
    except Exception:
        return 0.0


def normalize_option_type(value: str, segment: str = "") -> str:
    raw = (value or "").strip().lower()
    seg = (segment or "").strip().lower()
    if raw.startswith("c") or "call" in raw or "call" in seg:
        return "C"
    if raw.startswith("p") or "put" in raw or "put" in seg:
        return "P"
    return ""


def normalize_style(value: str) -> str:
    raw = (value or "").strip()
    upper = raw.upper()
    if upper.startswith("AMER"):
        return "Americano"
    if upper.startswith("EURO"):
        return "Europeu"
    return raw


def normalize_date(value) -> str:
    raw = str(value or "").strip()
    if len(raw) == 8 and raw.isdigit():
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
    return raw


def infer_underlying_from_isin(isin: str, base_key: str) -> str:
    """
    Para opcoes sobre acoes, o ISIN normalmente preserva a classe do papel:
      BRPETR3... -> PETR3
      BRPETR4... -> PETR4
      BRITUB4... -> ITUB4
    Tambem trata units terminadas em 11.
    """
    raw = (isin or "").strip().upper()
    base = (base_key or "").strip().upper()
    if raw and base:
        match = re.match(rf"^BR{re.escape(base)}(11|[3-8])", raw)
        if match:
            return f"{base}{match.group(1)}"
    return ""


def infer_underlying_from_spec(base_key: str, spec: str) -> str:
    """
    Fallback quando o cadastro mestre nao informa o ticker objeto.
    Usa a especificacao do papel do JSON de posicoes.
    """
    base = (base_key or "").strip().upper()
    spec_upper = (spec or "").strip().upper()

    if "PNA" in spec_upper:
        return f"{base}5"
    if "PNB" in spec_upper:
        return f"{base}6"
    if "PN" in spec_upper:
        return f"{base}4"
    if "ON" in spec_upper:
        return f"{base}3"
    if "UNT" in spec_upper or "UNIT" in spec_upper:
        return f"{base}11"

    return ATIVO_OBJETO_MAP.get(base, base)


# ── Parsers ───────────────────────────────────────────────────────────────────
def parse_instruments(rows: list[dict]) -> dict:
    result = {}

    for row in rows:
        ticker = row.get("TckrSymb", "").strip().upper()
        if not ticker or len(ticker) < 4:
            continue

        tipo = normalize_option_type(
            row.get("OptnTp", ""),
            row.get("SgmtNm", ""),
        )
        if not tipo:
            continue

        venc = normalize_date(row.get("XprtnDt", ""))
        if not venc:
            continue

        base_key = ticker[:4]
        underlying = row.get("UndrlygTckrSymb1", "").strip().upper()
        if not underlying:
            underlying = infer_underlying_from_isin(
                row.get("ISIN", ""),
                base_key,
            )

        result[ticker] = {
            "ativo_objeto": underlying,
            "tipo": tipo,
            "estilo": normalize_style(row.get("OptnStyle", "")),
            "strike": parse_float(row.get("ExrcPric", "")),
            "vencimento": venc,
            "premio": 0.0,
            "isin": row.get("ISIN", "").strip().upper(),
        }

    print(f"    Instruments: {len(result):,} opcoes")
    return result


def parse_positions_json(rows: list[dict]) -> tuple[dict, dict]:
    result = {}
    duplicates = 0
    balanced = 0
    invalid_type = 0

    for row in rows:
        ticker = str(row.get("ser") or "").strip().upper()
        if not ticker:
            continue

        market_type = str(row.get("tMerc") or "").strip()
        if market_type == "70":
            tipo = "C"
        elif market_type == "80":
            tipo = "P"
        else:
            invalid_type += 1
            continue

        coberta = parse_int(row.get("poCob"))
        travada = parse_int(row.get("posTr"))
        descoberta = parse_int(row.get("posDe"))
        total = parse_int(row.get("posTo"))

        if abs((coberta + travada + descoberta) - total) <= 1:
            balanced += 1

        if ticker in result:
            duplicates += 1

        titulares = parse_int(row.get("qtdClTit"))
        lancadores = parse_int(row.get("qtdClLan"))

        result[ticker] = {
            "tipo": tipo,
            "strike": parse_float(row.get("prEx")),
            "vencimento": normalize_date(row.get("dtVen")),
            "open_interest": total,
            "variacao_open_interest": 0,
            "qtd_coberta": coberta,
            "qtd_travada": travada,
            # Mantido por compatibilidade com o schema anterior.
            "total_posicoes_bloqueadas": travada,
            "qtd_descoberta": descoberta,
            "total_posicoes": total,
            "qtd_titulares": titulares,
            "qtd_lancadores": lancadores,
            # Aliases mantidos para nao quebrar consumidores existentes.
            "qtd_tomadores": titulares,
            "qtd_doadores": lancadores,
            "empresa": str(row.get("nmEmp") or "").strip(),
            "mercado_raiz": str(row.get("mer") or "").strip().upper(),
            "especificacao_papel": str(row.get("espPap") or "").strip(),
        }

    stats = {
        "raw_rows": len(rows),
        "parsed": len(result),
        "duplicates": duplicates,
        "invalid_type": invalid_type,
        "balanced": balanced,
        "sanity_ratio": (
            balanced / len(result)
            if result
            else 0.0
        ),
    }

    print(f"    Posicoes JSON: {len(result):,} opcoes individuais")
    print(
        f"    Sanidade coberta+trava+descoberta=total: "
        f"{stats['sanity_ratio']:.1%}"
    )
    if duplicates:
        print(f"    Tickers duplicados no JSON: {duplicates:,}")
    if invalid_type:
        print(f"    Linhas com tMerc desconhecido: {invalid_type:,}")

    return result, stats


# ── JOIN ──────────────────────────────────────────────────────────────────────
def build_options(instruments: dict, positions: dict) -> tuple[dict, dict]:
    by_ticker = defaultdict(list)
    matched = 0
    metadata_mismatch = 0

    for ticker_opcao, inst in instruments.items():
        base_key = ticker_opcao[:4].upper()
        pos = positions.get(ticker_opcao)

        if pos is not None:
            matched += 1

            if (
                inst["tipo"] != pos["tipo"]
                or (
                    inst["vencimento"]
                    and pos["vencimento"]
                    and inst["vencimento"] != pos["vencimento"]
                )
                or (
                    inst["strike"]
                    and pos["strike"]
                    and abs(inst["strike"] - pos["strike"]) > 0.011
                )
            ):
                metadata_mismatch += 1
        else:
            pos = {}

        underlying = inst["ativo_objeto"]
        if not underlying:
            underlying = infer_underlying_from_spec(
                base_key,
                pos.get("especificacao_papel", ""),
            )

        # Para series com posicao, o proprio JSON da pagina e a fonte primaria
        # de strike/tipo/vencimento; Instruments completa estilo e ativo objeto.
        tipo = pos.get("tipo") or inst["tipo"]
        strike = (
            pos["strike"]
            if pos.get("strike") is not None and pos.get("strike") != 0
            else inst["strike"]
        )
        vencimento = pos.get("vencimento") or inst["vencimento"]

        by_ticker[base_key].append({
            "ticker": ticker_opcao,
            "ativo_objeto": underlying,
            "tipo": tipo,
            "estilo": inst["estilo"],
            "strike": strike,
            "vencimento": vencimento,
            "premio": inst["premio"],
            "open_interest": pos.get("open_interest", 0),
            "variacao_open_interest": pos.get("variacao_open_interest", 0),
            "qtd_coberta": pos.get("qtd_coberta", 0),
            "qtd_travada": pos.get("qtd_travada", 0),
            "total_posicoes_bloqueadas": pos.get(
                "total_posicoes_bloqueadas",
                0,
            ),
            "qtd_descoberta": pos.get("qtd_descoberta", 0),
            "total_posicoes": pos.get("total_posicoes", 0),
            "qtd_titulares": pos.get("qtd_titulares", 0),
            "qtd_lancadores": pos.get("qtd_lancadores", 0),
            "qtd_tomadores": pos.get("qtd_tomadores", 0),
            "qtd_doadores": pos.get("qtd_doadores", 0),
            "com_oi": ticker_opcao in positions,
        })

    print(
        f"    JOIN: {matched:,} com posicao | "
        f"{len(instruments)-matched:,} sem posicao"
    )
    if metadata_mismatch:
        print(
            f"    Divergencias de metadados JSON x Instruments: "
            f"{metadata_mismatch:,}"
        )

    return dict(by_ticker), {
        "matched": matched,
        "metadata_mismatch": metadata_mismatch,
    }


def validate_sources(
    instruments_count: int,
    positions_count: int,
    matched: int,
    sanity_ratio: float,
):
    authorized_ratio = matched / instruments_count if instruments_count else 0.0
    positions_match_ratio = matched / positions_count if positions_count else 0.0

    print(f"    Cobertura sobre series autorizadas: {authorized_ratio:.2%}")
    print(
        f"    Cobertura das posicoes JSON no cadastro: "
        f"{matched:,}/{positions_count:,} ({positions_match_ratio:.1%})"
    )

    errors = []

    if positions_count < MIN_POSITION_ROWS:
        errors.append(
            f"JSON trouxe apenas {positions_count:,} opcoes "
            f"(minimo esperado {MIN_POSITION_ROWS:,})"
        )

    if matched < MIN_MATCHED:
        errors.append(
            f"apenas {matched:,} tickers fizeram JOIN "
            f"(minimo esperado {MIN_MATCHED:,})"
        )

    if positions_match_ratio < MIN_MATCH_RATIO:
        errors.append(
            f"cobertura do JOIN foi {positions_match_ratio:.1%} "
            f"(minimo {MIN_MATCH_RATIO:.0%})"
        )

    if sanity_ratio < MIN_SANITY_RATIO:
        errors.append(
            f"sanidade das posicoes foi {sanity_ratio:.1%} "
            f"(minimo {MIN_SANITY_RATIO:.0%})"
        )

    if errors:
        raise RuntimeError(
            "Fonte de posicoes da B3 considerada anormal: "
            + "; ".join(errors)
            + ". JSONs NAO serao sobrescritos."
        )


# ── Salva JSONs ───────────────────────────────────────────────────────────────
def save_jsons(data_date: str, by_ticker: dict) -> int:
    OUTPUT_FOLDER.mkdir(parents=True, exist_ok=True)
    changed = 0

    for ticker, opcoes in sorted(by_ticker.items()):
        opcoes_s = sorted(
            opcoes,
            key=lambda x: (x["vencimento"], x["tipo"], x["strike"])
        )
        content = json.dumps(
            {
                "ticker": ticker,
                "data": data_date,
                "total": len(opcoes_s),
                "opcoes": opcoes_s,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")

        out_dir = OUTPUT_FOLDER / ticker
        out_dir.mkdir(parents=True, exist_ok=True)
        out_file = out_dir / "latest.json"

        if out_file.exists() and out_file.read_bytes() == content:
            continue

        out_file.write_bytes(content)
        changed += 1

    return changed


# ── Relatorio ─────────────────────────────────────────────────────────────────
def save_report(
    data_date: str,
    instruments_rows: int,
    option_instruments: int,
    positions_rows: int,
    option_positions: int,
    matched: int,
    sanity_ratio: float,
    metadata_mismatch: int,
    tickers_count: int,
    changed: int,
    instruments_raw: bytes,
    positions_raw: bytes,
):
    now = datetime.now(BRT)

    report = {
        "status": "ok",
        "executado_em": now.strftime("%Y-%m-%d %H:%M:%S BRT"),
        "executado_em_ts": int(now.timestamp()),
        "data_referencia": data_date,
        "arquivos": {
            "instruments_consolidated": {
                "nome": INSTRUMENTS_FILE,
                "tamanho_mb": round(len(instruments_raw) / 1024 / 1024, 2),
                "linhas_total": instruments_rows,
                "opcoes_total": option_instruments,
            },
            "options_open_positions_json": {
                "nome": POSITIONS_JSON,
                "url": positions_json_url(data_date.replace("-", "")),
                "tamanho_mb": round(len(positions_raw) / 1024 / 1024, 2),
                "linhas_total": positions_rows,
                "opcoes_com_posicao": option_positions,
            },
        },
        "processamento": {
            "opcoes_com_join": matched,
            "opcoes_sem_posicao": option_instruments - matched,
            "cobertura_join_pct": round(
                (matched / option_positions * 100)
                if option_positions
                else 0,
                2,
            ),
            "sanidade_posicoes_pct": round(sanity_ratio * 100, 2),
            "divergencias_metadados": metadata_mismatch,
            "tickers_gerados": tickers_count,
            "arquivos_alterados": changed,
        },
        "erros": [],
    }

    logs_dir = Path("logs")
    logs_dir.mkdir(exist_ok=True)

    (logs_dir / "last_run.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    hist = logs_dir / "history.jsonl"
    lines = (
        hist.read_text(encoding="utf-8").strip().split("\n")
        if hist.exists()
        else []
    )
    lines = [line for line in lines if line.strip()]
    lines.append(json.dumps({
        "ts": report["executado_em"],
        "data": data_date,
        "status": report["status"],
        "tickers": tickers_count,
        "changed": changed,
        "matched": matched,
        "fonte": "SI_C_OPCPOSABEMP+InstrumentsConsolidated",
    }, ensure_ascii=False))

    hist.write_text("\n".join(lines[-30:]) + "\n", encoding="utf-8")

    print("\n  Relatorio: logs/last_run.json")
    print(
        f"  Instruments: {instruments_rows:,} linhas | "
        f"{option_instruments:,} opcoes"
    )
    print(
        f"  Posicoes JSON: {positions_rows:,} linhas | "
        f"{option_positions:,} opcoes individuais"
    )
    print(
        f"  Join: {matched:,} cruzados | "
        f"{option_instruments-matched:,} series sem posicao"
    )
    print(f"  Sanidade: {sanity_ratio:.1%}")
    print(f"  Changed: {changed} arquivos")


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    now = datetime.now(BRT)
    print("=" * 72)
    print("  Opcoes B3 — SI_C_OPCPOSABEMP + InstrumentsConsolidated")
    print(f"  {now.strftime('%Y-%m-%d %H:%M:%S')} BRT")
    print("=" * 72)

    print("\n[1/5] Localizando snapshot mais recente nas duas fontes...")
    date_str, inst_rows, pos_rows, inst_raw, pos_raw = download_latest_sources()
    data_date = date_str.replace("-", "")
    print(f"  Data escolhida: {date_str}")

    (TEMP_DIR / "InstrumentsConsolidated.csv").write_bytes(inst_raw)
    (TEMP_DIR / POSITIONS_JSON).write_bytes(pos_raw)

    print("\n[2/5] Processando InstrumentsConsolidated...")
    instruments = parse_instruments(inst_rows)

    print(f"\n[3/5] Processando {POSITIONS_JSON}...")
    positions, position_stats = parse_positions_json(pos_rows)

    print("\n[4/5] Cruzando dados...")
    by_ticker, join_stats = build_options(instruments, positions)

    validate_sources(
        instruments_count=len(instruments),
        positions_count=len(positions),
        matched=join_stats["matched"],
        sanity_ratio=position_stats["sanity_ratio"],
    )

    print("\n[5/5] Gerando JSONs...")
    changed = save_jsons(data_date, by_ticker)

    print(f"\n  Tickers: {len(by_ticker)} | Alterados: {changed}")

    save_report(
        data_date=date_str,
        instruments_rows=len(inst_rows),
        option_instruments=len(instruments),
        positions_rows=len(pos_rows),
        option_positions=len(positions),
        matched=join_stats["matched"],
        sanity_ratio=position_stats["sanity_ratio"],
        metadata_mismatch=join_stats["metadata_mismatch"],
        tickers_count=len(by_ticker),
        changed=changed,
        instruments_raw=inst_raw,
        positions_raw=pos_raw,
    )

    github_output("updated", "true" if changed > 0 else "false")
    github_output("data_date", data_date)
    print("\nConcluido.")


if __name__ == "__main__":
    main()
