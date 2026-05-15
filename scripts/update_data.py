"""
Macro Radar — Data Updater
Roda diariamente via GitHub Actions.
Busca dados do World Bank, BCB e BIS, e atualiza os JSONs no repositório.
"""

import json
import os
import time
import requests
from datetime import datetime, date

# ── Configurações ─────────────────────────────────────────────────────────────
DATA_DIR      = os.path.join(os.path.dirname(__file__), "..", "data")
MUNDO_DIR     = os.path.join(DATA_DIR, "macro-mundo")
HISTORIA_DIR  = os.path.join(MUNDO_DIR, "history")
BRASIL_DIR    = os.path.join(DATA_DIR, "macro-brasil")

WB_BASE       = "https://api.worldbank.org/v2"
BCB_BASE      = "https://api.bcb.gov.br/dados/serie/bcdata.sgs"
BIS_BASE      = "https://stats.bis.org/api/v2/data"

CURRENT_YEAR  = date.today().year
FROM_YEAR     = 1995  # Dados a partir de 1995 (evita distorções do Plano Real)

# Agregados regionais do World Bank — excluir do dataset de países
WB_AGGREGATES = {
    "WLD","EUU","ECS","ECA","LCN","LAC","MEA","MNA","SSF","SSA","SAS","EAP","EAS",
    "NAC","OED","HIC","UMC","MIC","LMC","LIC","LDC","HPC","IBD","IBT","IDB","IDX",
    "IDA","INX","PRE","PSS","PST","SST","TEA","TEC","TLA","TMN","TSA","TSS",
    "AFE","AFW","ARB","CEB","CSS","EMU","FCS","OSS","XZN",
}

# Indicadores do World Bank
WB_INDICATORS = {
    "NY.GDP.MKTP.CD":    "pib",
    "FP.CPI.TOTL.ZG":   "inflacao",
    "SL.UEM.TOTL.ZS":   "desemprego",
    "GC.DOD.TOTL.GD.ZS":"divida_pib",
    "BX.KLT.DINV.CD.WD":"ied",
}

# Séries do BCB (SGS)
BCB_SERIES = {
    "11":   "selic_meta",           # Selic Meta
    "433":  "ipca",                 # IPCA % mês
    "13522":"ipca_acum_12m",        # IPCA acumulado 12 meses
    "1":    "cdi",                  # CDI % dia (base 252)
    "3":    "igpm",                 # IGP-M % mês
    "10813":"dolar_ptax",           # Dólar PTAX (venda)
    "13:":  "reservas_internacionais", # Reservas internacionais (US$ milhões)
    "4192": "balanca_comercial",    # Balança comercial semanal (US$ milhões)
    "7326": "resultado_primario",   # Resultado primário (R$ milhões)
    "24369":"divida_bruta_pib",     # Dívida bruta governo geral % PIB
    "28":   "cambio_eur",           # Euro PTAX (venda)
}

# Séries BCB separadas (histórico longo)
BCB_HISTORICO = {
    "11":   "selic_meta",
    "433":  "ipca",
    "10813":"dolar_ptax",
    "13522":"ipca_acum_12m",
}

# BIS — taxa de juros dos bancos centrais (mensal)
BIS_COUNTRIES = [
    "AR","AU","BR","CA","CH","CL","CN","CO","CZ","DK","EA","GB","HK","HU","ID",
    "IL","IN","IS","JP","KR","KW","MA","MK","MX","MY","NO","NZ","PE","PH","PL",
    "RO","RS","RU","SA","SE","SG","TH","TR","US","ZA",
]

# ── Helpers ───────────────────────────────────────────────────────────────────
def load_json(path: str) -> dict:
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_json(path: str, data) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    print(f"  ✓ Salvo: {os.path.relpath(path)}")

