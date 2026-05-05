"""
scripts/update_options.py
==========================
1. Abre a página da B3 com Playwright
2. Baixa o arquivo (ZIP ou TXT)
3. Descompacta se necessário
4. Verifica data — se antiga, sinaliza "stale" para retry às 8h45
5. Converte para JSONs por ticker em grid-options/{TICKER}/latest.json
6. Sinaliza ao GitHub Actions se deve fazer commit
"""

import os, re, json, time, zipfile, io
from datetime import datetime, timezone, timedelta
from collections import defaultdict
from pathlib import Path

B3_PAGE_URL   = "https://www.b3.com.br/pt_br/market-data-e-indices/servicos-de-dados/market-data/consultas/mercado-a-vista/opcoes/series-autorizadas/"
B3_DIRECT_URL = "https://www.b3.com.br/lumis/portal/file/fileDownload.jsp?fileId=8AA8D0CC9DF273EF019DF53B3C720DBE"
LINK_TEXT     = "Lista Completa de Séries Autorizadas"
OUTPUT_FOLDER = Path("grid-options")
TEMP_FILE     = Path("/tmp/SI_D_SEDE.txt")
BRT           = timezone(timedelta(hours=-3))
MAX_RETRIES   = 3
PAGE_TIMEOUT  = 90000
NAV_TIMEOUT   = 120000


def github_output(key, value):
    gh = os.environ.get("GITHUB_OUTPUT", "")
    if gh:
        with open(gh, "a") as f:
            f.write(f"{key}={value}\n")
    print(f"[output] {key}={value}")


def extract_if_zip(raw_bytes):
    """Se for ZIP, extrai o primeiro .txt. Senão retorna os bytes originais."""
    if raw_bytes[:2] == b"PK":
        print("  Formato: ZIP — extraindo...")
        with zipfile.ZipFile(io.BytesIO(raw_bytes)) as zf:
            names = zf.namelist()
            print(f"  Conteúdo do ZIP: {names}")
            # Pega o .txt ou primeiro arquivo
            txt = next(
                (n for n in names if n.upper().endswith(".TXT")), names[0]
            )
            content = zf.read(txt)
            print(f"  Extraído: {txt} ({len(content):,} bytes)")
            return content
    print("  Formato: TXT direto")
    return raw_bytes


def _try_download(attempt):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox","--disable-setuid-sandbox",
                  "--disable-dev-shm-usage","--disable-gpu",
                  "--no-first-run","--no-zygote","--single-process"]
        )
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            locale="pt-BR",
            timezone_id="America/Sao_Paulo",
            viewport={"width": 1280, "height": 800},
        )
        context.set_default_timeout(PAGE_TIMEOUT)
        page = context.new_page()

        # Bloqueia recursos pesados para carregar mais rápido
        page.route("**/*.{png,jpg,jpeg,gif,svg,ico,woff,woff2,ttf}",
                   lambda r: r.abort())
        page.route("**/{analytics,gtm,googletagmanager,facebook,hotjar}**",
                   lambda r: r.abort())

        print(f"  Navegando para a página B3...")
        page.goto(B3_PAGE_URL, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
        time.sleep(5)

        # Encontra o link de download
        download_url = None

        # Método 1 — texto do link
        try:
            link = page.locator(f"a:has-text('{LINK_TEXT}')").first
            if link.count() > 0:
                href = link.get_attribute("href", timeout=5000)
                if href:
                    download_url = href if href.startswith("http") else f"https://www.b3.com.br{href}"
                    print(f"  Link (método 1): {download_url}")
        except Exception as e:
            print(f"  Método 1 falhou: {e}")

        # Método 2 — regex no HTML
        if not download_url:
            try:
                html = page.content()
                matches = re.findall(
                    r'href=["\']([^"\']*fileDownload\.jsp\?fileId=[^"\']+)["\']', html
                )
                if matches:
                    href = matches[0]
                    download_url = href if href.startswith("http") else f"https://www.b3.com.br{href}"
                    print(f"  Link (método 2): {download_url}")
            except Exception as e:
                print(f"  Método 2 falhou: {e}")

        # Método 3 — URL direta conhecida
        if not download_url:
            download_url = B3_DIRECT_URL
            print(f"  Link (fallback): {download_url}")

        cookies = context.cookies()
        browser.close()

    # Download com os cookies do browser
    import requests
    cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
    headers = {
        "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept":          "application/zip, application/octet-stream, text/plain, */*",
        "Accept-Language": "pt-BR,pt;q=0.9",
        "Referer":         B3_PAGE_URL,
        "Cookie":          cookie_str,
    }

    print(f"  Baixando...")
    r = requests.get(download_url, headers=headers, timeout=120, stream=True)

    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code} ao baixar o arquivo")

    raw = b""
    for chunk in r.iter_content(65536):
        raw += chunk

    print(f"  Baixado: {len(raw):,} bytes  Content-Type: {r.headers.get('Content-Type','?')}")

    # Descompacta se for ZIP
    content = extract_if_zip(raw)

    # Salva o TXT
    TEMP_FILE.write_bytes(content)

    # Valida — deve começar com "01|"
    first_line = content[:20].decode("latin-1", errors="replace")
    if not first_line.startswith("01|"):
        raise RuntimeError(f"Arquivo inválido — início: {repr(first_line[:40])}")

    size_mb = len(content) / 1024 / 1024
    print(f"  Arquivo OK: {size_mb:.1f} MB")
    return str(TEMP_FILE)


