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


# ── Playwright download helper ────────────────────────────────────────────────
def playwright_download(page_url, link_text, fallback_url, out_path, accept="*/*"):
    """Abre page_url, encontra o link pelo texto e baixa o arquivo."""
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

                # Tenta encontrar link pelo texto
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

                # Fallback
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

            # Download
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

            # Descompacta ZIP se necessário
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


def direct_download(url, out_path, cookies_str=""):
    """Tenta download direto sem browser (mais rápido se funcionar)."""
    import requests as req
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept":     "application/pdf,application/octet-stream,*/*",
        "Cookie":     cookies_str,
    }
    r = req.get(url, headers=headers, timeout=180, stream=True)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}")
    raw = b"".join(r.iter_content(65536))
    Path(out_path).write_bytes(raw)
    return raw


# ── Parser SI_D_SEDE.txt ──────────────────────────────────────────────────────
def parse_series(filepath) -> tuple[str, dict]:
    """Retorna (data_date, {ticker_opcao: {tipo,estilo,strike,vencimento,premio}})"""
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
    """
    Retorna {ticker_opcao: {qtd_descoberta, open_interest, qtd_tomadores, qtd_doadores}}
    Colunas (split por espaço):
      [0]  ticker_opcao
      [2]  ticker_base
      [5]  tipo (CALL/PUT)
      [9]  qtd_descoberta
      [11] open_interest (total posições)
      [13] qtd_tomadores
      [14] qtd_doadores
    """
    result = {}
    pages_read = 0

    def parse_int(s):
        try: return int(s.replace(".", "").replace(",", ""))
        except: return 0

    with pdfplumber.open(filepath) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            # Só processa páginas com dados de posições
            if "tomador" not in text.lower() and "descobert" not in text.lower():
                continue

            pages_read += 1
            for line in text.split("\n"):
                parts = line.split()
                # Linha válida: começa com ticker (letra+número), tem ~15+ campos
                if len(parts) < 14:
                    continue
                ticker = parts[0]
                if not (len(ticker) >= 4 and ticker[:4].isalpha()):
                    continue
                # Evita linha de header
                if ticker.upper() in ("CÓDIGO", "INSTRUMENTO", "REFERENTE"):
                    continue

                try:
                    isin = parts[1] if len(parts) > 1 else ""
                    # ISIN brasileiro: BR + 4 letras ticker + 1 dígito classe + resto
                    # ex: BRPETR4J1KC6 → ativo_objeto = PETR4
                    import re as _re
                    m = _re.match(r'^BR([A-Z]{4}\d)', isin)
                    ativo_objeto = m.group(1) if m else ""

                    result[ticker] = {
                        "ativo_objeto":   ativo_objeto,
                        "qtd_descoberta": parse_int(parts[9])  if len(parts) > 9  else 0,
                        "open_interest":  parse_int(parts[11]) if len(parts) > 11 else 0,
                        "qtd_tomadores":  parse_int(parts[13]) if len(parts) > 13 else 0,
                        "qtd_doadores":   parse_int(parts[14]) if len(parts) > 14 else 0,
                    }
                except Exception:
                    continue

    print(f"    BDI: {len(result):,} opções em {pages_read} páginas")
    return result


# ── JOIN + agrupamento por ticker base ────────────────────────────────────────
def build_options(series: dict, bdi: dict) -> dict:
    """
    Cruza os dois dicts pelo ticker_opcao.
    Retorna {ticker_base: [opcoes]}
    """
    by_ticker = defaultdict(list)
    matched   = 0
    no_bdi    = 0

    for ticker_opcao, s in series.items():
        base_key = ticker_opcao[:4].upper()
        oi_data  = bdi.get(ticker_opcao, {})

        if oi_data:
            matched += 1
        else:
            no_bdi += 1

        ativo_objeto = oi_data.get("ativo_objeto", "")
        # Fallback: se não veio do BDI, tenta inferir pelo ticker_opcao
        # (os 4 primeiros chars + último char do SI_D_SEDE não têm o número do ativo)
        # Mantém vazio se não souber — app pode usar o ticker_base como fallback

        entry = {
            "ticker":         ticker_opcao,
            "ativo_objeto":   ativo_objeto,
            "tipo":           s["tipo"],
            "estilo":         s["estilo"],
            "strike":         s["strike"],
            "vencimento":     s["vencimento"],
            "premio":         s["premio"],
            "qtd_descoberta": oi_data.get("qtd_descoberta", 0),
            "open_interest":  oi_data.get("open_interest",  0),
            "qtd_tomadores":  oi_data.get("qtd_tomadores",  0),
            "qtd_doadores":   oi_data.get("qtd_doadores",   0),
        }
        by_ticker[base_key].append(entry)

    print(f"    JOIN: {matched:,} com OI  |  {no_bdi:,} sem OI (só séries)")
    return dict(by_ticker)


# ── Salva JSONs ───────────────────────────────────────────────────────────────
def save_jsons(data_date: str, by_ticker: dict) -> int:
    OUTPUT_FOLDER.mkdir(parents=True, exist_ok=True)
    changed = 0

    for ticker, opcoes in sorted(by_ticker.items()):
        opcoes_s = sorted(opcoes, key=lambda x: (x["vencimento"], x["tipo"], x["strike"]))
        payload  = {
            "ticker": ticker,
            "data":   data_date,
            "total":  len(opcoes_s),
            "opcoes": opcoes_s,
        }
        content  = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
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
        today     = datetime.now(BRT).date()
        delta     = (today - file_date).days
        return delta <= 5, file_date
    except Exception:
        return False, None


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    now = datetime.now(BRT)
    print("=" * 60)
    print("  Opções B3 — Atualização Automática (Séries + BDI)")
    print(f"  {now.strftime('%Y-%m-%d %H:%M:%S')} BRT")
    print("=" * 60)
    print()

    # ── 1. Download SI_D_SEDE ─────────────────────────────────────────────────
    print("[1/5] Download SI_D_SEDE (Séries Autorizadas)...")
    series_path = TEMP_DIR / "SI_D_SEDE.txt"
    playwright_download(
        page_url     = B3_SERIES_URL,
        link_text    = B3_SERIES_TEXT,
        fallback_url = B3_SERIES_FB,
        out_path     = series_path,
        accept       = "application/zip,text/plain,*/*",
    )

    # Valida data
    with open(series_path, encoding="latin-1") as f:
        first = f.readline()
    data_date = first.strip().split("|")[1] if "|" in first else ""
    print(f"  Data séries: {data_date}")

    valid, _ = is_fresh(data_date)
    if not valid:
        print("  AVISO: data muito antiga (>5 dias)")
        github_output("updated", "stale")
        github_output("data_date", data_date)
        return

    # ── 2. Download BDI ───────────────────────────────────────────────────────
    print("\n[2/5] Download BDI (Posições em Aberto)...")

    # Tenta data do arquivo de séries primeiro, depois datas recentes
    file_dt  = datetime.strptime(data_date, "%Y%m%d")
    bdi_path = None
    bdi_url  = None

    # Tenta direto primeiro (mais rápido)
    for days_back in range(0, 5):
        dt  = file_dt - timedelta(days=days_back)
        url = get_bdi_url(dt)
        print(f"  Tentando download direto: {url}")
        try:
            import requests as req
            r = req.head(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code == 200:
                bdi_url = url
                print(f"  URL acessível diretamente!")
                break
        except Exception:
            pass

    bdi_path = TEMP_DIR / "bdi.pdf"

    if bdi_url:
        # Download direto sem Playwright
        import requests as req
        print(f"  Baixando diretamente...")
        r = req.get(bdi_url, timeout=180, stream=True,
                    headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        raw = b"".join(r.iter_content(65536))
        bdi_path.write_bytes(raw)
        print(f"  {len(raw):,} bytes")
    else:
        # Playwright como fallback
        print("  Download direto falhou — usando Playwright...")
        bdi_fb = get_bdi_url(file_dt)
        playwright_download(
            page_url     = B3_BDI_PAGE,
            link_text    = B3_BDI_TEXT,
            fallback_url = bdi_fb,
            out_path     = bdi_path,
            accept       = "application/pdf,*/*",
        )

    # ── 3. Parse séries ───────────────────────────────────────────────────────
    print("\n[3/5] Processando séries autorizadas...")
    data_date, series = parse_series(series_path)

    # ── 4. Parse BDI ─────────────────────────────────────────────────────────
    print("\n[4/5] Processando BDI (posições em aberto)...")
    bdi_data = parse_bdi(bdi_path)

    # ── 5. JOIN + salvar ──────────────────────────────────────────────────────
    print("\n[5/5] Gerando JSONs...")
    by_ticker = build_options(series, bdi_data)
    changed   = save_jsons(data_date, by_ticker)

    print(f"\n  Tickers: {len(by_ticker)}")
    print(f"  Arquivos alterados: {changed}")

    # Exemplo de saída
    example = sorted(by_ticker.keys())[0]
    sample  = by_ticker[example][0]
    print(f"\n  Exemplo ({example}/latest.json):")
    print(f"  {json.dumps(sample, ensure_ascii=False)}")

    github_output("updated",   "true" if changed > 0 else "false")
    github_output("data_date", data_date)
    print("\nConcluído.")


if __name__ == "__main__":
    main()