def wb_get(url: str, retries=3) -> dict:
    for i in range(retries):
        try:
            r = requests.get(url, timeout=30)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            if i < retries - 1:
                time.sleep(2 ** i)
            else:
                print(f"  ✗ Erro: {url} — {e}")
                return {}

def bcb_get(serie: str, inicio: str = "01/01/1995") -> list:
    """Busca série do BCB em chunks de 10 anos (limite imposto desde mar/2025)."""
    from datetime import datetime, timedelta

    headers = {"Accept": "application/json", "User-Agent": "Mozilla/5.0"}
    url     = f"{BCB_BASE}.{serie}/dados"
    todos   = []

    # Converte strings de data
    fmt = "%d/%m/%Y"
    dt_inicio = datetime.strptime(inicio, fmt)
    dt_fim    = date.today()

    # Divide em janelas de 9 anos (margem de segurança abaixo de 10)
    chunk_anos = 9
    dt_atual = dt_inicio
    while dt_atual.date() <= dt_fim:
        dt_chunk_fim = date(dt_atual.year + chunk_anos, dt_atual.month, dt_atual.day)
        if dt_chunk_fim > dt_fim:
            dt_chunk_fim = dt_fim

        params = {
            "formato":      "json",
            "dataInicial":  dt_atual.strftime(fmt),
            "dataFinal":    dt_chunk_fim.strftime(fmt),
        }
        try:
            r = requests.get(url, params=params, headers=headers, timeout=30)
            r.raise_for_status()
            dados = r.json()
            if isinstance(dados, list):
                todos.extend(dados)
        except Exception as e:
            print(f"  ✗ BCB série {serie} ({params['dataInicial']}–{params['dataFinal']}): {e}")

        dt_atual = datetime(dt_chunk_fim.year, dt_chunk_fim.month, dt_chunk_fim.day) + timedelta(days=1)
        time.sleep(0.5)

    return todos

# ── 1. World Bank — Histórico por indicador ───────────────────────────────────
def update_wb_history():
    print("\n📊 World Bank — histórico indicadores...")
    for wb_id, nome in WB_INDICATORS.items():
        path = os.path.join(HISTORIA_DIR, f"{wb_id}.json")
        historico = load_json(path)  # { iso3: { "2020": 1234.5, ... } }

        # Busca com paginação — WB limita 1000 por página
        rows = []
        page = 1
        while True:
            url = (f"{WB_BASE}/country/all/indicator/{wb_id}"
                   f"?format=json&date={FROM_YEAR}:{CURRENT_YEAR}&per_page=1000&page={page}")
            data = wb_get(url)
            if not isinstance(data, list) or len(data) < 2:
                break
            batch = data[1] or []
            rows.extend(batch)
            meta = data[0]
            if page >= int(meta.get("pages", 1)):
                break
            page += 1
            time.sleep(0.3)

        novos = 0
        for row in rows:
            iso3 = row.get("countryiso3code", "")
            if not iso3 or iso3 in WB_AGGREGATES:
                continue
            if row.get("value") is None:
                continue
            ano  = str(row["date"])
            pais = historico.setdefault(iso3, {})
            if ano not in pais:
                pais[ano] = row["value"]
                novos += 1

        save_json(path, historico)
        print(f"    {nome}: {novos} novos pontos")
        time.sleep(0.5)

# ── 2. World Bank — Snapshot atual ────────────────────────────────────────────
def update_wb_latest():
    print("\n🌍 World Bank — snapshot atual...")
    latest = {}

    for wb_id, nome in WB_INDICATORS.items():
        url  = (f"{WB_BASE}/country/all/indicator/{wb_id}"
                f"?format=json&mrv=1&per_page=300&gapfill=Y")
        data = wb_get(url)
        rows = data[1] if isinstance(data, list) and len(data) > 1 else []

        for row in rows:
            iso3 = row.get("countryiso3code", "")
            if not iso3 or iso3 in WB_AGGREGATES:
                continue
            if row.get("value") is None:
                continue
            entry = latest.setdefault(iso3, {
                "country": row.get("country", {}).get("value", iso3)
            })
            entry[wb_id] = {
                "value": row["value"],
                "year":  row["date"],
            }
        time.sleep(0.5)

    latest["_updated"] = datetime.utcnow().isoformat() + "Z"
    save_json(os.path.join(MUNDO_DIR, "latest.json"), latest)

