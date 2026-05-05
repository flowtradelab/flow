"""
scripts/update_options.py
==========================
1. Abre a página da B3 com Playwright (passa proteção anti-bot)
2. Encontra o link "Lista Completa de Séries Autorizadas"
3. Baixa o arquivo SI_D_SEDE.txt
4. Verifica se a data é de hoje (evita reprocessar arquivo antigo)
5. Converte para JSONs por ticker em grid-options/{TICKER}/latest.json
6. Sinaliza ao GitHub Actions se deve fazer commit

Pode ser rodado localmente também:
  pip install playwright requests
  playwright install chromium
  python scripts/update_options.py
"""

import os
import re
import json
import time
import hashlib
from datetime import datetime, timezone, timedelta
from collections import defaultdict
from pathlib import Path

# ── Constantes ────────────────────────────────────────────────────────────────
B3_PAGE_URL   = "https://www.b3.com.br/pt_br/market-data-e-indices/servicos-de-dados/market-data/consultas/mercado-a-vista/opcoes/series-autorizadas/"
LINK_TEXT     = "Lista Completa de Séries Autorizadas"
OUTPUT_FOLDER = Path("grid-options")
TEMP_FILE     = Path("/tmp/SI_D_SEDE.txt")

# Fuso horário Brasil (BRT = UTC-3)
BRT = timezone(timedelta(hours=-3))


def github_output(key, value):
    """Escreve output para GitHub Actions."""
    gh_output = os.environ.get("GITHUB_OUTPUT", "")
    if gh_output:
        with open(gh_output, "a") as f:
            f.write(f"{key}={value}\n")
    print(f"[output] {key}={value}")


def download_with_playwright():
    """Baixa o arquivo usando Playwright para passar anti-bot da B3."""
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

    print(f"Abrindo browser → {B3_PAGE_URL}")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox"]
        )
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            locale="pt-BR",
            timezone_id="America/Sao_Paulo",
        )
        page = context.new_page()

        # Navega para a página
        print("  Carregando página...")
        page.goto(B3_PAGE_URL, wait_until="domcontentloaded", timeout=30000)
        time.sleep(3)  # aguarda JS renderizar

        # Tenta encontrar o link pelo texto
        download_url = None
        try:
            # Busca link pelo texto exato
            link = page.locator(f"a:has-text('{LINK_TEXT}')").first
            href = link.get_attribute("href")
            if href:
                download_url = href if href.startswith("http") else f"https://www.b3.com.br{href}"
                print(f"  Link encontrado: {download_url}")
        except Exception:
            pass

        # Fallback: busca por fileDownload no HTML
        if not download_url:
            html = page.content()
            matches = re.findall(r'href=["\']([^"\']*fileDownload\.jsp[^"\']*)["\']', html)
            if matches:
                href = matches[0]
                download_url = href if href.startswith("http") else f"https://www.b3.com.br{href}"
                print(f"  Link via regex: {download_url}")

        if not download_url:
            raise RuntimeError("Link de download não encontrado na página da B3.")

        # Baixa o arquivo via requests aproveitando cookies do browser
        cookies = context.cookies()
        cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in cookies)

        browser.close()

    # Faz o download com os cookies do browser
    import requests
    headers = {
        "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "pt-BR,pt;q=0.9",
        "Referer":         B3_PAGE_URL,
        "Cookie":          cookie_str,
    }
    print(f"  Baixando arquivo...")
    r = requests.get(download_url, headers=headers, timeout=60, stream=True)
    r.raise_for_status()

    with open(TEMP_FILE, "wb") as f:
        for chunk in r.iter_content(chunk_size=65536):
            f.write(chunk)

    size_mb = TEMP_FILE.stat().st_size / 1024 / 1024
    print(f"  Arquivo salvo: {TEMP_FILE} ({size_mb:.1f} MB)")
    return str(TEMP_FILE)


