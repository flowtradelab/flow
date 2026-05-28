"""
iv_rank.py
==========
Lê os JSONs e calcula IV Rank e Percentil para todos os ativos.

Uso:
    python scripts/iv_rank.py              → todos os ativos
    python scripts/iv_rank.py PETR4 VALE3  → só esses
"""

import json
import sys
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent
DATA_DIR = BASE_DIR / "data" / "iv_history"
JANELA   = 252


def calcular(ativo: str) -> dict | None:
    path = DATA_DIR / f"{ativo}.json"
    if not path.exists():
        return None

    dados  = json.loads(path.read_text(encoding="utf-8"))
    serie  = sorted(dados.get("serie_iv_diaria", []), key=lambda x: x["date"])
    serie  = serie[-JANELA:]

    if len(serie) < 20:
        return {"ativo": ativo, "aviso": f"poucos dados ({len(serie)} pontos)"}

    valores   = [p["iv"] for p in serie]
    iv_atual  = valores[-1]
    iv_min    = min(valores)
    iv_max    = max(valores)
    iv_rank   = (iv_atual - iv_min) / (iv_max - iv_min) * 100 if iv_max != iv_min else 0
    iv_pct    = sum(1 for v in valores if v < iv_atual) / len(valores) * 100

    # Percentil do site (se disponível)
    tabela  = dados.get("tabela_atual", {})
    atm_key = next((k for k in tabela if "ATM" in k), None)
    percentil_site = None
    if atm_key:
        p = tabela[atm_key].get("geral", {}).get("percentil", "")
        if p:
            percentil_site = p.replace("º", "")

    return {
        "ativo":          ativo,
        "iv_atual":       round(iv_atual * 100, 2),
        "iv_rank":        round(iv_rank, 1),
        "iv_percentil":   round(iv_pct, 1),
        "percentil_site": percentil_site,
        "iv_min":         round(iv_min * 100, 2),
        "iv_max":         round(iv_max * 100, 2),
        "pontos":         len(serie),
        "ultimo_dia":     serie[-1]["date"],
    }


def sinal(rank):
    if rank >= 80: return "🔴 ALTA"
    if rank <= 20: return "🟢 BAIXA"
    return               "⚪ normal"


def main():
    ativos = [a.upper() for a in sys.argv[1:]] or [p.stem for p in sorted(DATA_DIR.glob("*.json"))]

    if not ativos:
        print("Nenhum JSON encontrado em data/iv_history/")
        print("Rode update_diario.py primeiro.")
        return

    resultados, avisos = [], []

    for ativo in ativos:
        r = calcular(ativo)
        if r is None:
            print(f"⚠️  {ativo}: arquivo não encontrado")
        elif "aviso" in r:
            avisos.append(r)
        else:
            resultados.append(r)

    resultados.sort(key=lambda x: x["iv_rank"], reverse=True)

    print(f"\n{'─'*82}")
    print(f"{'ATIVO':<8} {'IV%':>6}  {'IV Rank':>8}  {'Percentil':>10}  {'P.Site':>7}  {'Min%':>6} {'Max%':>6}  {'Sinal'}")
    print(f"{'─'*82}")

    for r in resultados:
        ps = f"{r['percentil_site']:>5}º" if r["percentil_site"] else "     -"
        print(
            f"{r['ativo']:<8} "
            f"{r['iv_atual']:>5.1f}%  "
            f"{r['iv_rank']:>7.1f}%  "
            f"{r['iv_percentil']:>9.1f}%  "
            f"{ps}  "
            f"{r['iv_min']:>5.1f}% "
            f"{r['iv_max']:>5.1f}%  "
            f"{sinal(r['iv_rank'])}"
        )

    print(f"{'─'*82}")
    print(f"\n{len(resultados)} ativos | janela: {JANELA} dias úteis\n")

    if avisos:
        print("⚠️  Ativos com poucos dados:")
        for a in avisos:
            print(f"   {a['ativo']}: {a['aviso']}")


if __name__ == "__main__":
    main()
