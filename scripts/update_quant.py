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

GitHub Actions: rode diariamente às 07:00 BRT (10:00 UTC)
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
HOURLY_DAYS      = 728        # 2 anos (Yahoo limita ~730 dias para 1h)
DAILY_DAYS       = 1825       # 5 anos para dados diários (era 1095/3 anos)
MIN_HOURLY_OBS   = 500        # mínimo de barras horárias válidas
MIN_DAILY_OBS    = 400        # mínimo de barras diárias válidas
MIN_AVG_VOLUME   = 2_000_000  # volume médio diário mínimo (R$)
GAP_THRESHOLDS   = [1.0, 2.0, 3.0, 5.0]  # thresholds de gap para análise
OHLC_CHART_DAYS  = 60         # dias de OHLC horário para gráficos

# ── Configurações dos sinais do Cockpit ──────────────────────────────────────────
SIG_Z_THRESHOLD  = 2.0     # |z-score| mínimo para sinal de reversão
SIG_GAP_MIN      = 1.0     # gap % mínimo para sinal de gap
SIG_MIN_COUNT    = 20      # amostra mínima para confiar num edge histórico
SIG_EDGE_LB      = 0.55    # piso do IC de Wilson p/ considerar edge "real" (>55%)
SIG_WILSON_Z     = 1.96    # 95% de confiança

