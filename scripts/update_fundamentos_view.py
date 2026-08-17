"""
Fundamentos CVM — View por Ticker
Roda depois de update_fundamentos.py, no mesmo workflow.

Cruza os dados fundamentalistas brutos (fundamentos/data/, baixados da CVM)
com o preço que o repo já coleta em quant/{TICKER}/ e com o histórico de
proventos do yfinance, e escreve um resumo por ativo em:

  fundamentos/{TICKER}/resumo.json

O que entra no resumo (fundamentos/{TICKER}/resumo.json):
  - Última Receita Líquida, Lucro/Prejuízo do Período, Patrimônio Líquido e
    Ativo Total reportados (DFP/ITR, prioriza consolidado sobre individual)
  - Preço atual (lido de quant/{TICKER}/analysis.json — não faz nova chamada
    de preço)
  - Valor de mercado e nº de ações (via yfinance fast_info)
  - Múltiplos derivados: P/L, P/VP, Margem Líquida, ROE
  - Dividend Yield (últimos 12 meses) e histórico de proventos, via
    yfinance (ticker.dividends)

Além do resumo, também escreve fundamentos/{TICKER}/historico.json — uma
série com TODOS os períodos disponíveis (trimestres isolados do ITR +
fechamentos anuais do DFP), pra dar pra comparar "esse trimestre vs. mesmo
trimestre do ano passado" ou a evolução ano a ano dentro do seu app.

⚠️ Limitação importante: os nomes das contas do plano padronizado da CVM
variam entre setores (bancos e seguradoras usam um modelo de DRE diferente
do de empresas não-financeiras). O parser abaixo tenta casar as contas certas
por texto (ex: "Lucro" + "Período") e pega a linha de nível mais alto — mas
pra instituições financeiras isso pode não encontrar todas as contas. Quando
algum campo não é encontrado, o resumo sai com esse campo em null e
"dados_incompletos": true, em vez de arriscar um número errado. Vale
conferir manualmente os primeiros resultados antes de confiar no dado.
"""

import json
import os
import re
import unicodedata
from pathlib import Path

import pandas as pd

try:
    import yfinance as yf
except ImportError:
    yf = None

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "fundamentos" / "data"
QUANT_DIR = REPO_ROOT / "quant"
OUT_ROOT = REPO_ROOT / "fundamentos"


def normalizar(txt: str) -> str:
    if not isinstance(txt, str):
        return ""
    sem_acento = unicodedata.normalize("NFKD", txt).encode("ascii", "ignore").decode()
    return sem_acento.upper()


def carregar_csv(nome: str) -> pd.DataFrame | None:
    caminho = DATA_DIR / nome
    if not caminho.exists():
        return None
    df = pd.read_csv(caminho, dtype=str)
    df["VL_CONTA"] = pd.to_numeric(df.get("VL_CONTA"), errors="coerce")
    return df


def linha_mais_recente(df: pd.DataFrame, cnpj: str, incluir: list[str], excluir: list[str] | None = None):
    """
    Entre as contas de uma empresa (já filtrado por CNPJ), acha a linha cujo
    DS_CONTA contém todos os termos de `incluir` (e nenhum de `excluir`), com
    o CD_CONTA mais "raso" (linha-síntese, não sub-conta) e o período mais
    recente. Prioriza TIPO_DEMONSTRATIVO == 'con' (consolidado) sobre 'ind'.
    """
    if df is None:
        return None
    sub = df[df["CNPJ_CIA"].astype(str).str.replace(r"\D", "", regex=True) == cnpj]
    if sub.empty:
        return None

    excluir = excluir or []
    ds_norm = sub["DS_CONTA"].fillna("").map(normalizar)
    ok = pd.Series(True, index=sub.index)
    for termo in incluir:
        ok &= ds_norm.str.contains(termo)
    for termo in excluir:
        ok &= ~ds_norm.str.contains(termo)
    candidatos = sub[ok].copy()
    if candidatos.empty:
        return None

    candidatos["profundidade"] = candidatos["CD_CONTA"].astype(str).str.count(r"\.")
    candidatos["eh_consolidado"] = (candidatos["TIPO_DEMONSTRATIVO"] == "con").astype(int)
    candidatos = candidatos.sort_values(
        ["DT_FIM_EXERC", "eh_consolidado", "profundidade"],
        ascending=[False, False, True],
    )
    return candidatos.iloc[0]


def serie_conta(df: pd.DataFrame, cnpj: str, incluir: list[str]) -> pd.DataFrame:
    """
    Como linha_mais_recente, mas devolve UMA LINHA POR PERÍODO (não só a mais
    recente) — resolvendo empate entre consolidado/individual e profundidade
    da conta do mesmo jeito. Usada pra montar a série histórica.
    """
    if df is None:
        return pd.DataFrame()
    sub = df[df["CNPJ_CIA"].astype(str).str.replace(r"\D", "", regex=True) == cnpj]
    if sub.empty:
        return pd.DataFrame()

    ds_norm = sub["DS_CONTA"].fillna("").map(normalizar)
    ok = pd.Series(True, index=sub.index)
    for termo in incluir:
        ok &= ds_norm.str.contains(termo)
    candidatos = sub[ok].copy()
    if candidatos.empty:
        return pd.DataFrame()

    candidatos["profundidade"] = candidatos["CD_CONTA"].astype(str).str.count(r"\.")
    candidatos["eh_consolidado"] = (candidatos["TIPO_DEMONSTRATIVO"] == "con").astype(int)
    candidatos = candidatos.sort_values(["eh_consolidado", "profundidade"], ascending=[False, True])
    # uma linha por período — fica com a de maior prioridade (consolidado + mais rasa)
    return candidatos.drop_duplicates(subset=["DT_FIM_EXERC"], keep="first")


