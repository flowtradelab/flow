"""
scripts/update_options.py
==========================
1. Baixa InstrumentsConsolidated.csv (cadastro dos instrumentos) da B3
2. Baixa DerivativesOpenPosition.csv (posicoes em aberto) da B3
3. Faz JOIN pelo ticker da opcao (TckrSymb)
4. Gera grid-options/{TICKER}/latest.json com metadados + OI completos
5. Commit via GitHub Actions

Fontes:
- InstrumentsConsolidated: strike, vencimento, tipo, estilo, ativo objeto
- DerivativesOpenPosition: OI, variacao de OI, coberta/descoberta,
  total de posicoes, tomadores e doadores

Os downloads usam a API publica de arquivos da B3 em duas etapas:
  GET /api/download/requestname?fileName=...&date=YYYY-MM-DD
  GET /api/download/?token=...
"""

import csv
import io
import json
import os
import time
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests


# ── Constantes ────────────────────────────────────────────────────────────────
B3_API_BASE       = "https://arquivos.b3.com.br"
B3_TOKEN_URL      = f"{B3_API_BASE}/api/download/requestname"
B3_DOWNLOAD_URL   = f"{B3_API_BASE}/api/download/"  # barra final e importante
INSTRUMENTS_FILE  = "InstrumentsConsolidated"
POSITIONS_FILE    = "DerivativesOpenPosition"

OUTPUT_FOLDER     = Path("grid-options")
TEMP_DIR          = Path("/tmp")
BRT               = timezone(timedelta(hours=-3))
MAX_RETRIES       = 3
REQUEST_TIMEOUT   = 300
CSV_ENCODING      = "iso-8859-1"