def download_with_retry():
    for attempt in range(1, MAX_RETRIES + 1):
        print(f"  Tentativa {attempt}/{MAX_RETRIES}...")
        try:
            return _try_download(attempt)
        except Exception as e:
            print(f"  Erro: {e}")
            if attempt < MAX_RETRIES:
                wait = 15 * attempt
                print(f"  Aguardando {wait}s...")
                time.sleep(wait)
            else:
                raise RuntimeError(f"Falha após {MAX_RETRIES} tentativas: {e}")


def parse_date_from_file(filepath):
    with open(filepath, encoding="latin-1") as f:
        first = f.readline()
    parts = first.strip().split("|")
    return parts[1] if len(parts) > 1 else ""


def is_fresh(date_str):
    try:
        file_date = datetime.strptime(date_str, "%Y%m%d").date()
        today     = datetime.now(BRT).date()
        yesterday = today - timedelta(days=1)
        return file_date in (today, yesterday), file_date
    except Exception:
        return False, None


def parse_options(filepath):
    by_ticker = defaultdict(list)
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

        base_key = ticker_opcao[:4].upper()
        tipo     = "C" if "COMPRA" in tipo_mercado else "P"

        try:    strike = round(float(parts[16].strip()), 2)
        except: strike = 0.0

        venc = (f"{venc_raw[:4]}-{venc_raw[4:6]}-{venc_raw[6:8]}"
                if len(venc_raw) == 8 and venc_raw.isdigit() else venc_raw)

        try:    preco = round(float(parts[18].strip()), 6)
        except: preco = 0.0

        by_ticker[base_key].append({
            "ticker":     ticker_opcao,
            "tipo":       tipo,
            "estilo":     estilo,
            "strike":     strike,
            "vencimento": venc,
            "premio":     preco,
        })

    total = sum(len(v) for v in by_ticker.values())
    print(f"  Tickers: {len(by_ticker)}  Opções: {total:,}")
    return data_date, dict(by_ticker)


def save_jsons(data_date, by_ticker):
    OUTPUT_FOLDER.mkdir(parents=True, exist_ok=True)
    changed = 0

    for ticker, opcoes in sorted(by_ticker.items()):
        opcoes_s = sorted(opcoes, key=lambda x: (x["vencimento"], x["tipo"], x["strike"]))
        payload  = {"ticker": ticker, "data": data_date,
                    "total": len(opcoes_s), "opcoes": opcoes_s}
        content  = json.dumps(payload, ensure_ascii=False,
                               separators=(",", ":")).encode("utf-8")
        out_dir  = OUTPUT_FOLDER / ticker
        out_dir.mkdir(parents=True, exist_ok=True)
        out_file = out_dir / "latest.json"

        if out_file.exists() and out_file.read_bytes() == content:
            continue

        out_file.write_bytes(content)
        changed += 1

    return changed


def main():
    print("=" * 58)
    print("  Opções B3 — Atualização Automática")
    print(f"  {datetime.now(BRT).strftime('%Y-%m-%d %H:%M:%S')} BRT")
    print("=" * 58)
    print()

    print("[1/4] Download...")
    try:
        filepath = download_with_retry()
    except Exception as e:
        print(f"  ERRO: {e}")
        github_output("updated", "false")
        raise SystemExit(1)

    print("\n[2/4] Verificando data...")
    data_date = parse_date_from_file(filepath)
    print(f"  Data arquivo: {data_date}")
    print(f"  Hoje (BRT):   {datetime.now(BRT).date()}")

    valid, _ = is_fresh(data_date)
    if not valid:
        print("  AVISO: data antiga — aguardando próxima tentativa.")
        github_output("updated", "stale")
        github_output("data_date", data_date)
        return

    print("\n[3/4] Convertendo...")
    data_date, by_ticker = parse_options(filepath)

    print("\n[4/4] Salvando JSONs...")
    changed = save_jsons(data_date, by_ticker)
    print(f"  Alterados: {changed}")

    github_output("updated", "true" if changed > 0 else "false")
    github_output("data_date", data_date)
    print("\nConcluído.")


if __name__ == "__main__":
    main()