def parse_date_from_file(filepath):
    """Lê a data do arquivo (linha 01 do pipe-delimited)."""
    with open(filepath, encoding="latin-1") as f:
        first = f.readline()
    parts = first.strip().split("|")
    return parts[1] if len(parts) > 1 else ""


def is_today_or_yesterday(date_str):
    """Verifica se a data do arquivo é de hoje ou ontem (BRT)."""
    try:
        file_date = datetime.strptime(date_str, "%Y%m%d").date()
        today     = datetime.now(BRT).date()
        yesterday = today - timedelta(days=1)
        return file_date in (today, yesterday), file_date
    except Exception:
        return False, None


def parse_options(filepath):
    """Converte o arquivo pipe-delimited em dict {ticker: [opcoes]}."""
    by_ticker  = defaultdict(list)
    data_date  = ""
    linha_erro = 0

    with open(filepath, encoding="latin-1") as f:
        lines = f.readlines()

    if lines:
        p0 = lines[0].strip().split("|")
        data_date = p0[1] if len(p0) > 1 else ""

    for line in lines[1:]:
        parts = line.strip().split("|")
        if len(parts) < 19 or parts[0] != "02":
            linha_erro += 1
            continue

        tipo_mercado = parts[3].strip()
        ticker_opcao = parts[13].strip()
        estilo       = parts[15].strip()
        venc_raw     = parts[17].strip()

        if not ticker_opcao or len(ticker_opcao) < 4:
            linha_erro += 1
            continue

        base_key = ticker_opcao[:4].upper()

        tipo = "C" if "COMPRA" in tipo_mercado else "P"

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
    print(f"  Tickers: {len(by_ticker)}  Opções: {total:,}  Erros: {linha_erro}")
    return data_date, dict(by_ticker)


def save_jsons(data_date, by_ticker):
    """Salva JSONs em grid-options/{TICKER}/latest.json. Retorna quantos mudaram."""
    OUTPUT_FOLDER.mkdir(parents=True, exist_ok=True)
    changed = 0

    for ticker, opcoes in sorted(by_ticker.items()):
        opcoes_s = sorted(opcoes, key=lambda x: (x["vencimento"], x["tipo"], x["strike"]))
        payload  = {
            "ticker":  ticker,
            "data":    data_date,
            "total":   len(opcoes_s),
            "opcoes":  opcoes_s,
        }
        content  = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        out_dir  = OUTPUT_FOLDER / ticker
        out_dir.mkdir(parents=True, exist_ok=True)
        out_file = out_dir / "latest.json"

        # Só escreve se mudou
        if out_file.exists():
            existing = out_file.read_bytes()
            if existing == content:
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

    # 1. Download
    print("[1/4] Download do arquivo B3...")
    try:
        filepath = download_with_playwright()
    except Exception as e:
        print(f"  ERRO no download: {e}")
        github_output("updated", "false")
        raise

    # 2. Verifica data
    print("\n[2/4] Verificando data do arquivo...")
    data_date = parse_date_from_file(filepath)
    print(f"  Data no arquivo: {data_date}")

    valid, file_date = is_today_or_yesterday(data_date)
    today = datetime.now(BRT).date()
    print(f"  Hoje (BRT):      {today}")

    if not valid:
        print(f"  AVISO: Arquivo com data antiga ({data_date}) — possível atraso da B3.")
        github_output("updated", "stale")
        github_output("data_date", data_date)
        return

    print(f"  Data OK — processando.")

    # 3. Converte
    print("\n[3/4] Convertendo opções por ticker...")
    data_date, by_ticker = parse_options(filepath)

    # 4. Salva JSONs
    print("\n[4/4] Salvando JSONs...")
    changed = save_jsons(data_date, by_ticker)
    print(f"  Arquivos alterados: {changed}")

    if changed > 0:
        print(f"\n  Commit necessário — {changed} ticker(s) atualizados.")
        github_output("updated", "true")
    else:
        print(f"\n  Sem mudanças.")
        github_output("updated", "false")

    github_output("data_date", data_date)
    print("\nConcluído.")


if __name__ == "__main__":
    main()