# Protecao contra publicar milhares de zeros se a B3 mudar o schema novamente.
# DerivativesOpenPosition contem apenas contratos com posicao em aberto, enquanto
# InstrumentsConsolidated contem todas as series autorizadas. Por isso nao faz
# sentido exigir uma cobertura percentual minima sobre todas as series.
MIN_MATCHED       = 50

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/127.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/html, */*",
    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
    "Origin": B3_API_BASE,
    "Referer": f"{B3_API_BASE}/",
}

# Fallback apenas se o InstrumentsConsolidated nao trouxer o ativo objeto.
ATIVO_OBJETO_MAP = {
    "PETR": "PETR4",
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


# ── Download B3 CSV ───────────────────────────────────────────────────────────
def get_download_token(api_name: str, date_str: str) -> str | None:
    """Solicita o token de download para uma tabela/data da B3."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = requests.get(
                B3_TOKEN_URL,
                params={"fileName": api_name, "date": date_str},
                headers=HEADERS,
                timeout=30,
            )
            if r.status_code == 200:
                payload = r.json()
                token = payload.get("token", "")
                return token or None
            if r.status_code == 400:
                return None
            print(f"    {api_name} {date_str}: HTTP {r.status_code}")
        except Exception as e:
            print(f"    {api_name} {date_str}: erro token ({e})")
        if attempt < MAX_RETRIES:
            time.sleep(attempt)
    return None


def download_csv_from_token(token: str, api_name: str) -> bytes:
    """Baixa o CSV usando o token retornado pela B3."""
    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = requests.get(
                B3_DOWNLOAD_URL,
                params={"token": token},
                headers=HEADERS,
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
    """
    Converte o CSV da B3 em lista de dicts.

    Alguns arquivos possuem na primeira linha:
      Status do Arquivo: Final
    DerivativesOpenPosition normalmente comeca direto pelo cabecalho.
    """
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


def download_latest_pair() -> tuple[str, list[dict], list[dict], bytes, bytes]:
    """
    Procura a data mais recente em que as duas tabelas estejam publicadas.
    Exige a mesma data para cadastro e posicoes, evitando JOIN entre snapshots
    de dias diferentes.
    """
    for d in dias_uteis_recentes(7):
        date_str = d.strftime("%Y-%m-%d")
        print(f"  Tentando {date_str}...")

        pos_token = get_download_token(POSITIONS_FILE, date_str)
        if not pos_token:
            print("    DerivativesOpenPosition indisponivel")
            continue

        inst_token = get_download_token(INSTRUMENTS_FILE, date_str)
        if not inst_token:
            print("    InstrumentsConsolidated indisponivel")
            continue

        print("    Baixando DerivativesOpenPosition...")
        pos_raw = download_csv_from_token(pos_token, POSITIONS_FILE)
        print(f"      {len(pos_raw)/1024/1024:.1f} MB")

        print("    Baixando InstrumentsConsolidated...")
        inst_raw = download_csv_from_token(inst_token, INSTRUMENTS_FILE)
        print(f"      {len(inst_raw)/1024/1024:.1f} MB")

        positions, pos_status = parse_b3_csv(pos_raw)
        instruments, inst_status = parse_b3_csv(inst_raw)

        if inst_status and inst_status.lower() != "final":
            print(f"    InstrumentsConsolidated status={inst_status}; ignorando snapshot parcial")
            continue
        if pos_status and pos_status.lower() != "final":
            print(f"    DerivativesOpenPosition status={pos_status}; ignorando snapshot parcial")
            continue

        if not positions or not instruments:
            print("    CSV vazio; tentando data anterior")
            continue

        print(
            f"    OK: {len(instruments):,} instrumentos | "
            f"{len(positions):,} posicoes"
        )
        return date_str, instruments, positions, inst_raw, pos_raw

    raise RuntimeError(
        "Nao foi encontrada uma data recente com InstrumentsConsolidated "
        "e DerivativesOpenPosition publicados."
    )


# ── Conversores ───────────────────────────────────────────────────────────────
def parse_int(value) -> int:
    s = str(value or "").strip()
    if not s or s in ("-", "N/A", "NA"):
        return 0
    s = s.replace(" ", "").replace(".", "").replace(",", "")
    try:
        return int(s)
    except Exception:
        try:
            return int(float(s))
        except Exception:
            return 0


def parse_float(value) -> float:
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


def normalize_date(value: str) -> str:
    raw = (value or "").strip()
    if len(raw) == 8 and raw.isdigit():
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
    return raw


# ── Parsers das duas fontes ───────────────────────────────────────────────────
def parse_instruments(rows: list[dict]) -> dict:
    result = {}

    for row in rows:
        ticker = row.get("TckrSymb", "").strip().upper()
        if not ticker or len(ticker) < 4:
            continue

        tipo = normalize_option_type(row.get("OptnTp", ""), row.get("SgmtNm", ""))
        if not tipo:
            continue

        strike = parse_float(row.get("ExrcPric", ""))
        venc = normalize_date(row.get("XprtnDt", ""))
        if not venc:
            continue

        base_key = ticker[:4]
        underlying = (
            row.get("UndrlygTckrSymb1", "").strip().upper()
            or ATIVO_OBJETO_MAP.get(base_key, base_key)
        )

        result[ticker] = {
            "ativo_objeto": underlying,
            "tipo": tipo,
            "estilo": normalize_style(row.get("OptnStyle", "")),
            "strike": strike,
            "vencimento": venc,
            "premio": 0.0,
        }

    print(f"    Instruments: {len(result):,} opcoes")
    return result


def parse_positions(rows: list[dict], valid_option_tickers: set[str]) -> dict:
    """
    Le DerivativesOpenPosition e mantem somente os registros cujo TckrSymb
    existe no universo de opcoes do InstrumentsConsolidated.

    IMPORTANTE: SgmtNm NAO indica Call/Put. Esse campo representa o segmento
    de pos-negociacao (ex.: financeiro, derivativos de acoes etc.), portanto
    nao deve ser usado para filtrar tipo de opcao.
    """
    result = {}
    segment_counts = defaultdict(int)
    sample_rows = []

    for row in rows:
        ticker = row.get("TckrSymb", "").strip().upper()
        if not ticker:
            continue

        segment = row.get("SgmtNm", "").strip() or "(vazio)"
        segment_counts[segment] += 1

        if len(sample_rows) < 12:
            sample_rows.append(
                f"{ticker} | Asst={row.get('Asst','')} | "
                f"XprtnCd={row.get('XprtnCd','')} | SgmtNm={segment}"
            )

        # O filtro de opcao e feito pelo cadastro mestre, nao pelo SgmtNm.
        if ticker not in valid_option_tickers:
            continue

        result[ticker] = {
            "open_interest": parse_int(row.get("OpnIntrst", "")),
            "variacao_open_interest": parse_int(row.get("VartnOpnIntrst", "")),
            "qtd_coberta": parse_int(row.get("CvrdQty", "")),
            "total_posicoes_bloqueadas": parse_int(row.get("TtlBlckdPos", "")),
            "qtd_descoberta": parse_int(row.get("UcvrdQty", "")),
            "total_posicoes": parse_int(row.get("TtlPos", "")),
            "qtd_tomadores": parse_int(row.get("BrrwrQty", "")),
            "qtd_doadores": parse_int(row.get("LndrQty", "")),
        }

    if rows:
        print(f"    Colunas Open Position: {', '.join(rows[0].keys())}")
    if segment_counts:
        top_segments = sorted(
            segment_counts.items(), key=lambda x: x[1], reverse=True
        )[:10]
        print(
            "    Segmentos recebidos: "
            + ", ".join(f"{name}={count}" for name, count in top_segments)
        )
    if sample_rows:
        print("    Amostra TckrSymb recebidos:")
        for sample in sample_rows:
            print(f"      {sample}")

    print(
        f"    Open Position: {len(result):,} opcoes com ticker "
        f"presente no InstrumentsConsolidated"
    )
    return result


# ── JOIN ──────────────────────────────────────────────────────────────────────
def build_options(instruments: dict, positions: dict) -> tuple[dict, int]:
    by_ticker = defaultdict(list)
    matched = 0

    for ticker_opcao, inst in instruments.items():
        base_key = ticker_opcao[:4].upper()
        pos = positions.get(ticker_opcao)
        if pos is not None:
            matched += 1
        else:
            pos = {}

        by_ticker[base_key].append({
            "ticker": ticker_opcao,
            "ativo_objeto": inst["ativo_objeto"],
            "tipo": inst["tipo"],
            "estilo": inst["estilo"],
            "strike": inst["strike"],
            "vencimento": inst["vencimento"],
            "premio": inst["premio"],
            "open_interest": pos.get("open_interest", 0),
            "variacao_open_interest": pos.get("variacao_open_interest", 0),
            "qtd_coberta": pos.get("qtd_coberta", 0),
            "total_posicoes_bloqueadas": pos.get("total_posicoes_bloqueadas", 0),
            "qtd_descoberta": pos.get("qtd_descoberta", 0),
            "total_posicoes": pos.get("total_posicoes", 0),
            "qtd_tomadores": pos.get("qtd_tomadores", 0),
            "qtd_doadores": pos.get("qtd_doadores", 0),
            "com_oi": ticker_opcao in positions,
        })

    print(
        f"    JOIN: {matched:,} com posicao | "
        f"{len(instruments)-matched:,} sem posicao"
    )
    return dict(by_ticker), matched


def validate_join(
    instruments_count: int,
    matched: int,
    parsed_positions_count: int,
    raw_positions_count: int,
):
    authorized_ratio = matched / instruments_count if instruments_count else 0.0
    raw_ratio = matched / raw_positions_count if raw_positions_count else 0.0

    print(f"    Cobertura sobre series autorizadas: {authorized_ratio:.2%}")
    print(f"    Matches / linhas Open Position: {matched:,}/{raw_positions_count:,} ({raw_ratio:.1%})")

    if matched < MIN_MATCHED:
        raise RuntimeError(
            "JOIN anormal entre InstrumentsConsolidated e "
            f"DerivativesOpenPosition: apenas {matched:,} tickers de opcao "
            f"foram encontrados entre {raw_positions_count:,} linhas do arquivo "
            f"({parsed_positions_count:,} linhas reconhecidas como opcoes por ticker). "
            "JSONs NAO serao sobrescritos. Confira a amostra de TckrSymb impressa acima."
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
            "derivatives_open_position": {
                "nome": POSITIONS_FILE,
                "tamanho_mb": round(len(positions_raw) / 1024 / 1024, 2),
                "linhas_total": positions_rows,
                "opcoes_com_posicao": option_positions,
            },
        },
        "processamento": {
            "opcoes_com_join": matched,
            "opcoes_sem_posicao": option_instruments - matched,
            "cobertura_join_pct": round(
                (matched / option_instruments * 100) if option_instruments else 0,
                2,
            ),
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
        "fonte": "DerivativesOpenPosition+InstrumentsConsolidated",
    }, ensure_ascii=False))

    hist.write_text("\n".join(lines[-30:]) + "\n", encoding="utf-8")

    print("\n  Relatorio: logs/last_run.json")
    print(
        f"  Instruments: {instruments_rows:,} linhas | "
        f"{option_instruments:,} opcoes"
    )
    print(
        f"  Open Position: {positions_rows:,} linhas | "
        f"{option_positions:,} opcoes"
    )
    print(
        f"  Join: {matched:,} cruzados | "
        f"{option_instruments-matched:,} sem posicao"
    )
    print(f"  Changed: {changed} arquivos")


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    now = datetime.now(BRT)
    print("=" * 68)
    print("  Opcoes B3 — InstrumentsConsolidated + DerivativesOpenPosition")
    print(f"  {now.strftime('%Y-%m-%d %H:%M:%S')} BRT")
    print("=" * 68)

    print("\n[1/5] Localizando snapshot mais recente nas duas fontes...")
    date_str, inst_rows, pos_rows, inst_raw, pos_raw = download_latest_pair()
    data_date = date_str.replace("-", "")
    print(f"  Data escolhida: {date_str}")

    (TEMP_DIR / "InstrumentsConsolidated.csv").write_bytes(inst_raw)
    (TEMP_DIR / "DerivativesOpenPosition.csv").write_bytes(pos_raw)

    print("\n[2/5] Processando InstrumentsConsolidated...")
    instruments = parse_instruments(inst_rows)

    print("\n[3/5] Processando DerivativesOpenPosition...")
    positions = parse_positions(pos_rows, set(instruments.keys()))

    print("\n[4/5] Cruzando dados...")
    by_ticker, matched = build_options(instruments, positions)
    validate_join(
        len(instruments),
        matched,
        len(positions),
        len(pos_rows),
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
        matched=matched,
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
