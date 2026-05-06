"""
scripts/update_options.py
==========================
1. Baixa SI_D_SEDE.txt  (séries autorizadas) via Playwright
2. Baixa BDI_03-4_YYYYMMDD.pdf (posições em aberto) via Playwright
3. Faz JOIN pelo ticker da opção
4. Gera grid-options/{TICKER}/latest.json com dados completos
5. Commit via GitHub Actions
"""

import os, re, json, time, zipfile, io
import pdfplumber
from datetime import datetime, timezone, timedelta
from collections import defaultdict
from pathlib import Path

# ── Constantes ────────────────────────────────────────────────────────────────
B3_SERIES_URL  = "https://www.b3.com.br/pt_br/market-data-e-indices/servicos-de-dados/market-data/consultas/mercado-a-vista/opcoes/series-autorizadas/"
B3_SERIES_TEXT = "Lista Completa de Séries Autorizadas"
B3_SERIES_FB   = "https://www.b3.com.br/lumis/portal/file/fileDownload.jsp?fileId=8AA8D0CC9DF273EF019DF53B3C720DBE"

B3_BDI_PAGE    = "https://www.b3.com.br/pt_br/market-data-e-indices/servicos-de-dados/market-data/historico/mercados-a-vista-e-derivativos/bdi/"
B3_BDI_TEXT    = "BDI"

OUTPUT_FOLDER  = Path("grid-options")
TEMP_DIR       = Path("/tmp")
BRT            = timezone(timedelta(hours=-3))
MAX_RETRIES    = 3
NAV_TIMEOUT    = 120000
PAGE_TIMEOUT   = 90000


def github_output(key, value):
    gh = os.environ.get("GITHUB_OUTPUT", "")
    if gh:
        with open(gh, "a") as f:
            f.write(f"{key}={value}\n")
    print(f"[output] {key}={value}")


def get_bdi_url(date: datetime) -> str:
    d = date.strftime("%Y-%m-%d")
    c = date.strftime("%Y%m%d")
    return f"https://arquivos.b3.com.br/bdi/download/bdi/{d}/BDI_03-4_{c}.pdf"


def dias_uteis_recentes(n=7):
    """
    Retorna os últimos N dias úteis (seg-sex) a partir de hoje (BRT),
    em ordem decrescente (mais recente primeiro).
    Ignora fins de semana mas não feriados (BDI não existe nesses dias).
    """
    hoje  = datetime.now(BRT).date()
    dias  = []
    delta = 0
    while len(dias) < n:
        d = hoje - timedelta(days=delta)
        if d.weekday() < 5:   # 0=seg … 4=sex
            dias.append(d)
        delta += 1
    return dias


# ── Playwright download helper ────────────────────────────────────────────────
def playwright_download(page_url, link_text, fallback_url, out_path, accept="*/*"):
    from playwright.sync_api import sync_playwright

    for attempt in range(1, MAX_RETRIES + 1):
        print(f"    Tentativa {attempt}/{MAX_RETRIES}...")
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(
                    headless=True,
                    args=["--no-sandbox","--disable-setuid-sandbox",
                          "--disable-dev-shm-usage","--disable-gpu",
                          "--no-first-run","--single-process"]
                )
                ctx = browser.new_context(
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                    locale="pt-BR",
                    timezone_id="America/Sao_Paulo",
                )
                ctx.set_default_timeout(PAGE_TIMEOUT)
                pg = ctx.new_page()
                pg.route("**/*.{png,jpg,jpeg,gif,svg,ico,woff,woff2}", lambda r: r.abort())

                print(f"    Carregando: {page_url}")
                pg.goto(page_url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
                time.sleep(4)

                dl_url = None
                try:
                    link = pg.locator(f"a:has-text('{link_text}')").first
                    if link.count() > 0:
                        href = link.get_attribute("href", timeout=5000)
                        if href:
                            dl_url = href if href.startswith("http") else f"https://www.b3.com.br{href}"
                            print(f"    Link encontrado: {dl_url}")
                except Exception:
                    pass

                if not dl_url:
                    html = pg.content()
                    m = re.search(r'href=["\']([^"\']*fileDownload[^"\']*)["\']', html)
                    if m:
                        href = m.group(1)
                        dl_url = href if href.startswith("http") else f"https://www.b3.com.br{href}"
                if not dl_url:
                    dl_url = fallback_url
                    print(f"    Usando fallback: {dl_url}")

                cookies = ctx.cookies()
                browser.close()

            import requests as req
            cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept":     accept,
                "Referer":    page_url,
                "Cookie":     cookie_str,
            }
            print(f"    Baixando...")
            r = req.get(dl_url, headers=headers, timeout=180, stream=True)
            if r.status_code != 200:
                raise RuntimeError(f"HTTP {r.status_code}")
            raw = b"".join(r.iter_content(65536))
            print(f"    {len(raw):,} bytes  CT: {r.headers.get('Content-Type','?')}")

            import builtins
            builtins._last_dl_headers = {
                "content_type":        r.headers.get("Content-Type", ""),
                "last_modified":       r.headers.get("Last-Modified", ""),
                "content_disposition": r.headers.get("Content-Disposition", ""),
                "content_length":      r.headers.get("Content-Length", ""),
                "etag":                r.headers.get("ETag", ""),
            }

            if raw[:2] == b"PK":
                print("    ZIP detectado — extraindo...")
                with zipfile.ZipFile(io.BytesIO(raw)) as zf:
                    names = zf.namelist()
                    print(f"    Conteúdo: {names}")
                    fname = next((n for n in names if n.upper().endswith((".TXT",".PDF"))), names[0])
                    raw = zf.read(fname)
                    print(f"    Extraído: {fname} ({len(raw):,} bytes)")

            Path(out_path).write_bytes(raw)
            print(f"    Salvo em: {out_path}")
            return str(out_path)

        except Exception as e:
            print(f"    Erro: {e}")
            if attempt < MAX_RETRIES:
                wait = 20 * attempt
                print(f"    Aguardando {wait}s...")
                time.sleep(wait)
            else:
                raise RuntimeError(f"Download falhou após {MAX_RETRIES} tentativas: {e}")