# ── 3. BIS — Taxa de juros bancos centrais ────────────────────────────────────
def update_bis_rates():
    print("\n🏦 BIS — taxas de juros bancos centrais...")
    path     = os.path.join(HISTORIA_DIR, "CBPOL.json")
    historico = load_json(path)  # { iso2: { "2024-01": 10.5, ... } }

    for iso2 in BIS_COUNTRIES:
        url = f"{BIS_BASE}/BIS,WS_CBPOL,1.0/M.{iso2}?format=jsondata&startPeriod={FROM_YEAR}-01"
        try:
            r = requests.get(url, timeout=30)
            if r.status_code != 200:
                continue
            j = r.json()
            series = (j.get("data", {})
                       .get("dataSets", [{}])[0]
                       .get("series", {}))
            obs_all = next(iter(series.values()), {}).get("observations", {})
            periods = (j.get("data", {})
                        .get("structure", {})
                        .get("dimensions", {})
                        .get("observation", [{}])[0]
                        .get("values", []))
            pais = historico.setdefault(iso2, {})
            novos = 0
            for idx, period in enumerate(periods):
                periodo = period.get("id", "")
                obs     = obs_all.get(str(idx), [None])
                valor   = obs[0] if obs and obs[0] is not None else None
                if valor is not None and periodo not in pais:
                    pais[periodo] = valor
                    novos += 1
            if novos:
                print(f"    {iso2}: {novos} novos pontos")
            time.sleep(0.3)
        except Exception as e:
            print(f"  ✗ BIS {iso2}: {e}")

    # Snapshot atual (último valor de cada país)
    snapshot = {}
    for iso2, periodos in historico.items():
        if not periodos or not isinstance(periodos, dict):
            continue
        if iso2.startswith("_"):
            continue
        periodos_validos = {k: v for k, v in periodos.items() if not k.startswith("_")}
        if not periodos_validos:
            continue
        ultimo_periodo = sorted(periodos_validos.keys())[-1]
        snapshot[iso2] = {
            "value":  periodos_validos[ultimo_periodo],
            "period": ultimo_periodo,
        }

    historico["_updated"] = datetime.utcnow().isoformat() + "Z"
    save_json(path, historico)
    save_json(os.path.join(MUNDO_DIR, "juros_bancos_centrais.json"), {
        "snapshot": snapshot,
        "_updated": datetime.utcnow().isoformat() + "Z",
    })