# ── Lista completa de tickers B3 ───────────────────────────────────────────────
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
    "AURE3","BRKM5","CGAS5","CLSC4","COCE5","CPRE3","CRPG6",
    "CSRN6","CURY3","CYRELA","DASA3","ESPA3","EVEN3","FIQE3","FRAS3",
    "GFSA3","GGBR3","GRND3","HBSA3","INEP4","JALL3","KEPL3","LAND3",
    "LIGT3","LIQH3","LOGG3","LOGN3","LPSB3","LVTC3","MELN3","MELK3",
    "MILS3","MNPR3","MTRE3","MXRF11","NEFIN3","NINJ3","OIBR3","OPCT3",
    "PARD3","PMAM3","PORT3","PTBL3","RAIA3","RBEV3","RCSL4",
    "ROMI3","RSID3","SANB4","SCAR3","SEQL3","SFXA4","SGPS3",
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
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        # FIX: bloco duplicado removido — NaN/Inf agora tratados corretamente
        v = float(obj)
        return None if (np.isnan(v) or np.isinf(v)) else v
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
    if isinstance(obj, (np.ndarray,)):
        return obj.tolist()
    if isinstance(obj, pd.Timestamp):
        return str(obj.date())
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
    """Salva JSON sanitizado — FIX: era escrito duas vezes no mesmo arquivo."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(sanitize(data), f, ensure_ascii=False, separators=(",", ":"), default=to_json)

# ── Download ───────────────────────────────────────────────────────────────────
def download_ohlc(ticker: str, interval: str, days: int) -> pd.DataFrame:
    """Baixa dados OHLC do Yahoo Finance com preços ajustados (dividendos/splits)."""
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
                "threshold":    threshold,
                "direction":    direction,
                "count":        len(subset),
                "reversal_rate":round(float(reversals.mean() * 100), 1),
                "avg_gap":      round(float(subset["gap_pct"].mean()), 2),
                "avg_intraday": round(float(subset["intraday"].mean()), 2),
                "avg_d1":       round(float(subset["d1_ret"].dropna().mean()), 2) if len(subset["d1_ret"].dropna()) > 3 else None,
                "avg_d5":       round(float(subset["d5_ret"].dropna().mean()), 2) if len(subset["d5_ret"].dropna()) > 3 else None,
                "std_intraday": round(float(subset["intraday"].std()), 2),
                "win_rate":     round(float((subset["intraday"] > 0).mean() * 100), 1),
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

    if len(hourly) > 100:
        h = hourly.copy()
        h["ret"]  = h["close"].pct_change() * 100
        h["hour"] = h.index.hour
        h["dow"]  = h.index.dayofweek
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

    # Por mês (5 anos = ~5 amostras por mês — mais robusto que 3 anos)
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
                result["half_life_days"]  = round(hl / 8, 1)

    if len(daily) > 60:
        window = 60
        close  = daily["close"].dropna()
        mean   = close.iloc[-window:].mean()
        std    = close.iloc[-window:].std()
        if std > 0:
            result["zscore_current"] = round(float((close.iloc[-1] - mean) / std), 3)
            result["zscore_mean"]    = round(float(mean), 2)
            result["zscore_std"]     = round(float(std), 2)

    if len(daily) > 100:
        d = daily.copy()
        d["ret"] = d["close"].pct_change() * 100
        d = d.dropna(subset=["ret"])

        reversion_stats = {}
        for pct in [1.0, 2.0, 3.0]:
            up_days   = d[d["ret"] >= pct]
            down_days = d[d["ret"] <= -pct]

            if len(up_days) >= 5:
                next_ret_up = d["ret"].shift(-1).loc[up_days.index].dropna()
                reversion_stats[f"after_up_{pct:.0f}pct"] = {
                    "count":        len(up_days),
                    "next_mean":    round(float(next_ret_up.mean()), 3),
                    "reversal_pct": round(float((next_ret_up < 0).mean() * 100), 1),
                }
            if len(down_days) >= 5:
                next_ret_dn = d["ret"].shift(-1).loc[down_days.index].dropna()
                reversion_stats[f"after_down_{pct:.0f}pct"] = {
                    "count":        len(down_days),
                    "next_mean":    round(float(next_ret_dn.mean()), 3),
                    "reversal_pct": round(float((next_ret_dn > 0).mean() * 100), 1),
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

    seq_stats = {}
    for n in [2, 3, 4, 5]:
        up_seq   = (d["ret"] > 0).rolling(n).sum() == n
        down_seq = (d["ret"] < 0).rolling(n).sum() == n

        if up_seq.sum() >= 5:
            next_after_up = d["ret"].shift(-1)[up_seq].dropna()
            seq_stats[f"{n}_up_days"] = {
                "count":         int(up_seq.sum()),
                "next_mean":     round(float(next_after_up.mean()), 3),
                "continues_pct": round(float((next_after_up > 0).mean() * 100), 1),
            }
        if down_seq.sum() >= 5:
            next_after_dn = d["ret"].shift(-1)[down_seq].dropna()
            seq_stats[f"{n}_down_days"] = {
                "count":         int(down_seq.sum()),
                "next_mean":     round(float(next_after_dn.mean()), 3),
                "continues_pct": round(float((next_after_dn < 0).mean() * 100), 1),
            }
    result["consecutive_days"] = seq_stats

    returns = d["ret"]
    gains   = returns[returns > 0].mean() if len(returns[returns > 0]) > 0 else 0
    losses  = abs(returns[returns < 0].mean()) if len(returns[returns < 0]) > 0 else 0
    result["avg_gain"]        = round(float(gains), 3)
    result["avg_loss"]        = round(float(losses), 3)
    result["gain_loss_ratio"] = round(float(gains / losses), 3) if losses > 0 else None
    result["pos_day_rate"]    = round(float((returns > 0).mean() * 100), 1)

    return result

# ── Volume Profile ─────────────────────────────────────────────────────────────
def analyze_volume_profile(daily: pd.DataFrame, hourly: pd.DataFrame) -> dict:
    """Calcula distribuição de volume por dia da semana e hora."""
    result = {}

    if len(daily) > 50 and "volume" in daily.columns:
        d = daily.dropna(subset=["close","volume"]).copy()
        d["dow"] = d.index.dayofweek
        dow_vol = {}
        dow_labels = ["Seg","Ter","Qua","Qui","Sex"]
        for dow in range(5):
            sub = d[d["dow"] == dow]["volume"]
            if len(sub) > 5:
                dow_vol[dow_labels[dow]] = round(float(sub.mean()), 0)
        result["avg_volume_by_dow"]    = dow_vol
        result["avg_daily_volume"]     = round(float(d["volume"].mean()), 0)
        result["avg_daily_volume_brl"] = round(float((d["volume"] * d["close"]).mean()), 0)

    if len(hourly) > 100 and "volume" in hourly.columns:
        h = hourly.dropna(subset=["volume"]).copy()
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
                "o": round(float(row["open"]),  2),
                "h": round(float(row["high"]),  2),
                "l": round(float(row["low"]),   2),
                "c": round(float(row["close"]), 2),
                "v": int(row["volume"]) if "volume" in row and pd.notna(row["volume"]) else 0,
            })
        except Exception:
            continue
    return result

# ── Estado atual (live) — alimenta os sinais de momentum e gap ──────────────────
def build_live_state(daily: pd.DataFrame) -> dict:
    """Estado atual do ativo: streak de dias consecutivos + gap da última sessão.
    Num batch das 07:00 BRT a barra de hoje ainda não existe, então gap_today
    reflete a ÚLTIMA SESSÃO FECHADA (gap já realizado). Para gap como sinal de
    pré-abertura de verdade, rode o job perto da abertura (10:00 BRT)."""
    if len(daily) < 6:
        return {}
    d = daily.copy()
    d["ret"] = d["close"].pct_change() * 100
    rets = d["ret"].dropna()

    streak = {"count": 0, "dir": None}
    if len(rets):
        last = 1 if rets.iloc[-1] > 0 else -1 if rets.iloc[-1] < 0 else 0
        if last != 0:
            c = 0
            for r in reversed(rets.tolist()):
                s = 1 if r > 0 else -1 if r < 0 else 0
                if s == last:
                    c += 1
                else:
                    break
            streak = {"count": c, "dir": "up" if last > 0 else "down"}

    gap_today = None
    if len(d) >= 2 and "open" in d.columns:
        prev_close = float(d["close"].iloc[-2])
        today_open = float(d["open"].iloc[-1])
        if prev_close > 0:
            gap_today = round((today_open - prev_close) / prev_close * 100, 2)

    return {"streak": streak, "gap_today": gap_today}

# ── Sinais do Cockpit — IC de Wilson + geradores ────────────────────────────────
def wilson(successes: float, n: int, z: float = SIG_WILSON_Z):
    """Retorna (p, lo, hi) em 0..1. Mais honesto que p±erro normal p/ n pequeno."""
    if n <= 0:
        return 0.0, 0.0, 0.0
    p      = successes / n
    denom  = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    margin = z * np.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return p, max(0.0, center - margin), min(1.0, center + margin)

def _edge_lb(rate_pct, n):
    """Piso do IC de Wilson a partir de taxa(%) + n."""
    if rate_pct is None or n is None or n <= 0:
        return None
    succ = round(rate_pct / 100.0 * n)
    _, lo, _ = wilson(succ, n)
    return lo

def _sig_reversion(a):
    mr = a.get("mean_reversion") or {}
    z  = mr.get("zscore_current")
    if z is None or abs(z) < SIG_Z_THRESHOLD:
        return None
    side = "long" if z < 0 else "short"
    rev  = mr.get("reversion_after_move") or {}
    key  = "after_down_1pct" if side == "long" else "after_up_1pct"
    n_hist = rev.get(key, {}).get("count")
    lo     = _edge_lb(rev.get(key, {}).get("reversal_pct"), n_hist)
    confirmed = bool(lo is not None and n_hist and n_hist >= SIG_MIN_COUNT and lo > SIG_EDGE_LB)
    if confirmed:
        head = f"z-score {z:+.2f} (esticado). Reversão histórica na direção: piso IC {lo*100:.0f}%, n={n_hist}."
    else:
        head = f"z-score {z:+.2f} (esticado). Sem edge de reversão confirmado — só estiramento estatístico."
    return dict(type="reversao", side=side, value=round(float(z), 2),
                score=round(min(abs(z) / 3.0, 1.0) * 100, 1),
                confirmed=confirmed, headline=head)

def _sig_momentum(a):
    streak = (a.get("live") or {}).get("streak") or {}
    n_run  = streak.get("count", 0)
    d      = streak.get("dir")
    if n_run < 3 or d not in ("up", "down"):
        return None
    cons   = (a.get("momentum") or {}).get("consecutive_days") or {}
    bucket = min(n_run, 5)
    hist   = cons.get(f"{bucket}_{d}_days")
    if not hist:
        return None
    n_hist = hist.get("count")
    lo     = _edge_lb(hist.get("continues_pct"), n_hist)
    if lo is None or n_hist is None or n_hist < SIG_MIN_COUNT:
        return None
    side = "long" if d == "up" else "short"
    head = (f"{n_run} dias {'de alta' if d == 'up' else 'de baixa'} consecutivos. "
            f"Sequências de {bucket}d continuam {hist['continues_pct']:.0f}% (piso IC {lo*100:.0f}%, n={n_hist}).")
    return dict(type="momentum", side=side, value=int(n_run),
                score=round((max(lo, 0.5) - 0.5) * 200, 1),
                confirmed=bool(lo > SIG_EDGE_LB), headline=head)

def _sig_gap(a):
    gap = (a.get("live") or {}).get("gap_today")
    if gap is None or abs(gap) < SIG_GAP_MIN:
        return None
    direction = "up" if gap > 0 else "down"
    bt = (a.get("gap") or {}).get("by_threshold") or {}
    chosen = None
    for thr in (5.0, 3.0, 2.0, 1.0):
        if abs(gap) >= thr:
            cand = bt.get(f"{thr:.0f}pct_{direction}")
            if cand and cand.get("count", 0) >= SIG_MIN_COUNT:
                chosen = cand
                break
    if not chosen:
        return None
    n    = chosen.get("count")
    fade = chosen.get("reversal_rate", 0) > 55
    if fade:
        side = "short" if direction == "up" else "long"
        lo   = _edge_lb(chosen.get("reversal_rate"), n)
        head = (f"Gap {'de alta' if direction == 'up' else 'de baixa'} {gap:+.2f}% (últ. sessão). "
                f"Fade: reverte {chosen['reversal_rate']:.0f}% (piso IC {(lo or 0)*100:.0f}%, n={n}).")
    else:
        side = "long" if direction == "up" else "short"
        lo   = _edge_lb(chosen.get("win_rate"), n)
        head = (f"Gap {'de alta' if direction == 'up' else 'de baixa'} {gap:+.2f}% (últ. sessão). "
                f"Sustenta: win rate {chosen['win_rate']:.0f}% (piso IC {(lo or 0)*100:.0f}%, n={n}).")
    return dict(type="gap", side=side, value=round(float(gap), 2),
                score=round((max(lo or 0.5, 0.5) - 0.5) * 200, 1),
                confirmed=bool(lo is not None and lo > SIG_EDGE_LB), headline=head)

def build_signals(analyses: list) -> dict:
    """A partir da lista de analyses já calculadas, monta os sinais ativos."""
    signals = []
    for a in analyses:
        base = dict(ticker=a.get("ticker"), price=a.get("price"), change_pct=a.get("change_pct"))
        for fn in (_sig_reversion, _sig_momentum, _sig_gap):
            s = fn(a)
            if s:
                signals.append({**base, **s})
    # confirmados primeiro, depois por score
    signals.sort(key=lambda s: (s["confirmed"], s["score"]), reverse=True)
    return {
        "updated": ts(),
        "count":   len(signals),
        "params":  dict(z_threshold=SIG_Z_THRESHOLD, gap_min=SIG_GAP_MIN,
                        min_count=SIG_MIN_COUNT, edge_lb=SIG_EDGE_LB),
        "signals": signals,
    }

# ── Processa um ativo ─────────────────────────────────────────────────────────
def process_ticker(ticker: str, min_volume: float) -> dict | None:
    """Baixa dados e calcula todas as análises para um ativo."""
    print(f"  [{ticker}] Baixando dados...")

    # Download diário (5 anos)
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

    # Download horário (2 anos — limite Yahoo Finance para 1h)
    hourly = download_ohlc(ticker, "1h", HOURLY_DAYS)
    if len(hourly) < MIN_HOURLY_OBS:
        print(f"  [{ticker}] ⚠ Dados horários insuficientes ({len(hourly)} barras) — usando só diário")
        hourly = pd.DataFrame()

    print(f"  [{ticker}] ✓ {len(daily)} barras diárias, {len(hourly)} horárias")

    analysis = {
        "ticker":         ticker,
        "updated":        ts(),
        "daily_bars":     len(daily),
        "hourly_bars":    len(hourly),
        "price":          round(float(daily["close"].iloc[-1]), 2),
        "change_pct":     round(float((daily["close"].iloc[-1] / daily["close"].iloc[-2] - 1) * 100), 2) if len(daily) > 1 else 0,
        "gap":            analyze_gaps(daily),
        "seasonality":    analyze_seasonality(hourly, daily),
        "mean_reversion": analyze_mean_reversion(hourly, daily),
        "momentum":       analyze_momentum(daily),
        "volume":         analyze_volume_profile(daily, hourly),
        "live":           build_live_state(daily),
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
    index    = []
    analyses = []
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
            analyses.append(result["analysis"])
            ok += 1
            print(f"  [{ticker}] ✅ Salvo")

            # Rate limit — evita ban do Yahoo Finance
            time.sleep(0.5)

        except Exception as e:
            print(f"  [{ticker}] ❌ Erro: {e}")
            failed += 1

    save_json(OUTPUT_DIR / "_index.json", {
        "updated": ts(),
        "count":   len(index),
        "tickers": sorted(index, key=lambda x: x["ticker"]),
    })

    # Sinais do Cockpit — ativos que estão dando sinal agora
    signals = build_signals(analyses)
    save_json(OUTPUT_DIR / "signals.json", signals)

    print(f"\n{'='*60}")
    print(f"  RESUMO")
    print(f"  ✅ Processados: {ok}")
    print(f"  ⚠  Ignorados:   {skipped} (volume/dados insuficientes)")
    print(f"  ❌ Erros:        {failed}")
    print(f"  🎯 Sinais:       {signals['count']} ({sum(s['confirmed'] for s in signals['signals'])} confirmados)")
    print(f"  📁 Salvos em:    {OUTPUT_DIR}/")
    print(f"{'='*60}")

if __name__ == "__main__":
    main()