# ── Parser SI_D_SEDE.txt ──────────────────────────────────────────────────────
def parse_series_header(filepath) -> dict:
    with open(filepath, encoding="latin-1") as f:
        first = f.readline().strip()
    parts = first.split("|")
    return {
        "data_pregao":  parts[1] if len(parts) > 1 else "",
        "data_geracao": parts[2] if len(parts) > 2 else "",
        "hora_geracao": parts[3] if len(parts) > 3 else "",
    }


def parse_series(filepath) -> tuple[str, dict]:
    result    = {}
    data_date = ""

    with open(filepath, encoding="latin-1") as f:
        lines = f.readlines()

    if lines:
        p0 = lines[0].strip().split("|")
        data_date = p0[1] if len(p0) > 1 else ""

    for line in lines[1:]:
        parts = line.strip().split("|")
        if len(parts) < 19 or parts[0] != "02":
            continue

        tipo_mercado = parts[3].strip()
        ticker_opcao = parts[13].strip()
        estilo       = parts[15].strip()
        venc_raw     = parts[17].strip()

        if not ticker_opcao or len(ticker_opcao) < 4:
            continue

        tipo = "C" if "COMPRA" in tipo_mercado else "P"

        try:    strike = round(float(parts[16].strip()), 2)
        except: strike = 0.0

        venc = (f"{venc_raw[:4]}-{venc_raw[4:6]}-{venc_raw[6:8]}"
                if len(venc_raw) == 8 and venc_raw.isdigit() else venc_raw)

        try:    preco = round(float(parts[18].strip()), 6)
        except: preco = 0.0

        result[ticker_opcao] = {
            "tipo":       tipo,
            "estilo":     estilo,
            "strike":     strike,
            "vencimento": venc,
            "premio":     preco,
        }

    print(f"    Séries: {len(result):,} opções")
    return data_date, result


# ── Parser BDI PDF ────────────────────────────────────────────────────────────
def parse_bdi(filepath) -> dict:
    result     = {}
    pages_read = 0

    def parse_int(s):
        try: return int(s.replace(".", "").replace(",", ""))
        except: return 0

    with pdfplumber.open(filepath) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            if "tomador" not in text.lower() and "descobert" not in text.lower():
                continue
            pages_read += 1
            for line in text.split("\n"):
                parts = line.split()
                if len(parts) < 14:
                    continue
                ticker = parts[0]
                if not (len(ticker) >= 4 and ticker[:4].isalpha()):
                    continue
                if ticker.upper() in ("CÓDIGO", "INSTRUMENTO", "REFERENTE"):
                    continue
                try:
                    isin = parts[1] if len(parts) > 1 else ""
                    m    = re.match(r'^BR([A-Z]{4}\d)', isin)
                    result[ticker] = {
                        "ativo_objeto":   m.group(1) if m else "",
                        "qtd_descoberta": parse_int(parts[9])  if len(parts) > 9  else 0,
                        "open_interest":  parse_int(parts[11]) if len(parts) > 11 else 0,
                        "qtd_tomadores":  parse_int(parts[13]) if len(parts) > 13 else 0,
                        "qtd_doadores":   parse_int(parts[14]) if len(parts) > 14 else 0,
                    }
                except Exception:
                    continue

    print(f"    BDI: {len(result):,} opções em {pages_read} páginas")
    return result


