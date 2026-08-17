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
  - Dívida bruta (Empréstimos e Financiamentos, curto + longo prazo), Caixa
    e Equivalentes e Dívida Líquida
  - EBITDA aproximado (EBIT + Depreciação/Amortização do fluxo de caixa) —
    não é conta nativa da CVM, é aproximação (ver aviso abaixo)
  - Fluxo de Caixa Operacional, de Investimento e de Financiamento, Capex
    aproximado (Imobilizado + Intangível) e FCF aproximado
  - Preço atual (lido de quant/{TICKER}/analysis.json — não faz nova chamada
    de preço)
  - Valor de mercado e nº de ações (via yfinance fast_info)
  - Múltiplos derivados: P/L, P/VP, Margem Líquida, ROE, Dívida Líquida/EBITDA
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


def _filtrar_candidatos(df: pd.DataFrame, cnpj: str, incluir: list[str]) -> pd.DataFrame:
    """Base comum: filtra por empresa e por termos no DS_CONTA (todos precisam bater)."""
    if df is None:
        return pd.DataFrame()
    sub = df[df["CNPJ_CIA"].astype(str).str.replace(r"\D", "", regex=True) == cnpj]
    if sub.empty:
        return pd.DataFrame()
    ds_norm = sub["DS_CONTA"].fillna("").map(normalizar)
    ok = pd.Series(True, index=sub.index)
    for termo in incluir:
        ok &= ds_norm.str.contains(termo)
    return sub[ok].copy()


def _somar_sem_dobrar(candidatos: pd.DataFrame) -> float | None:
    """
    Some VL_CONTA sem contar uma linha-síntese junto com as próprias
    subcontas dela: agrupa por "ramo" (2 primeiros níveis do CD_CONTA — ex:
    dívida de curto prazo "2.01.x" é um ramo, longo prazo "2.02.x" é outro) e
    fica só com a linha mais rasa de cada ramo antes de somar.
    """
    if candidatos.empty:
        return None
    candidatos = candidatos.copy()
    candidatos["profundidade"] = candidatos["CD_CONTA"].astype(str).str.count(r"\.")
    candidatos["ramo"] = candidatos["CD_CONTA"].astype(str).str.split(".").str[:2].str.join(".")
    candidatos = candidatos.sort_values("profundidade").drop_duplicates(subset=["ramo"], keep="first")
    total = candidatos["VL_CONTA"].sum()
    return float(total) if pd.notna(total) else None


def valor_mais_recente_somado(df: pd.DataFrame, cnpj: str, incluir: list[str]):
    """
    Como linha_mais_recente, mas soma todas as linhas que baterem no período
    mais recente (em vez de pegar só uma) — usado quando o valor que
    interessa vem fatiado em mais de uma conta (ex: dívida curto + longo
    prazo, capex de imobilizado + intangível).
    """
    candidatos = _filtrar_candidatos(df, cnpj, incluir)
    if candidatos.empty:
        return None, None

    candidatos["eh_consolidado"] = (candidatos["TIPO_DEMONSTRATIVO"] == "con").astype(int)
    if candidatos["eh_consolidado"].max() == 1:
        candidatos = candidatos[candidatos["eh_consolidado"] == 1]

    dt_mais_recente = candidatos["DT_FIM_EXERC"].max()
    do_periodo = candidatos[candidatos["DT_FIM_EXERC"] == dt_mais_recente]
    total = _somar_sem_dobrar(do_periodo)
    return total, str(dt_mais_recente)


def serie_soma_contas(df: pd.DataFrame, cnpj: str, incluir: list[str]) -> pd.DataFrame:
    """Como serie_conta, mas somando (sem dobrar) todas as linhas de cada período."""
    candidatos = _filtrar_candidatos(df, cnpj, incluir)
    if candidatos.empty:
        return pd.DataFrame()

    candidatos["eh_consolidado"] = (candidatos["TIPO_DEMONSTRATIVO"] == "con").astype(int)

    linhas = []
    for dt, grupo in candidatos.groupby("DT_FIM_EXERC"):
        g = grupo[grupo["eh_consolidado"] == 1] if grupo["eh_consolidado"].max() == 1 else grupo
        total = _somar_sem_dobrar(g)
        if total is not None:
            linhas.append({"DT_FIM_EXERC": dt, "VL_CONTA": total})
    return pd.DataFrame(linhas)


