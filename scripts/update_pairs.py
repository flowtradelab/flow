"""
update_pairs.py
───────────────────────────────────────────────────────────────────────────────
Baixa 2 anos de dados diários da B3 via Yahoo Finance,
calcula correlação, beta, half-life e cointegração para todos os pares,
e salva os resultados em pair-trading/pairs.json

Uso:
    pip install yfinance pandas numpy scipy statsmodels
    python update_pairs.py

GitHub Actions: rode este script toda segunda-feira às 07:00 BRT
───────────────────────────────────────────────────────────────────────────────
"""

import json
import os
import itertools
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import yfinance as yf
from scipy import stats
from statsmodels.tsa.stattools import coint

# ── Configurações ─────────────────────────────────────────────────────────────

OUTPUT_FILE   = "pair-trading/pairs_{period}.json"  # {period} substituído no loop
# Períodos disponíveis — cada um gera um arquivo separado no GitHub
PERIODS = {
    "3y":  1095,   # 3 anos
    "2y":  730,    # 2 anos (padrão)
    "1y":  365,    # 1 ano
    "6m":  182,    # 6 meses
    "3m":  91,     # 3 meses
    "2m":  60,     # 2 meses
}
LOOKBACK_DAYS = 730          # fallback — sobrescrito pelo loop
MIN_CORR      = 0.60         # Correlação mínima para incluir o par
MIN_OBS       = 200          # Mínimo de observações válidas
MAX_HALF_LIFE = 60           # Half-life máximo em dias úteis (~3 meses)
MAX_PAIRS     = 300          # Limite de pares no JSON final

# ── Tickers da B3 ─────────────────────────────────────────────────────────────
# ~100 principais ações — adicione mais conforme necessário

# ── Composição do Ibovespa (carteira vigente) ────────────────────────────────
# Fonte: B3 — ~90 ações mais líquidas do mercado brasileiro
# Atualizar quando a B3 revisar a carteira (jan/mai/set)
TICKERS_B3 = [
    # Financeiro & Seguros
    "ITUB4", "BBDC4", "BBAS3", "SANB11", "BPAC11", "BRSR6",
    "BBSE3", "PSSA3", "CXSE3", "IRBR3",

    # Petróleo, Gás & Petroquímica
    "PETR3", "PETR4", "PRIO3", "RECV3", "CSAN3", "UGPA3", "RRRP3",

    # Mineração & Siderurgia
    "VALE3", "CSNA3", "USIM5", "GOAU4", "GGBR4",

    # Energia Elétrica
    "CMIG4", "CPFE3", "ENGI11", "EGIE3", "TAEE11",
    "SBSP3", "AURE3", "ENEV3", "ISAE4", "ELET3",

    # Varejo & Consumo
    "MGLU3", "LREN3", "AMER3", "CEAB3", "SOMA3",
    "NTCO3", "PETZ3", "VIVA3",

    # Alimentos & Bebidas
    "ABEV3", "JBSS3", "BRFS3", "BEEF3", "SMTO3", "MDIA3",

    # Saúde & Farma
    "HAPV3", "RDOR3", "FLRY3", "RADL3", "DASA3", "ODPV3",

    # Construção & Imóveis
    "CYRE3", "MRVE3", "EVEN3", "EZTC3", "DIRR3",
    "CVCB3", "LAVV3",

    # Logística & Transporte
    "RAIL3", "CCRO3", "AZUL4", "GOLL4",

    # Papel & Celulose
    "SUZB3", "KLBN11", "DXCO3",

    # Telecomunicações & Tech
    "VIVT3", "TIMS3", "TOTVS3",

    # Agro & Insumos
    "AGRO3", "SLCE3", "SMAG3",

    # Indústria & Outros
    "WEGE3", "RENT3", "MOVI3", "VAMO3",
    "RAIZ4", "HYPE3", "BRAP4", "EMBR3",
    "MULT3", "IGTI11", "PRIO3",
]

# ── Helpers ───────────────────────────────────────────────────────────────────

def calc_half_life(spread: pd.Series) -> float:
    """Calcula o half-life do processo de Ornstein-Uhlenbeck."""
    spread = spread.dropna()
    if len(spread) < 20:
        return 9999.0
    lag = spread.shift(1).dropna()
    delta = spread.iloc[1:] - lag
    slope, _, _, _, _ = stats.linregress(lag, delta)
    if slope >= 0:
        return 9999.0
    return round(-np.log(2) / slope, 1)