def montar_historico(cnpj: str, dfp: dict, itr: dict) -> list[dict]:
    """
    Junta os períodos anuais (DFP) e trimestrais isolados (ITR, já filtrados
    em update_fundamentos.py) numa única série ordenada por data, pra dar pra
    comparar evolução ao longo do tempo dentro do app.
    """
    linhas: dict[tuple[str, str], dict] = {}

    def registrar(fontes: dict, tipo_periodo: str):
        campos = {
            "ativo_total": serie_conta(fontes.get("bpa"), cnpj, ["ATIVO TOTAL"]),
            "patrimonio_liquido": serie_conta(fontes.get("bpp"), cnpj, ["PATRIMONIO LIQUIDO"]),
            "receita_liquida": serie_conta(fontes.get("dre"), cnpj, ["RECEITA"]),
        }
        lucro = serie_conta(fontes.get("dre"), cnpj, ["LUCRO", "PERIODO"])
        if lucro.empty:
            lucro = serie_conta(fontes.get("dre"), cnpj, ["PREJUIZO", "PERIODO"])
        campos["lucro_liquido"] = lucro

        for nome_campo, serie in campos.items():
            for _, row in serie.iterrows():
                dt = str(row["DT_FIM_EXERC"])
                chave = (dt, tipo_periodo)
                # todo ponto sempre nasce com os 4 campos presentes (mesmo que
                # null) — schema consistente é mais fácil de consumir no app
                # do que ter que checar se a chave existe.
                linhas.setdefault(chave, {
                    "data_fim_exercicio": dt,
                    "tipo_periodo": tipo_periodo,
                    "ativo_total": None,
                    "patrimonio_liquido": None,
                    "receita_liquida": None,
                    "lucro_liquido": None,
                })
                valor = row["VL_CONTA"]
                linhas[chave][nome_campo] = float(valor) if pd.notna(valor) else None

    registrar(dfp, "anual")
    registrar(itr, "trimestre")

    serie = list(linhas.values())
    for ponto in serie:
        lucro = ponto.get("lucro_liquido")
        receita = ponto.get("receita_liquida")
        pl = ponto.get("patrimonio_liquido")
        ponto["margem_liquida"] = round(lucro / receita, 4) if (lucro is not None and receita) else None
        ponto["roe"] = round(lucro / pl, 4) if (lucro is not None and pl) else None
        ponto["dados_incompletos"] = any(
            ponto[c] is None for c in ("ativo_total", "patrimonio_liquido", "receita_liquida", "lucro_liquido")
        )

    serie.sort(key=lambda x: (x["data_fim_exercicio"], x["tipo_periodo"]))
    return serie


def carregar_preco_quant(ticker: str):
    caminho = QUANT_DIR / ticker / "analysis.json"
    if not caminho.exists():
        return None
    try:
        with open(caminho, encoding="utf-8") as f:
            d = json.load(f)
        return d.get("price")
    except Exception:
        return None


