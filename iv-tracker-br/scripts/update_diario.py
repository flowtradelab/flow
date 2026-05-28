"""
update_diario.py
================
Busca a IV ATM do dia via yfinance para todos os ativos e adiciona
ao histórico em data/iv_history/<TICKER>.json.

Metodologia:
  - Faixa ATM: strikes entre -5% e +5% do preço atual
  - IV do dia = média(iv_calls_atm, iv_puts_atm)
  - Mesmo formato do histórico coletado via opcoes.net.br

Uso:
    python scripts/update_diario.py            → todos os ativos
    python scripts/update_diario.py PETR4 VALE3 → só esses
"""

import json
import sys
import time
from datetime import date
from pathlib import Path

import yfinance as yf

# ── Configuração ──────────────────────────────────────────────
BASE_DIR = Path(__file__).parent.parent          # raiz do iv-tracker-br
DATA_DIR = BASE_DIR / "data" / "iv_history"

ATIVOS = [
    "ABEV3","ALOS3","ASAI3","AURE3","AXIA3","AXIA6","AZZA3","B3SA3","BBAS3",
    "BBDC3","BBDC4","BBSE3","BEEF3","BPAC11","BRAP4","BRAV3","BRKM5","CEAB3",
    "CMIG4","CMIN3","COGN3","CPFE3","CPLE3","CSAN3","CSMG3","CSNA3","CURY3",
    "CXSE3","CYRE3","DIRR3","EGIE3","EMBJ3","ENEV3","ENGI11","EQTL3","FLRY3",
    "GGBR4","GOAU4","HAPV3","HYPE3","IGTI11","ISAE4","ITSA4","ITUB4","KLBN11",
    "LREN3","MBRF3","MGLU3","MOTV3","MRVE3","MULT3","NATU3","PETR3","PETR4",
    "POMO4","PRIO3","PSSA3","RADL3","RAIL3","RDOR3","RECV3","RENT3","ROXO34",
    "SANB11","SBSP3","SLCE3","SMFT3","SUZB3","TAEE11","TIMS3","TOTS3","UGPA3",
    "USIM5","VALE3","VAMO3","VBBR3","VIVA3","VIVT3","WEGE3","XPBR31","YDUQ3",
]

FAIXA_ATM = 0.05   # -5% a +5% do preço atual


# ── Funções ───────────────────────────────────────────────────

def get_preco_atual(ticker: yf.Ticker) -> float | None:
    """Retorna o último preço de fechamento."""
    try:
        hist = ticker.history(period="1d")
        if hist.empty:
            return None
        return float(hist["Close"].iloc[-1])
    except Exception:
        return None


def get_iv_atm(ticker: yf.Ticker, preco: float) -> dict | None:
    """
    Calcula a IV ATM como média das impliedVolatility de calls e puts
    cujos strikes estão entre -5% e +5% do preço atual.

    Retorna {"iv": float, "n_calls": int, "n_puts": int} ou None.
    """
    try:
        vencimentos = ticker.options
    except Exception:
        return None

    if not vencimentos:
        return None

    # Usa o vencimento mais próximo (30 dias ou menos)
    from datetime import datetime
    hoje = datetime.today()
    venc_alvo = None

    for v in vencimentos:
        dias = (datetime.strptime(v, "%Y-%m-%d") - hoje).days
        if dias >= 7:   # ignora vencimentos muito próximos (< 1 semana)
            venc_alvo = v
            break

    if not venc_alvo:
        venc_alvo = vencimentos[0]

    try:
        chain = ticker.option_chain(venc_alvo)
    except Exception:
        return None

    calls = chain.calls
    puts  = chain.puts

    limite_inf = preco * (1 - FAIXA_ATM)
    limite_sup = preco * (1 + FAIXA_ATM)

    calls_atm = calls[
        (calls["strike"] >= limite_inf) &
        (calls["strike"] <= limite_sup) &
        (calls["impliedVolatility"] > 0)
    ]["impliedVolatility"]

    puts_atm = puts[
        (puts["strike"] >= limite_inf) &
        (puts["strike"] <= limite_sup) &
        (puts["impliedVolatility"] > 0)
    ]["impliedVolatility"]

    if calls_atm.empty and puts_atm.empty:
        return None

    iv_calls = float(calls_atm.mean()) if not calls_atm.empty else None
    iv_puts  = float(puts_atm.mean())  if not puts_atm.empty  else None

    if iv_calls and iv_puts:
        iv_media = (iv_calls + iv_puts) / 2
    elif iv_calls:
        iv_media = iv_calls
    else:
        iv_media = iv_puts

    return {
        "iv":      round(iv_media, 6),
        "n_calls": len(calls_atm),
        "n_puts":  len(puts_atm),
    }


def adicionar_ponto(ativo: str, ponto: dict) -> bool:
    """
    Adiciona um ponto novo ao JSON existente.
    Retorna True se adicionou, False se a data já existia.
    """
    path = DATA_DIR / f"{ativo}.json"

    if path.exists():
        dados = json.loads(path.read_text(encoding="utf-8"))
    else:
        dados = {
            "ativo": ativo,
            "fonte": "yfinance",
            "serie_iv_diaria": [],
            "tabela_atual": {},
        }

    datas = {p["date"] for p in dados["serie_iv_diaria"]}
    if ponto["date"] in datas:
        return False   # já existe, não duplica

    dados["serie_iv_diaria"].append(ponto)
    dados["serie_iv_diaria"] = sorted(dados["serie_iv_diaria"], key=lambda x: x["date"])
    dados["ultima_atualizacao"] = ponto["date"]

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dados, ensure_ascii=False, indent=2), encoding="utf-8")
    return True


# ── Main ──────────────────────────────────────────────────────

def main():
    ativos = [a.upper() for a in sys.argv[1:]] or ATIVOS
    hoje   = date.today().isoformat()

    print(f"\n📅 Update diário — {hoje}")
    print(f"📦 {len(ativos)} ativos\n")

    ok, pulados, erros = 0, 0, []

    for i, ativo in enumerate(ativos, 1):
        ticker_symbol = f"{ativo}.SA"
        try:
            ticker = yf.Ticker(ticker_symbol)
            preco  = get_preco_atual(ticker)

            if preco is None:
                raise ValueError("preço não encontrado")

            resultado = get_iv_atm(ticker, preco)

            if resultado is None:
                raise ValueError("chain de opções não disponível")

            ponto = {
                "date":     hoje,
                "iv":       resultado["iv"],
                "negocios": None,   # yfinance não fornece volume total
                "fonte":    "yfinance",
                "n_strikes": resultado["n_calls"] + resultado["n_puts"],
            }

            adicionado = adicionar_ponto(ativo, ponto)

            if adicionado:
                print(f"  [{i:>2}/{len(ativos)}] ✅ {ativo:<8} IV={resultado['iv']*100:.2f}%  "
                      f"(calls:{resultado['n_calls']} puts:{resultado['n_puts']})")
                ok += 1
            else:
                print(f"  [{i:>2}/{len(ativos)}] ⏭  {ativo:<8} já atualizado hoje")
                pulados += 1

        except Exception as e:
            print(f"  [{i:>2}/{len(ativos)}] ❌ {ativo:<8} {e}")
            erros.append(ativo)

        time.sleep(0.5)

    print(f"\n{'═'*50}")
    print(f"✅ {ok} atualizados  |  ⏭ {pulados} já existiam  |  ❌ {len(erros)} erros")
    if erros:
        print(f"Erros: {', '.join(erros)}")
    print()


if __name__ == "__main__":
    main()