def calc_beta(s1: pd.Series, s2: pd.Series) -> float:
    """Hedge ratio via OLS."""
    s1, s2 = s1.dropna(), s2.dropna()
    idx = s1.index.intersection(s2.index)
    if len(idx) < 20:
        return 1.0
    slope, _, _, _, _ = stats.linregress(s2.loc[idx], s1.loc[idx])
    return round(slope, 4)


def calc_zscore(spread: pd.Series, window: int = 60) -> float:
    """Z-Score atual do spread."""
    if len(spread) < window:
        return 0.0
    recent = spread.iloc[-window:]
    mean, std = recent.mean(), recent.std()
    if std == 0:
        return 0.0
    return round((spread.iloc[-1] - mean) / std, 3)


def get_sector(ticker: str) -> str:
    """Retorna setor baseado em grupos conhecidos."""
    groups = {
        "Financeiro":   ["ITUB4","BBDC4","BBAS3","SANB11","BPAC11","BBSE3","PSSA3","CXSE3","IRBR3","BRSR6"],
        "Petróleo":     ["PETR3","PETR4","PRIO3","RECV3","RRRP3","CSAN3","UGPA3"],
        "Mineração":    ["VALE3","CSNA3","USIM5","GOAU4","GGBR4","GGBR3","BRAP4"],
        "Energia":      ["ELET3","CMIG4","CMIG3","CPFE3","ENGI11","EGIE3","TAEE11","SBSP3","AURE3","ENEV3","ISAE4"],
        "Varejo":       ["MGLU3","AMER3","LREN3","SOMA3","CEAB3","NTCO3","PETZ3","VIVA3","AMAR3"],
        "Alimentos":    ["ABEV3","BRFS3","JBSS3","BEEF3","MDIA3","SLCE3","SMTO3"],
        "Saúde":        ["HAPV3","RDOR3","FLRY3","DASA3","RADL3","ODPV3"],
        "Construção":   ["CYRE3","MRVE3","EVEN3","EZTC3","DIRR3","CVCB3","LAVV3"],
        "Logística":    ["RAIL3","CCRO3","AZUL4","GOLL4","EMBR3"],
        "Papel":        ["SUZB3","KLBN11","DXCO3"],
        "Telecom":      ["VIVT3","TIMS3","TOTVS3"],
        "Agro":         ["AGRO3","SLCE3","SMAG3"],
        "Indústria":    ["WEGE3","RENT3","MOVI3","VAMO3","RAIZ4","HYPE3","MULT3"],
    }
    for sector, tickers in groups.items():
        if ticker in tickers:
            return sector
    return "Outros"

# ── Download de dados ──────────────────────────────────────────────────────────

def download_prices(tickers: list, days: int = 730) -> pd.DataFrame:
    """Baixa preços de fechamento ajustados para todos os tickers."""
    end   = datetime.today()
    start = end - timedelta(days=days)

    print(f"Baixando {len(tickers)} tickers do Yahoo Finance...")
    print(f"Período: {start.strftime('%Y-%m-%d')} → {end.strftime('%Y-%m-%d')}\n")

    yf_tickers = [f"{t}.SA" for t in tickers]

    try:
        raw = yf.download(
            yf_tickers,
            start=start.strftime("%Y-%m-%d"),
            end=end.strftime("%Y-%m-%d"),
            auto_adjust=True,
            progress=True,
            threads=True,
        )
        prices = raw["Close"].copy()
    except Exception as e:
        print(f"Erro no download em lote: {e}")
        print("Tentando download individual...")
        prices = pd.DataFrame()
        for t, yt in zip(tickers, yf_tickers):
            try:
                d = yf.download(yt, start=start.strftime("%Y-%m-%d"),
                                end=end.strftime("%Y-%m-%d"),
                                auto_adjust=True, progress=False)
                if not d.empty:
                    prices[t] = d["Close"]
            except Exception as ex:
                print(f"  ✗ {t}: {ex}")

    # Renomeia colunas removendo sufixo .SA
    prices.columns = [c.replace(".SA", "") if hasattr(c, "replace") else c for c in prices.columns]
    # Normaliza índice — remove timezone para evitar problemas de alinhamento
    if hasattr(prices.index, "tz") and prices.index.tz is not None:
        prices.index = prices.index.tz_localize(None)

    # Remove tickers com dados insuficientes
    valid = [c for c in prices.columns if prices[c].count() >= MIN_OBS]
    print(f"\n✓ {len(valid)}/{len(tickers)} tickers com dados suficientes (≥{MIN_OBS} obs)")

    return prices[valid].ffill().dropna(how="all")