def montar_resumo(ticker: str, cnpj: str, dfp: dict, itr: dict, cadastro: pd.DataFrame | None):
    incompleto = False

    def pegar(fontes, incluir, excluir=None):
        """Tenta achar a conta primeiro no ITR (mais recente) e cai pro DFP."""
        for df in fontes:
            r = linha_mais_recente(df, cnpj, incluir, excluir)
            if r is not None:
                return float(r["VL_CONTA"]) if pd.notna(r["VL_CONTA"]) else None, str(r["DT_FIM_EXERC"])
        return None, None

    ativo_total, dt_ativo = pegar([itr.get("bpa"), dfp.get("bpa")], ["ATIVO TOTAL"])
    pl, dt_pl = pegar([itr.get("bpp"), dfp.get("bpp")], ["PATRIMONIO LIQUIDO"])
    receita, dt_receita = pegar([itr.get("dre"), dfp.get("dre")], ["RECEITA"])
    lucro, dt_lucro = pegar([itr.get("dre"), dfp.get("dre")], ["LUCRO", "PERIODO"])
    if lucro is None:
        lucro, dt_lucro = pegar([itr.get("dre"), dfp.get("dre")], ["PREJUIZO", "PERIODO"])

    for campo in (ativo_total, pl, receita, lucro):
        if campo is None:
            incompleto = True

    razao_social = None
    if cadastro is not None:
        linha = cadastro[cadastro["CNPJ_CIA_norm"] == cnpj]
        if not linha.empty:
            razao_social = linha.iloc[0].get("DENOM_SOCIAL") or linha.iloc[0].get("DENOM_COMERCIAL")

    preco = carregar_preco_quant(ticker)

    valor_mercado = None
    n_acoes = None
    dividend_yield_12m = None
    proventos = []

    if yf is not None:
        try:
            yt = yf.Ticker(f"{ticker}.SA")
            fi = yt.fast_info
            valor_mercado = getattr(fi, "market_cap", None)
            n_acoes = getattr(fi, "shares", None)
            if preco is None:
                preco = getattr(fi, "last_price", None)

            divs = yt.dividends
            if divs is not None and not divs.empty:
                ult_12m = divs[divs.index >= (divs.index.max() - pd.Timedelta(days=365))]
                soma_12m = float(ult_12m.sum())
                if preco:
                    dividend_yield_12m = round(soma_12m / preco, 4)
                proventos = [
                    {"data": str(idx.date()), "valor_por_acao": round(float(v), 4)}
                    for idx, v in divs.tail(20).items()
                ]
        except Exception as e:
            print(f"    ⚠️  yfinance falhou para {ticker}: {e}")
            incompleto = True
    else:
        incompleto = True

    p_l = (valor_mercado / lucro) if (valor_mercado and lucro and lucro > 0) else None
    p_vp = (valor_mercado / pl) if (valor_mercado and pl and pl > 0) else None
    margem_liquida = (lucro / receita) if (lucro is not None and receita) else None
    roe = (lucro / pl) if (lucro is not None and pl) else None

    return {
        "ticker": ticker,
        "cnpj": cnpj,
        "razao_social": razao_social,
        "atualizado_em": pd.Timestamp.now("UTC").isoformat(),
        "fundamentos": {
            "ativo_total": ativo_total,
            "patrimonio_liquido": pl,
            "receita_liquida": receita,
            "lucro_liquido": lucro,
            "data_referencia_balanco": dt_ativo,
            "data_referencia_resultado": dt_lucro,
        },
        "mercado": {
            "preco": preco,
            "valor_de_mercado": valor_mercado,
            "numero_acoes": n_acoes,
        },
        "multiplos": {
            "p_l": round(p_l, 2) if p_l else None,
            "p_vp": round(p_vp, 2) if p_vp else None,
            "margem_liquida": round(margem_liquida, 4) if margem_liquida is not None else None,
            "roe": round(roe, 4) if roe is not None else None,
            "dividend_yield_12m": dividend_yield_12m,
        },
        "proventos_recentes": proventos,
        "dados_incompletos": incompleto,
    }


def main():
    tickers_cnpj_path = DATA_DIR / "tickers_cnpj.csv"
    if not tickers_cnpj_path.exists():
        print("⚠️  fundamentos/data/tickers_cnpj.csv não existe ainda — "
              "rode update_fundamentos.py primeiro. Abortando.")
        return

    tickers_cnpj = pd.read_csv(tickers_cnpj_path, dtype=str)

    cadastro = carregar_csv("cadastro_cias.csv")
    if cadastro is not None and "CNPJ_CIA" in cadastro.columns:
        cadastro["CNPJ_CIA_norm"] = cadastro["CNPJ_CIA"].astype(str).str.replace(r"\D", "", regex=True)

    dfp = {chave: carregar_csv(f"dfp_{chave}.csv") for chave in ("bpa", "bpp", "dre", "dfc_md", "dfc_mi")}
    itr = {chave: carregar_csv(f"itr_{chave}.csv") for chave in ("bpa", "bpp", "dre", "dfc_md", "dfc_mi")}

    # só monta resumo pra quem o repo já acompanha em quant/ (evita rodar
    # yfinance pra milhares de ativos ilíquidos)
    index_path = QUANT_DIR / "_index.json"
    tickers_acompanhados = None
    if index_path.exists():
        with open(index_path, encoding="utf-8") as f:
            idx = json.load(f)
        tickers_acompanhados = {t["ticker"] for t in idx.get("tickers", [])}

    total, ok = 0, 0
    for _, row in tickers_cnpj.iterrows():
        ticker, cnpj = row["TICKER"], row["CNPJ"]
        if tickers_acompanhados is not None and ticker not in tickers_acompanhados:
            continue
        total += 1
        print(f"→ {ticker}")
        try:
            resumo = montar_resumo(ticker, cnpj, dfp, itr, cadastro)
            historico = montar_historico(cnpj, dfp, itr)
        except Exception as e:
            print(f"  ⚠️  falhou: {e}")
            continue

        pasta = OUT_ROOT / ticker
        pasta.mkdir(parents=True, exist_ok=True)
        with open(pasta / "resumo.json", "w", encoding="utf-8") as f:
            json.dump(resumo, f, ensure_ascii=False, indent=2)
        with open(pasta / "historico.json", "w", encoding="utf-8") as f:
            json.dump({"ticker": ticker, "cnpj": cnpj, "periodos": historico}, f, ensure_ascii=False, indent=2)
        ok += 1

    print(f"\n✅ {ok}/{total} resumos + históricos gerados em fundamentos/<TICKER>/")


if __name__ == "__main__":
    main()
