"""
update_quant.py
────────────────────────────────────────────────────────────────────────────────
Baixa dados OHLC horários e diários de todos os ativos da B3 via Yahoo Finance,
calcula análises quantitativas e salva em quant/{TICKER}/analysis.json

Estrutura gerada:
  quant/
    _index.json              → lista de ativos disponíveis + metadata
    PETR4/
      analysis.json          → gap analysis + sazonalidade + mean reversion + momentum
      ohlc_1h.json           → últimos 60 dias de candles horários (para gráficos)
    VALE3/
      analysis.json
      ohlc_1h.json
    ...

Uso:
    pip install yfinance pandas numpy scipy
    python update_quant.py
    python update_quant.py --ticker PETR4    # só um ativo
    python update_quant.py --min-volume 5    # volume mínimo em milhões R$

GitHub Actions: rode diariamente às 07:00 BRT
────────────────────────────────────────────────────────────────────────────────
"""

import json, os, time, argparse, warnings
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf
from scipy import stats as scipy_stats

warnings.filterwarnings("ignore")

# ── Configurações ──────────────────────────────────────────────────────────────
OUTPUT_DIR       = Path("quant")
HOURLY_DAYS      = 728        # 2 anos (Yahoo exige < 730 dias, não <=)
DAILY_DAYS       = 1095       # 3 anos para dados diários
MIN_HOURLY_OBS   = 500        # mínimo de barras horárias válidas
MIN_DAILY_OBS    = 400        # mínimo de barras diárias válidas
MIN_AVG_VOLUME   = 2_000_000  # volume médio diário mínimo (R$)
GAP_THRESHOLDS   = [1.0, 2.0, 3.0, 5.0]  # thresholds de gap para análise
OHLC_CHART_DAYS  = 60         # dias de OHLC horário para gráficos

# ── Lista completa de tickers B3 ───────────────────────────────────────────────
# Todos os principais — o script filtra automaticamente os sem dados
B3_TICKERS = [
    # Ibovespa core
    "ABEV3","ALOS3","ASAI3","AZUL4","B3SA3","BBAS3","BBDC3","BBDC4",
    "BBSE3","BEEF3","BPAC11","BRAP4","BRFS3","BRIO3","BRSR6","CAML3",
    "CCRO3","CIEL3","CLSA3","CMIG4","CMIN3","COGN3","CPFE3","CPLE6",
    "CRFB3","CSAN3","CSNA3","CVCB3","CYRE3","DEXC3","DIRD3","DIRR3",
    "DXCO3","ECOR3","EGIE3","ELET3","ELET6","EMBR3","ENEV3","ENGI11",
    "EQTL3","EZTC3","FESA4","FLRY3","GGBR4","GOAU4","GOLL4","HAPV3",
    "HYPE3","IGTI11","IRBR3","ISAE4","ITSA4","ITUB4","JBSS3","JHSF3",
    "KLBN11","LAVV3","LREN3","LWSA3","MATD3","MBLY3","MDIA3","MGLU3",
    "MOVI3","MRFG3","MRVE3","MULT3","NTCO3","ODPV3","PCAR3","PETR3",
    "PETR4","PETZ3","PNVL3","POSI3","PRIO3","PSSA3","RADL3","RAIL3",
    "RAIZ4","RDOR3","RECV3","RENT3","RRRP3","SANB11","SAPR11","SBSP3",
    "SEER3","SIMH3","SLCE3","SMFT3","SMTO3","SOMA3","SUZB3","TAEE11",
    "TIMS3","TOTS3","TRPL4","UGPA3","USIM5","VALE3","VAMO3","VIIA3",
    "VIVT3","VIVA3","VULC3","WEGE3","YDUQ3",
    # Extras com liquidez
    "AURE3","BEEF3","BRKM5","CGAS5","CLSC4","COCE5","CPRE3","CRPG6",
    "CSRN6","CURY3","CYRELA","DASA3","ESPA3","EVEN3","FIQE3","FRAS3",
    "GFSA3","GGBR3","GRND3","HBSA3","INEP4","JALL3","KEPL3","LAND3",
    "LIGT3","LIQH3","LOGG3","LOGN3","LPSB3","LVTC3","MELN3","MELK3",
    "MILS3","MNPR3","MTRE3","MXRF11","NEFIN3","NINJ3","OIBR3","OPCT3",
    "PARD3","PMAM3","PORT3","PRIO3","PTBL3","RAIA3","RBEV3","RCSL4",
    "ROMI3","RSID3","SANB4","SCAR3","SEER3","SEQL3","SFXA4","SGPS3",
    "SHOW3","SOJA3","SRNA3","STBP3","SULA11","SYNE3","TASA4","TGMA3",
    "TPIS3","TUPY3","TXRX4","UCAS3","UNIP6","UNIT3","USAL3","VLID3",
    "VVEO3","WEST3","WIZC3","XBXL3","XLCA3","ZAMP3",
]
# Remove duplicatas
B3_TICKERS = list(dict.fromkeys(B3_TICKERS))