# ── Cálculo de pares ──────────────────────────────────────────────────────────

def calculate_pairs(prices: pd.DataFrame) -> list:
    """Calcula todos os pares válidos com correlação, beta, half-life e cointegração."""
    tickers = list(prices.columns)
    returns = prices.pct_change().dropna()

    print(f"\nCalculando pares para {len(tickers)} tickers...")
    print(f"Total de combinações: {len(tickers)*(len(tickers)-1)//2}\n")

    pairs     = []
    total     = 0
    valid     = 0
    cointegrated = 0

    for a, b in itertools.combinations(tickers, 2):
        total += 1

        if total % 500 == 0:
            print(f"  {total} pares processados... ({valid} válidos, {cointegrated} cointegrados)")

        # Alinha séries
        idx = prices[a].dropna().index.intersection(prices[b].dropna().index)
        if len(idx) < MIN_OBS:
            continue

        pa = prices[a].loc[idx]
        pb = prices[b].loc[idx]
        # Alinha preços e calcula retornos sobre a interseção
        pa = pa.loc[idx]
        pb = pb.loc[idx]
        ra = pa.pct_change().dropna()
        rb = pb.pct_change().dropna()
        # Interseção final dos retornos
        ret_idx = ra.index.intersection(rb.index)
        ra = ra.loc[ret_idx]
        rb = rb.loc[ret_idx]
        pa = pa.reindex(ret_idx).ffill()
        pb = pb.reindex(ret_idx).ffill()

        # Correlação de Pearson
        if len(ra) < MIN_OBS or len(rb) < MIN_OBS:
            continue
        corr, _ = stats.pearsonr(ra, rb)
        if abs(corr) < MIN_CORR:
            continue

        valid += 1

        # Beta (hedge ratio)
        beta = calc_beta(pa, pb)

        # Spread
        spread = pa - beta * pb

        # Half-life
        hl = calc_half_life(spread)
        if hl > MAX_HALF_LIFE:
            continue

        # Z-Score atual
        zscore = calc_zscore(spread)

        # Teste de cointegração (Engle-Granger)
        try:
            _, pvalue, _ = coint(pa, pb)
            is_coint = pvalue < 0.05
        except Exception:
            is_coint = False

        if is_coint:
            cointegrated += 1

        # Estatísticas do spread
        spread_vals  = spread.values
        spread_mean  = round(float(np.mean(spread_vals)), 4)
        spread_std   = round(float(np.std(spread_vals)), 4)

        # Preço atual e variação
        price_a = round(float(pa.iloc[-1]), 2)
        price_b = round(float(pb.iloc[-1]), 2)

        pairs.append({
            "a":          a,
            "b":          b,
            "corr":       round(corr, 4),
            "beta":       beta,
            "halfLife":   hl,
            "zscore":     zscore,
            "coint":      is_coint,
            "pricA":      price_a,
            "pricB":      price_b,
            "spreadMean": spread_mean,
            "spreadStd":  spread_std,
            "sectorA":    get_sector(a),
            "sectorB":    get_sector(b),
            "sameSector": get_sector(a) == get_sector(b),
            "obs":        len(idx),
        })

    print(f"\n✓ Total processado: {total} combinações")
    print(f"✓ Correlação ≥ {MIN_CORR}: {valid} pares")
    print(f"✓ Half-life ≤ {MAX_HALF_LIFE}d: {len(pairs)} pares")
    print(f"✓ Cointegrados (p<0.05): {cointegrated} pares")

    # Ordena por: cointegrado > mesmo setor > correlação
    pairs.sort(key=lambda p: (
        not p["coint"],
        not p["sameSector"],
        -abs(p["corr"])
    ))

    return pairs[:MAX_PAIRS]


# ── Histórico de preços por par ────────────────────────────────────────────────