# ── 4. BCB — Dados Brasil ─────────────────────────────────────────────────────
def update_brasil():
    print("\n🇧🇷 BCB — dados Brasil...")

    # ── Snapshot atual (último valor de cada série) ───────────────────────────
    snapshot = {"_updated": datetime.utcnow().isoformat() + "Z"}

    # Códigos SGS verificados no Portal de Dados Abertos BCB
    series_snapshot = {
        "1178": "selic_meta",           # Selic anualizada base 252 % a.a. (proxy da meta)
        "433":  "ipca",                 # IPCA % mês
        "13522":"ipca_acum_12m",        # IPCA acumulado 12 meses % a.a.
        "4389": "cdi",                  # CDI acumulado no mês anualizado base 252 % a.a.
        "189":  "igpm",                 # IGP-M % mês (FGV)
        "10813":"dolar_ptax",           # Dólar PTAX venda diária (R$) — snapshot
        "13621":"reservas_internacionais", # Reservas internacionais conceito caixa (US$ mi)
        "22704":"balanca_comercial",    # Balança comercial saldo mensal (US$ mi)
        "5793": "resultado_primario",   # Resultado primário governo central (R$ mi)
        "4537": "divida_bruta_pib",     # Dívida bruta governo geral % PIB
        "21619":"cambio_eur",           # Euro PTAX venda (R$)
    }

    for serie_id, nome in series_snapshot.items():
        # Pega só os últimos 5 registros para snapshot
        url = (f"{BCB_BASE}.{serie_id}/dados/ultimos/5"
               f"?formato=json")
        try:
            r = requests.get(url, timeout=20)
            r.raise_for_status()
            dados = r.json()
            if dados:
                ultimo = dados[-1]
                snapshot[nome] = {
                    "value": float(ultimo.get("valor", 0)),
                    "date":  ultimo.get("data", ""),
                }
                print(f"    {nome}: {ultimo.get('valor')} ({ultimo.get('data')})")
        except Exception as e:
            print(f"  ✗ BCB {nome} ({serie_id}): {e}")
        time.sleep(0.3)

    # ── Focus — expectativas de mercado ──────────────────────────────────────
    try:
        focus_url = "https://olinda.bcb.gov.br/olinda/servico/Expectativas/versao/v1/odata/ExpectativaMercadoAnuais"
        focus_params = {
            "$top": "50",
            "$filter": "Indicador eq 'IPCA' or Indicador eq 'Selic' or Indicador eq 'PIB Total' or Indicador eq 'Taxa de câmbio'",
            "$format": "json",
            "$select": "Indicador,Ano,Mediana,Data",
            "$orderby": "Data desc",
        }
        r = requests.get(focus_url, params=focus_params, timeout=20)
        r.raise_for_status()
        focus_raw = r.json().get("value", [])
        focus = {}
        for item in focus_raw:
            ind  = item.get("Indicador", "")
            ano  = item.get("Ano", "")
            med  = item.get("Mediana")
            data = item.get("Data", "")
            key  = f"{ind}_{ano}"
            if key not in focus or data > focus[key].get("data", ""):
                focus[key] = {"indicador": ind, "ano": ano, "mediana": med, "data": data}
        snapshot["focus"] = list(focus.values())
        print(f"    focus: {len(snapshot['focus'])} expectativas")
    except Exception as e:
        print(f"  ✗ Focus: {e}")

    save_json(os.path.join(BRASIL_DIR, "latest.json"), snapshot)

    # ── Histórico longo (30 anos) ─────────────────────────────────────────────
    path_hist = os.path.join(BRASIL_DIR, "history.json")
    historico = load_json(path_hist)

    series_hist = {
        "432":  "selic_meta",           # Meta Selic definida pelo Copom % a.a.
        "433":  "ipca",                 # IPCA % mês
        "13522":"ipca_acum_12m",        # IPCA acumulado 12 meses
        "3698": "dolar_ptax",           # Dólar PTAX venda média mensal (R$)
        "189":  "igpm",                 # IGP-M % mês
        "4389": "cdi",                  # CDI acumulado mês anualizado base 252 % a.a.
    }

    for serie_id, nome in series_hist.items():
        dados = bcb_get(serie_id, inicio="01/01/1995")
        serie = historico.setdefault(nome, {})
        novos = 0
        for d in dados:
            chave = d.get("data", "")
            valor = d.get("valor")
            if chave and valor is not None and chave not in serie:
                try:
                    serie[chave] = float(valor)
                    novos += 1
                except ValueError:
                    pass
        print(f"    hist {nome}: {novos} novos pontos")
        time.sleep(0.5)

    historico["_updated"] = datetime.utcnow().isoformat() + "Z"
    save_json(path_hist, historico)


# ── Main ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print(f"🚀 Macro Radar — atualização {datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC")
    print("=" * 60)

    update_brasil()
    update_wb_latest()
    update_wb_history()
    update_bis_rates()

    print("\n✅ Concluído!")