# ── Helpers ────────────────────────────────────────────────────────────────────
def ts():
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

def to_json(obj):
    """Converte tipos numpy para Python nativo. Trata Infinity e NaN como null."""
    if isinstance(obj, (np.integer,)):  return int(obj)
    if isinstance(obj, (np.floating,)):
        v = float(obj)
        return None if (np.isnan(v) or np.isinf(v)) else v
    if isinstance(obj, (np.bool_,)):    return bool(obj)
    if isinstance(obj, (np.ndarray,)):  return obj.tolist()
    if isinstance(obj, pd.Timestamp):   return str(obj.date())
    if isinstance(obj, float):
        return None if (np.isnan(obj) or np.isinf(obj)) else obj
    raise TypeError(f"Não serializável: {type(obj)}")

def sanitize(obj):
    """Percorre recursivamente e substitui Infinity/NaN por None."""
    if isinstance(obj, dict):
        return {k: sanitize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [sanitize(v) for v in obj]
    if isinstance(obj, float) and (np.isnan(obj) or np.isinf(obj)):
        return None
    return obj

def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(sanitize(data), f, ensure_ascii=False, separators=(",",":"), default=to_json)

# ── Download ───────────────────────────────────────────────────────────────────
def download_ohlc(ticker: str, interval: str, days: int) -> pd.DataFrame:
    """Baixa dados OHLC do Yahoo Finance."""
    end   = datetime.today()
    start = end - timedelta(days=days)
    yf_ticker = f"{ticker}.SA"
    try:
        df = yf.download(
            yf_ticker, start=start.strftime("%Y-%m-%d"),
            end=end.strftime("%Y-%m-%d"),
            interval=interval, auto_adjust=True, progress=False,
        )
        if df.empty:
            return pd.DataFrame()
        # Normaliza timezone
        if hasattr(df.index, "tz") and df.index.tz is not None:
            df.index = df.index.tz_localize(None)
        # Flatten multi-level columns
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df.columns = [c.lower() for c in df.columns]
        df = df.dropna(subset=["close"])
        return df
    except Exception as e:
        print(f"  ✗ {ticker} ({interval}): {e}")
        return pd.DataFrame()

# ── Análise de Gap ─────────────────────────────────────────────────────────────
def analyze_gaps(daily: pd.DataFrame) -> dict:
    """Calcula estatísticas de gap de abertura."""
    if len(daily) < 50:
        return {}

    daily = daily.copy()
    daily["prev_close"] = daily["close"].shift(1)
    daily["gap_pct"]    = (daily["open"] - daily["prev_close"]) / daily["prev_close"] * 100
    daily["intraday"]   = (daily["close"] - daily["open"]) / daily["open"] * 100
    daily["d1_ret"]     = daily["close"].pct_change(1).shift(-1) * 100
    daily["d5_ret"]     = daily["close"].pct_change(5).shift(-5) * 100
    daily = daily.dropna(subset=["gap_pct", "intraday"])

    results = {}
    for threshold in GAP_THRESHOLDS:
        for direction in ["up", "down", "both"]:
            if direction == "up":
                mask = daily["gap_pct"] >= threshold
            elif direction == "down":
                mask = daily["gap_pct"] <= -threshold
            else:
                mask = daily["gap_pct"].abs() >= threshold

            subset = daily[mask]
            if len(subset) < 5:
                continue

            key = f"{threshold:.0f}pct_{direction}"
            reversals = (subset["intraday"] * (1 if direction == "down" else -1)) > 0
            results[key] = {
                "threshold":      threshold,
                "direction":      direction,
                "count":          len(subset),
                "reversal_rate":  round(float(reversals.mean() * 100), 1),
                "avg_gap":        round(float(subset["gap_pct"].mean()), 2),
                "avg_intraday":   round(float(subset["intraday"].mean()), 2),
                "avg_d1":         round(float(subset["d1_ret"].dropna().mean()), 2) if len(subset["d1_ret"].dropna()) > 3 else None,
                "avg_d5":         round(float(subset["d5_ret"].dropna().mean()), 2) if len(subset["d5_ret"].dropna()) > 3 else None,
                "std_intraday":   round(float(subset["intraday"].std()), 2),
                "win_rate":       round(float((subset["intraday"] > 0).mean() * 100), 1),
            }

    # Top 10 maiores gaps recentes
    recent = daily.nlargest(10, "gap_pct")[["gap_pct","intraday","d1_ret"]].copy()
    recent.index = recent.index.strftime("%Y-%m-%d")
    top_gaps = []
    for date, row in recent.iterrows():
        top_gaps.append({
            "date":     date,
            "gap_pct":  round(float(row["gap_pct"]), 2),
            "intraday": round(float(row["intraday"]), 2) if pd.notna(row["intraday"]) else None,
            "d1_ret":   round(float(row["d1_ret"]), 2) if pd.notna(row["d1_ret"]) else None,
        })

    return {"by_threshold": results, "top_gaps": top_gaps}

# ── Sazonalidade ───────────────────────────────────────────────────────────────
def analyze_seasonality(hourly: pd.DataFrame, daily: pd.DataFrame) -> dict:
    """Calcula retornos médios por hora, dia da semana e mês."""
    result = {}

    # Por hora do dia (dados horários)
    if len(hourly) > 100:
        h = hourly.copy()
        h["ret"] = h["close"].pct_change() * 100
        h["hour"] = h.index.hour
        h["dow"]  = h.index.dayofweek  # 0=seg
        h = h.dropna(subset=["ret"])

        hourly_stats = {}
        for hour in range(10, 18):
            sub = h[h["hour"] == hour]["ret"]
            if len(sub) < 20:
                continue
            hourly_stats[str(hour)] = {
                "mean":     round(float(sub.mean()), 3),
                "std":      round(float(sub.std()), 3),
                "pos_rate": round(float((sub > 0).mean() * 100), 1),
                "count":    len(sub),
            }
        result["by_hour"] = hourly_stats

        # Por dia da semana
        dow_stats = {}
        dow_labels = ["Seg","Ter","Qua","Qui","Sex"]
        for dow in range(5):
            sub = h[h["dow"] == dow]["ret"]
            if len(sub) < 20:
                continue
            dow_stats[dow_labels[dow]] = {
                "mean":     round(float(sub.mean()), 3),
                "std":      round(float(sub.std()), 3),
                "pos_rate": round(float((sub > 0).mean() * 100), 1),
                "count":    len(sub),
            }
        result["by_dow"] = dow_stats

    # Por mês (dados diários)
    if len(daily) > 50:
        d = daily.copy()
        d["ret"]   = d["close"].pct_change() * 100
        d["month"] = d.index.month
        d = d.dropna(subset=["ret"])

        month_labels = ["Jan","Fev","Mar","Abr","Mai","Jun","Jul","Ago","Set","Out","Nov","Dez"]
        month_stats = {}
        for m in range(1, 13):
            sub = d[d["month"] == m]["ret"]
            if len(sub) < 10:
                continue
            month_stats[month_labels[m-1]] = {
                "mean":     round(float(sub.mean()), 3),
                "pos_rate": round(float((sub > 0).mean() * 100), 1),
                "count":    len(sub),
            }
        result["by_month"] = month_stats

    return result

# ── Mean Reversion ─────────────────────────────────────────────────────────────
def analyze_mean_reversion(hourly: pd.DataFrame, daily: pd.DataFrame) -> dict:
    """Calcula half-life, velocidade de reversão e Z-Score."""
    result = {}

    # Half-life intraday (dados horários)
    if len(hourly) > 100:
        prices = hourly["close"].dropna()
        log_p  = np.log(prices)
        lag    = log_p.shift(1).dropna()
        delta  = log_p.diff().dropna()
        idx    = lag.index.intersection(delta.index)
        if len(idx) > 50:
            slope, _, _, _, _ = scipy_stats.linregress(lag.loc[idx], delta.loc[idx])
            if slope < 0:
                hl = round(-np.log(2) / slope, 1)
                result["half_life_hours"] = hl
                result["half_life_days"]  = round(hl / 8, 1)  # ~8h de pregão/dia

    # Z-Score atual (últimas 60 barras diárias)
    if len(daily) > 60:
        window = 60
        close  = daily["close"].dropna()
        mean   = close.iloc[-window:].mean()
        std    = close.iloc[-window:].std()
        if std > 0:
            result["zscore_current"]   = round(float((close.iloc[-1] - mean) / std), 3)
            result["zscore_mean"]      = round(float(mean), 2)
            result["zscore_std"]       = round(float(std), 2)

    # Taxa de reversão após desvios (dados diários)
    if len(daily) > 100:
        d = daily.copy()
        d["ret"]    = d["close"].pct_change() * 100
        d = d.dropna(subset=["ret"])

        reversion_stats = {}
        for pct in [1.0, 2.0, 3.0]:
            up_days   = d[d["ret"] >= pct]
            down_days = d[d["ret"] <= -pct]

            if len(up_days) >= 5:
                next_ret_up = d["ret"].shift(-1).loc[up_days.index].dropna()
                reversion_stats[f"after_up_{pct:.0f}pct"] = {
                    "count":       len(up_days),
                    "next_mean":   round(float(next_ret_up.mean()), 3),
                    "reversal_pct":round(float((next_ret_up < 0).mean() * 100), 1),
                }
            if len(down_days) >= 5:
                next_ret_dn = d["ret"].shift(-1).loc[down_days.index].dropna()
                reversion_stats[f"after_down_{pct:.0f}pct"] = {
                    "count":       len(down_days),
                    "next_mean":   round(float(next_ret_dn.mean()), 3),
                    "reversal_pct":round(float((next_ret_dn > 0).mean() * 100), 1),
                }
        result["reversion_after_move"] = reversion_stats

    return result

# ── Momentum ───────────────────────────────────────────────────────────────────
def analyze_momentum(daily: pd.DataFrame) -> dict:
    """Analisa padrões de momentum."""
    if len(daily) < 50:
        return {}

    d = daily.copy()
    d["ret"] = d["close"].pct_change() * 100
    d = d.dropna(subset=["ret"])

    result = {}

    # Sequências consecutivas
    seq_stats = {}
    for n in [2, 3, 4, 5]:
        up_seq   = (d["ret"] > 0).rolling(n).sum() == n
        down_seq = (d["ret"] < 0).rolling(n).sum() == n

        if up_seq.sum() >= 5:
            next_after_up = d["ret"].shift(-1)[up_seq].dropna()
            seq_stats[f"{n}_up_days"] = {
                "count":        int(up_seq.sum()),
                "next_mean":    round(float(next_after_up.mean()), 3),
                "continues_pct":round(float((next_after_up > 0).mean() * 100), 1),
            }
        if down_seq.sum() >= 5:
            next_after_dn = d["ret"].shift(-1)[down_seq].dropna()
            seq_stats[f"{n}_down_days"] = {
                "count":        int(down_seq.sum()),
                "next_mean":    round(float(next_after_dn.mean()), 3),
                "continues_pct":round(float((next_after_dn < 0).mean() * 100), 1),
            }
    result["consecutive_days"] = seq_stats

    # RSI-like momentum
    returns = d["ret"]
    gains   = returns[returns > 0].mean() if len(returns[returns > 0]) > 0 else 0
    losses  = abs(returns[returns < 0].mean()) if len(returns[returns < 0]) > 0 else 0
    result["avg_gain"]     = round(float(gains), 3)
    result["avg_loss"]     = round(float(losses), 3)
    result["gain_loss_ratio"] = round(float(gains / losses), 3) if losses > 0 else None
    result["pos_day_rate"] = round(float((returns > 0).mean() * 100), 1)

    return result

# ── Volume Profile (dados diários) ────────────────────────────────────────────
def analyze_volume_profile(daily: pd.DataFrame, hourly: pd.DataFrame) -> dict:
    """Calcula distribuição de volume por faixa de preço."""
    result = {}

    if len(daily) > 50 and "volume" in daily.columns:
        d = daily.dropna(subset=["close","volume"])
        # Volume médio por dia da semana
        d = d.copy()
        d["dow"] = d.index.dayofweek
        dow_vol = {}
        dow_labels = ["Seg","Ter","Qua","Qui","Sex"]
        for dow in range(5):
            sub = d[d["dow"] == dow]["volume"]
            if len(sub) > 5:
                dow_vol[dow_labels[dow]] = round(float(sub.mean()), 0)
        result["avg_volume_by_dow"] = dow_vol

        # Estatísticas gerais de volume
        result["avg_daily_volume"]    = round(float(d["volume"].mean()), 0)
        result["avg_daily_volume_brl"]= round(float((d["volume"] * d["close"]).mean()), 0)

    # Volume por hora (dados horários)
    if len(hourly) > 100 and "volume" in hourly.columns:
        h = hourly.copy()
        h = h.dropna(subset=["volume"])
        h["hour"] = h.index.hour
        hourly_vol = {}
        for hour in range(10, 18):
            sub = h[h["hour"] == hour]["volume"]
            if len(sub) > 20:
                hourly_vol[str(hour)] = round(float(sub.mean()), 0)
        result["avg_volume_by_hour"] = hourly_vol

    return result

# ── OHLC para gráficos ────────────────────────────────────────────────────────
def build_ohlc_chart(hourly: pd.DataFrame, days: int = 60) -> list:
    """Retorna últimas N barras horárias para gráficos."""
    if hourly.empty:
        return []
    cutoff = datetime.today() - timedelta(days=days)
    df = hourly[hourly.index >= cutoff].copy()
    result = []
    for ts_idx, row in df.iterrows():
        try:
            result.append({
                "t": str(ts_idx.date()) + "T" + str(ts_idx.time())[:5],
                "o": round(float(row["open"]), 2),
                "h": round(float(row["high"]), 2),
                "l": round(float(row["low"]),  2),
                "c": round(float(row["close"]),2),
                "v": int(row["volume"]) if "volume" in row and pd.notna(row["volume"]) else 0,
            })
        except Exception:
            continue
    return result

# ── Processa um ativo ─────────────────────────────────────────────────────────
def process_ticker(ticker: str, min_volume: float) -> dict | None:
    """Baixa dados e calcula todas as análises para um ativo."""
    print(f"  [{ticker}] Baixando dados...")

    # Download diário
    daily = download_ohlc(ticker, "1d", DAILY_DAYS)
    if len(daily) < MIN_DAILY_OBS:
        print(f"  [{ticker}] ✗ Dados diários insuficientes ({len(daily)} barras)")
        return None

    # Filtro de volume
    if "volume" in daily.columns:
        avg_vol_brl = (daily["volume"] * daily["close"]).mean()
        if avg_vol_brl < min_volume:
            print(f"  [{ticker}] ✗ Volume insuficiente (R${avg_vol_brl:,.0f})")
            return None

    # Download horário
    hourly = download_ohlc(ticker, "1h", HOURLY_DAYS)
    if len(hourly) < MIN_HOURLY_OBS:
        print(f"  [{ticker}] ⚠ Dados horários insuficientes ({len(hourly)} barras) — usando só diário")
        hourly = pd.DataFrame()

    print(f"  [{ticker}] ✓ {len(daily)} barras diárias, {len(hourly)} horárias")

    # Calcula análises
    analysis = {
        "ticker":      ticker,
        "updated":     ts(),
        "daily_bars":  len(daily),
        "hourly_bars": len(hourly),
        "price":       round(float(daily["close"].iloc[-1]), 2),
        "change_pct":  round(float((daily["close"].iloc[-1] / daily["close"].iloc[-2] - 1) * 100), 2) if len(daily) > 1 else 0,
        "gap":         analyze_gaps(daily),
        "seasonality": analyze_seasonality(hourly, daily),
        "mean_reversion": analyze_mean_reversion(hourly, daily),
        "momentum":    analyze_momentum(daily),
        "volume":      analyze_volume_profile(daily, hourly),
    }

    return {
        "analysis": analysis,
        "ohlc":     build_ohlc_chart(hourly, OHLC_CHART_DAYS),
    }

# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ticker",     help="Processa só um ticker")
    parser.add_argument("--min-volume", type=float, default=2.0,
                        help="Volume mínimo diário em milhões R$ (default: 2)")
    args = parser.parse_args()

    min_vol = args.min_volume * 1_000_000
    tickers = [args.ticker.upper()] if args.ticker else B3_TICKERS

    print("=" * 60)
    print(f"  PNT Trade Lab — Quant Data Updater")
    print(f"  {ts()}")
    print(f"  {len(tickers)} tickers · volume mín: R${min_vol:,.0f}")
    print("=" * 60)

    OUTPUT_DIR.mkdir(exist_ok=True)
    index = []
    ok, skipped, failed = 0, 0, 0

    for i, ticker in enumerate(tickers, 1):
        print(f"\n[{i}/{len(tickers)}] {ticker}")
        try:
            result = process_ticker(ticker, min_vol)
            if result is None:
                skipped += 1
                continue

            ticker_dir = OUTPUT_DIR / ticker
            ticker_dir.mkdir(exist_ok=True)

            save_json(ticker_dir / "analysis.json", result["analysis"])
            if result["ohlc"]:
                save_json(ticker_dir / "ohlc_1h.json", result["ohlc"])

            index.append({
                "ticker":      ticker,
                "price":       result["analysis"]["price"],
                "change_pct":  result["analysis"]["change_pct"],
                "daily_bars":  result["analysis"]["daily_bars"],
                "hourly_bars": result["analysis"]["hourly_bars"],
                "updated":     result["analysis"]["updated"],
            })
            ok += 1
            print(f"  [{ticker}] ✅ Salvo")

            # Rate limit — evita ban do Yahoo Finance
            time.sleep(0.5)

        except Exception as e:
            print(f"  [{ticker}] ❌ Erro: {e}")
            failed += 1

    # Salva índice global
    index_data = {
        "updated":  ts(),
        "count":    len(index),
        "tickers":  sorted(index, key=lambda x: x["ticker"]),
    }
    save_json(OUTPUT_DIR / "_index.json", index_data)

    # Resumo
    print(f"\n{'='*60}")
    print(f"  RESUMO")
    print(f"  ✅ Processados: {ok}")
    print(f"  ⚠  Ignorados:   {skipped} (volume/dados insuficientes)")
    print(f"  ❌ Erros:        {failed}")
    print(f"  📁 Salvos em:    {OUTPUT_DIR}/")
    print(f"{'='*60}")

if __name__ == "__main__":
    main()