def concat_dfc(fontes: dict) -> pd.DataFrame | None:
    """Junta DFC-MD e DFC-MI — a empresa reporta um ou outro (raramente os dois)."""
    partes = [fontes.get("dfc_mi"), fontes.get("dfc_md")]
    partes = [p for p in partes if p is not None]
    return pd.concat(partes, ignore_index=True) if partes else None


# campos "base" que todo ponto do histórico sempre tem (com valor ou None) —
# schema consistente pro app não precisar checar se a chave existe.
CAMPOS_HISTORICO_BASE = (
    "ativo_total", "patrimonio_liquido", "receita_liquida", "lucro_liquido",
    "caixa_e_equivalentes", "divida_bruta", "ebit", "depreciacao_amortizacao",
    "caixa_operacional", "caixa_investimento", "caixa_financiamento", "capex",
)


def montar_historico(cnpj: str, dfp: dict, itr: dict) -> list[dict]:
    """
    Junta os períodos anuais (DFP) e trimestrais isolados (ITR, já filtrados
    em update_fundamentos.py) numa única série ordenada por data, pra dar pra
    comparar evolução ao longo do tempo dentro do app.
    """
    linhas: dict[tuple[str, str], dict] = {}

    def registrar(fontes: dict, tipo_periodo: str):
        dfc = concat_dfc(fontes)

        capex_imob = serie_soma_contas(dfc, cnpj, ["IMOBILIZADO"])
        capex_intang = serie_soma_contas(dfc, cnpj, ["INTANGIVEL"])
        capex = pd.concat([capex_imob, capex_intang], ignore_index=True)
        if not capex.empty:
            capex = capex.groupby("DT_FIM_EXERC", as_index=False)["VL_CONTA"].sum()

        campos = {
            "ativo_total": serie_conta(fontes.get("bpa"), cnpj, ["ATIVO TOTAL"]),
            "patrimonio_liquido": serie_conta(fontes.get("bpp"), cnpj, ["PATRIMONIO LIQUIDO"]),
            "receita_liquida": serie_conta(fontes.get("dre"), cnpj, ["RECEITA"]),
            "caixa_e_equivalentes": serie_conta(fontes.get("bpa"), cnpj, ["CAIXA E EQUIVALENTES"]),
            "divida_bruta": serie_soma_contas(fontes.get("bpp"), cnpj, ["EMPRESTIMOS E FINANCIAMENTOS"]),
            "ebit": serie_conta(fontes.get("dre"), cnpj, ["ANTES DO RESULTADO FINANCEIRO"]),
            "depreciacao_amortizacao": serie_conta(dfc, cnpj, ["DEPRECIACAO"]),
            "caixa_operacional": serie_conta(dfc, cnpj, ["CAIXA LIQUIDO", "OPERACIONAIS"]),
            "caixa_investimento": serie_conta(dfc, cnpj, ["CAIXA LIQUIDO", "INVESTIMENTO"]),
            "caixa_financiamento": serie_conta(dfc, cnpj, ["CAIXA LIQUIDO", "FINANCIAMENTO"]),
            "capex": capex,
        }
        lucro = serie_conta(fontes.get("dre"), cnpj, ["LUCRO", "PERIODO"])
        if lucro.empty:
            lucro = serie_conta(fontes.get("dre"), cnpj, ["PREJUIZO", "PERIODO"])
        campos["lucro_liquido"] = lucro

        for nome_campo, serie in campos.items():
            for _, row in serie.iterrows():
                dt = str(row["DT_FIM_EXERC"])
                chave = (dt, tipo_periodo)
                linhas.setdefault(chave, {
                    "data_fim_exercicio": dt,
                    "tipo_periodo": tipo_periodo,
                    **{c: None for c in CAMPOS_HISTORICO_BASE},
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
        ebit = ponto.get("ebit")
        da = ponto.get("depreciacao_amortizacao")
        divida_bruta = ponto.get("divida_bruta")
        caixa = ponto.get("caixa_e_equivalentes")
        cfo = ponto.get("caixa_operacional")
        capex = ponto.get("capex")

        ponto["margem_liquida"] = round(lucro / receita, 4) if (lucro is not None and receita) else None
        ponto["roe"] = round(lucro / pl, 4) if (lucro is not None and pl) else None

        # EBITDA aproximado = EBIT + Depreciação/Amortização. Não é uma conta
        # nativa da CVM (é métrica não-contábil que cada empresa calcula do
        # seu jeito) — essa é uma aproximação, pode não bater exatamente com
        # o "EBITDA ajustado" que a empresa divulga no release dela.
        ebitda = (ebit + da) if (ebit is not None and da is not None) else None
        ponto["ebitda_aprox"] = ebitda
        ponto["margem_ebitda_aprox"] = round(ebitda / receita, 4) if (ebitda is not None and receita) else None

        divida_liquida = (divida_bruta - caixa) if (divida_bruta is not None and caixa is not None) else None
        ponto["divida_liquida"] = divida_liquida
        ponto["divida_liquida_ebitda"] = (
            round(divida_liquida / ebitda, 2) if (divida_liquida is not None and ebitda) else None
        )

        # capex já sai negativo da CVM (é saída de caixa), então soma direto
        ponto["fcf_aprox"] = (cfo + capex) if (cfo is not None and capex is not None) else None

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

    def pegar_soma(fontes, incluir):
        """Como pegar(), mas soma todas as contas que baterem (ex: dívida CP+LP)."""
        for df in fontes:
            total, dt = valor_mais_recente_somado(df, cnpj, incluir)
            if total is not None:
                return total, dt
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

    # Dívida, EBITDA aproximado e fluxo de caixa — ver comentário em
    # montar_historico sobre a aproximação do EBITDA (EBIT + D&A).
    itr_dfc = concat_dfc(itr)
    dfp_dfc = concat_dfc(dfp)

    caixa, _ = pegar([itr.get("bpa"), dfp.get("bpa")], ["CAIXA E EQUIVALENTES"])
    divida_bruta, dt_divida = pegar_soma([itr.get("bpp"), dfp.get("bpp")], ["EMPRESTIMOS E FINANCIAMENTOS"])
    divida_liquida = (divida_bruta - caixa) if (divida_bruta is not None and caixa is not None) else None

    ebit, _ = pegar([itr.get("dre"), dfp.get("dre")], ["ANTES DO RESULTADO FINANCEIRO"])
    dep_amort, _ = pegar([itr_dfc, dfp_dfc], ["DEPRECIACAO"])
    ebitda_aprox = (ebit + dep_amort) if (ebit is not None and dep_amort is not None) else None
    divida_liquida_ebitda = (
        round(divida_liquida / ebitda_aprox, 2) if (divida_liquida is not None and ebitda_aprox) else None
    )

    caixa_operacional, dt_cfo = pegar([itr_dfc, dfp_dfc], ["CAIXA LIQUIDO", "OPERACIONAIS"])
    caixa_investimento, _ = pegar([itr_dfc, dfp_dfc], ["CAIXA LIQUIDO", "INVESTIMENTO"])
    caixa_financiamento, _ = pegar([itr_dfc, dfp_dfc], ["CAIXA LIQUIDO", "FINANCIAMENTO"])
    capex_imob, _ = pegar_soma([itr_dfc, dfp_dfc], ["IMOBILIZADO"])
    capex_intang, _ = pegar_soma([itr_dfc, dfp_dfc], ["INTANGIVEL"])
    capex = None
    if capex_imob is not None or capex_intang is not None:
        capex = (capex_imob or 0) + (capex_intang or 0)
    fcf_aprox = (caixa_operacional + capex) if (caixa_operacional is not None and capex is not None) else None

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
        "divida": {
            "bruta": divida_bruta,
            "caixa_e_equivalentes": caixa,
            "liquida": divida_liquida,
            "data_referencia": dt_divida,
        },
        "resultado_operacional": {
            "ebit": ebit,
            # aproximação (EBIT + D&A) — CVM não tem conta nativa de EBITDA,
            # ver comentário no topo do arquivo e em montar_historico.
            "ebitda_aprox": ebitda_aprox,
            "margem_ebitda_aprox": round(ebitda_aprox / receita, 4) if (ebitda_aprox is not None and receita) else None,
        },
        "fluxo_de_caixa": {
            "operacional": caixa_operacional,
            "investimento": caixa_investimento,
            "financiamento": caixa_financiamento,
            "capex_aprox": capex,
            "fcf_aprox": fcf_aprox,
            "data_referencia": dt_cfo,
        },
        "multiplos": {
            "p_l": round(p_l, 2) if p_l else None,
            "p_vp": round(p_vp, 2) if p_vp else None,
            "margem_liquida": round(margem_liquida, 4) if margem_liquida is not None else None,
            "roe": round(roe, 4) if roe is not None else None,
            "dividend_yield_12m": dividend_yield_12m,
            "divida_liquida_ebitda": divida_liquida_ebitda,
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