def build_spread_history(prices: pd.DataFrame, pairs: list, n_points: int = 120) -> dict:
    """
    Gera histórico do spread (últimos n_points dias úteis) para cada par.
    Usado pelo gráfico SpreadChart no LongShort.jsx
    """
    history = {}
    for p in pairs:
        a, b, beta = p["a"], p["b"], p["beta"]
        if a not in prices.columns or b not in prices.columns:
            continue
        idx = prices[a].dropna().index.intersection(prices[b].dropna().index)
        if len(idx) < n_points:
            continue
        pa = prices[a].loc[idx].iloc[-n_points:]
        pb = prices[b].loc[idx].iloc[-n_points:]
        spread = pa - beta * pb
        mean   = spread.mean()
        std    = spread.std() or 1

        history[f"{a}_{b}"] = [
            {
                "date":   str(d.date()),
                "spread": round(float(s), 4),
                "z":      round(float((s - mean) / std), 3),
                "r1":     round(float(pa.loc[d]), 2),
                "r2":     round(float(pb.loc[d]), 2),
            }
            for d, s in zip(pa.index, spread)
        ]

    return history


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("  PNT Trade Lab — Pair Trading Data Updater")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)
    print()

    # 1. Download
    prices = download_prices(TICKERS_B3, days=LOOKBACK_DAYS)  # usa global LOOKBACK_DAYS
    if prices.empty:
        print("ERRO: Nenhum dado baixado. Verifique a conexão.")
        return

    # 2. Calcula pares
    pairs = calculate_pairs(prices)
    if not pairs:
        print("ERRO: Nenhum par encontrado com os critérios definidos.")
        return

    # 3. Histórico de spread
    print("\nGerando histórico de spread para gráficos...")
    history = build_spread_history(prices, pairs[:50])  # top 50 com histórico
    print(f"✓ Histórico gerado para {len(history)} pares")

    # 4. Snapshot dos preços atuais (para WATCHLIST_BASE do app)
    print("\nGerando snapshot de preços...")
    snapshot = {}
    for ticker in prices.columns:
        try:
            close_vals = prices[ticker].dropna()
            if len(close_vals) >= 2:
                price     = float(close_vals.iloc[-1])
                prev      = float(close_vals.iloc[-2])
                chg_pct   = (price - prev) / prev * 100
                snapshot[ticker] = {
                    "price":     round(price, 2),
                    "close":     round(prev, 2),
                    "changePct": round(chg_pct, 2),
                }
        except Exception:
            pass

    # 5. Monta JSON final
    output = {
        "updated":    datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "period":     f"{LOOKBACK_DAYS}d",
        "tickers":    len(prices.columns),
        "pairs":      pairs,
        "history":    history,
        "prices":     snapshot,
        "meta": {
            "minCorr":     MIN_CORR,
            "maxHalfLife": MAX_HALF_LIFE,
            "minObs":      MIN_OBS,
        }
    }

    # 6. Salva arquivo
    # Converte tipos numpy para tipos Python nativos (JSON serializável)
    def convert(obj):
        if isinstance(obj, (np.integer,)):   return int(obj)
        if isinstance(obj, (np.floating,)):  return float(obj)
        if isinstance(obj, (np.bool_,)):     return bool(obj)
        if isinstance(obj, (np.ndarray,)):   return obj.tolist()
        raise TypeError(f"Não serializável: {type(obj)}")

    os.makedirs(os.path.dirname(OUTPUT_FILE), exist_ok=True)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, separators=(",", ":"), default=convert)

    size_kb = os.path.getsize(OUTPUT_FILE) / 1024
    print(f"\n✅ Salvo em {OUTPUT_FILE} ({size_kb:.0f} KB)")
    print(f"   {len(pairs)} pares | {len(history)} com histórico | {len(snapshot)} preços")
    print(f"\nPróximo passo: git add {OUTPUT_FILE} && git commit -m 'chore: update pairs' && git push")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--period", "-p",
        choices=list(PERIODS.keys()),
        default=None,
        help="Período a calcular. Se omitido, calcula todos."
    )
    args = parser.parse_args()

    periods_to_run = [args.period] if args.period else list(PERIODS.keys())

    for period in periods_to_run:
        print(f"\n{'='*60}")
        print(f"  Calculando período: {period} ({PERIODS[period]} dias)")
        print(f"{'='*60}\n")
        LOOKBACK_DAYS = PERIODS[period]
        OUTPUT_FILE   = f"pair-trading/pairs_{period}.json"
        main()