# ── JOIN ──────────────────────────────────────────────────────────────────────
def build_options(series: dict, bdi: dict) -> dict:
    by_ticker = defaultdict(list)
    matched   = 0

    for ticker_opcao, s in series.items():
        base_key = ticker_opcao[:4].upper()
        oi_data  = bdi.get(ticker_opcao, {})
        if oi_data:
            matched += 1

        by_ticker[base_key].append({
            "ticker":         ticker_opcao,
            "ativo_objeto":   oi_data.get("ativo_objeto", ""),
            "tipo":           s["tipo"],
            "estilo":         s["estilo"],
            "strike":         s["strike"],
            "vencimento":     s["vencimento"],
            "premio":         s["premio"],
            "qtd_descoberta": oi_data.get("qtd_descoberta", 0),
            "open_interest":  oi_data.get("open_interest",  0),
            "qtd_tomadores":  oi_data.get("qtd_tomadores",  0),
            "qtd_doadores":   oi_data.get("qtd_doadores",   0),
        })

    print(f"    JOIN: {matched:,} com OI  |  {len(series)-matched:,} sem OI")
    return dict(by_ticker)


# ── Salva JSONs ───────────────────────────────────────────────────────────────
def save_jsons(data_date: str, by_ticker: dict) -> int:
    OUTPUT_FOLDER.mkdir(parents=True, exist_ok=True)
    changed = 0
    for ticker, opcoes in sorted(by_ticker.items()):
        opcoes_s = sorted(opcoes, key=lambda x: (x["vencimento"], x["tipo"], x["strike"]))
        content  = json.dumps(
            {"ticker": ticker, "data": data_date, "total": len(opcoes_s), "opcoes": opcoes_s},
            ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        out_dir  = OUTPUT_FOLDER / ticker
        out_dir.mkdir(parents=True, exist_ok=True)
        out_file = out_dir / "latest.json"
        if out_file.exists() and out_file.read_bytes() == content:
            continue
        out_file.write_bytes(content)
        changed += 1
    return changed


def is_fresh(date_str: str) -> tuple[bool, object]:
    try:
        file_date = datetime.strptime(date_str, "%Y%m%d").date()
        delta     = (datetime.now(BRT).date() - file_date).days
        return delta <= 5, file_date
    except Exception:
        return False, None


# ── Relatório ─────────────────────────────────────────────────────────────────
def save_report(data_date, series_count, bdi_count, matched, tickers_count,
                changed, series_path, bdi_path, bdi_date_used, errors=None):
    import builtins
    now      = datetime.now(BRT)
    bdi_size = Path(bdi_path).stat().st_size    if Path(bdi_path).exists() else 0
    ser_size = Path(series_path).stat().st_size if Path(series_path).exists() else 0
    hdr      = parse_series_header(series_path) if Path(series_path).exists() else {}
    dl_hdr   = getattr(builtins, "_last_dl_headers", {})

    def fmt(d):
        return f"{d[:4]}-{d[4:6]}-{d[6:8]}" if d and len(d)==8 and d.isdigit() else d

    report = {
        "status":          "ok" if not errors else "error",
        "executado_em":    now.strftime("%Y-%m-%d %H:%M:%S BRT"),
        "executado_em_ts": int(now.timestamp()),
        "arquivos": {
            "series_autorizadas": {
                "nome":               "SI_D_SEDE.txt",
                "tamanho_mb":         round(ser_size/1024/1024, 2),
                "data_pregao":        fmt(hdr.get("data_pregao","")),
                "data_geracao":       fmt(hdr.get("data_geracao","")),
                "hora_geracao":       hdr.get("hora_geracao",""),
                "last_modified_http": dl_hdr.get("last_modified",""),
                "opcoes_total":       series_count,
            },
            "bdi": {
                "nome":             f"BDI_03-4_{bdi_date_used}.pdf",
                "tamanho_mb":       round(bdi_size/1024/1024, 2),
                "data_referencia":  fmt(bdi_date_used),  # ← data real do BDI baixado
                "opcoes_com_oi":    bdi_count,
            },
        },
        "processamento": {
            "opcoes_com_join":    matched,
            "opcoes_sem_oi":      series_count - matched,
            "tickers_gerados":    tickers_count,
            "arquivos_alterados": changed,
        },
        "erros": errors or [],
    }

    logs_dir = Path("logs")
    logs_dir.mkdir(exist_ok=True)
    (logs_dir / "last_run.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    hist = logs_dir / "history.jsonl"
    lines = hist.read_text(encoding="utf-8").strip().split("\n") if hist.exists() else []
    lines = [l for l in lines if l.strip()]
    lines.append(json.dumps({
        "ts": report["executado_em"], "data_series": data_date,
        "data_bdi": bdi_date_used, "status": report["status"],
        "tickers": tickers_count, "changed": changed, "matched": matched,
    }, ensure_ascii=False))
    hist.write_text("\n".join(lines[-30:]) + "\n", encoding="utf-8")

    print(f"\n  Relatório: logs/last_run.json")
    print(f"  Séries:  {series_count:,} ({ser_size/1024:.0f} KB)")
    print(f"  BDI:     {bdi_count:,} com OI — data {fmt(bdi_date_used)} ({bdi_size/1024/1024:.1f} MB)")
    print(f"  Join:    {matched:,} cruzados | {series_count-matched:,} sem OI")
    print(f"  Changed: {changed} arquivos")


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    now = datetime.now(BRT)
    print("=" * 60)
    print("  Opções B3 — Atualização Automática (Séries + BDI)")
    print(f"  {now.strftime('%Y-%m-%d %H:%M:%S')} BRT")
    print("=" * 60)
    print()

    series_path = TEMP_DIR / "SI_D_SEDE.txt"
    bdi_path    = TEMP_DIR / "bdi.pdf"

    # ── 1. Download SI_D_SEDE ─────────────────────────────────────────────────
    print("[1/5] Download SI_D_SEDE...")
    playwright_download(
        page_url=B3_SERIES_URL, link_text=B3_SERIES_TEXT,
        fallback_url=B3_SERIES_FB, out_path=series_path,
        accept="application/zip,text/plain,*/*",
    )
    with open(series_path, encoding="latin-1") as f:
        first = f.readline()
    data_date = first.strip().split("|")[1] if "|" in first else ""
    print(f"  Data séries (SI_D_SEDE): {data_date}")

    valid, _ = is_fresh(data_date)
    if not valid:
        print("  AVISO: data muito antiga (>5 dias)")
        github_output("updated", "stale")
        github_output("data_date", data_date)
        return

    # ── 2. Download BDI — busca por data independente do SI_D_SEDE ───────────
    # O BDI é publicado com a data do pregão. Busca D-0 até D-6 (últimos dias úteis),
    # independente da data do SI_D_SEDE que pode estar desatualizada.
    print("\n[2/5] Download BDI...")
    bdi_url       = None
    bdi_date_used = None
    import requests as req

    dias = dias_uteis_recentes(n=7)
    print(f"  Buscando BDI nos últimos {len(dias)} dias úteis: {[str(d) for d in dias[:3]]}...")

    for d in dias:
        url = get_bdi_url(datetime.combine(d, datetime.min.time()))
        print(f"  Tentando: {url.split('/')[-1]} ... ", end="", flush=True)
        try:
            r = req.head(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code == 200:
                bdi_url       = url
                bdi_date_used = d.strftime("%Y%m%d")
                print("OK")
                break
            else:
                print(f"{r.status_code}")
        except Exception as e:
            print(f"erro ({e})")

    if bdi_url:
        print(f"  BDI encontrado: {bdi_date_used}")
        print(f"  Baixando diretamente...")
        r = req.get(bdi_url, timeout=180, stream=True, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        raw = b"".join(r.iter_content(65536))
        bdi_path.write_bytes(raw)
        print(f"  {len(raw)/1024/1024:.1f} MB")
    else:
        # Playwright fallback — tenta a página do BDI
        print("  Nenhum BDI encontrado via HEAD — tentando Playwright...")
        dias = dias_uteis_recentes(n=3)
        bdi_date_used = dias[0].strftime("%Y%m%d")  # D-0 ou D-1
        bdi_fb = get_bdi_url(datetime.combine(dias[0], datetime.min.time()))
        playwright_download(
            page_url=B3_BDI_PAGE, link_text=B3_BDI_TEXT,
            fallback_url=bdi_fb, out_path=bdi_path, accept="application/pdf,*/*",
        )

    # ── 3. Parse séries ───────────────────────────────────────────────────────
    print("\n[3/5] Processando séries...")
    data_date, series = parse_series(series_path)

    # ── 4. Parse BDI ─────────────────────────────────────────────────────────
    print("\n[4/5] Processando BDI...")
    bdi_data = parse_bdi(bdi_path)

    # ── 5. JOIN + salvar ──────────────────────────────────────────────────────
    print("\n[5/5] Gerando JSONs...")
    by_ticker = build_options(series, bdi_data)
    changed   = save_jsons(data_date, by_ticker)

    print(f"\n  Tickers: {len(by_ticker)} | Alterados: {changed}")

    save_report(
        data_date     = data_date,
        series_count  = len(series),
        bdi_count     = len(bdi_data),
        matched       = sum(1 for opts in by_ticker.values() for o in opts if o.get("ativo_objeto")),
        tickers_count = len(by_ticker),
        changed       = changed,
        series_path   = series_path,
        bdi_path      = bdi_path,
        bdi_date_used = bdi_date_used,
    )

    github_output("updated",   "true" if changed > 0 else "false")
    github_output("data_date", data_date)
    print("\nConcluído.")


if __name__ == "__main__":
    main()
