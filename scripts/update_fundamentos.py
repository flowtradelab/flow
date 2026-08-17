"""
Fundamentos CVM — Data Updater
Roda semanalmente via GitHub Actions.

Baixa os dados fundamentalistas (DFP e ITR) de TODAS as companhias abertas
direto do Portal de Dados Abertos da CVM e versiona os CSVs no repositório.

Fonte oficial:
  https://dados.cvm.gov.br/dataset/cia_aberta-doc-dfp   (Demonstrações Financeiras Padronizadas — anual)
  https://dados.cvm.gov.br/dataset/cia_aberta-doc-itr   (Informações Trimestrais — trimestral)
  https://dados.cvm.gov.br/dataset/cia_aberta-cad       (Cadastro de companhias abertas)
  https://dados.cvm.gov.br/dataset/cia_aberta-doc-fca   (Formulário Cadastral — traz o ticker/código
                                                          de negociação de cada valor mobiliário)

Observação: a CVM identifica as empresas por CNPJ/CD_CVM, não pelo ticker da
B3 (ex: PETR4). Por isso este script também baixa o FCA e monta
tickers_cnpj.csv — o de-para necessário pra cruzar isso com o preço que já
mora em quant/{TICKER}/ neste repositório (ver update_fundamentos_view.py).

⚠️ O nome exato das colunas do FCA (fca_cia_aberta_valor_mobiliario_AAAA.csv)
não foi validado ao vivo na hora que este script foi escrito — o parser abaixo
procura por colunas que contenham "NEGOCIACAO" e "CNPJ" em vez de fixar nomes
exatos, exatamente pra tolerar pequenas variações. Se o primeiro run não
gerar tickers_cnpj.csv, veja no log do Action quais colunas ele encontrou.
"""

import io
import os
import unicodedata
import zipfile
from datetime import date

import pandas as pd
import requests

BASE_DFP = "https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/DFP/DADOS"
BASE_ITR = "https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/ITR/DADOS"
BASE_CAD = "https://dados.cvm.gov.br/dados/CIA_ABERTA/CAD/DADOS"
BASE_FCA = "https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/FCA/DADOS"

OUT_DIR = os.path.join(os.path.dirname(__file__), "..", "fundamentos", "data")
os.makedirs(OUT_DIR, exist_ok=True)

CURRENT_YEAR = date.today().year
# DFP é anual — 3 exercícios dá margem sem pesar muito (poucos períodos por empresa).
YEARS_DFP = [CURRENT_YEAR, CURRENT_YEAR - 1, CURRENT_YEAR - 2]
# ITR é trimestral — 3 anos vira até 12 períodos por empresa, o que já foi
# grande o suficiente pra estourar o limite de arquivo do GitHub (ver
# MAX_PROFUNDIDADE abaixo). 2 anos (até 8 trimestres) é suficiente pra
# qualquer indicador TTM e mantém o arquivo num tamanho razoável.
YEARS_ITR = [CURRENT_YEAR, CURRENT_YEAR - 1]

# Demonstrativos baixados de cada pacote (sigla usada nos nomes dos CSVs da CVM)
STATEMENTS = {
    "bpa": "BPA",        # Balanço Patrimonial Ativo
    "bpp": "BPP",        # Balanço Patrimonial Passivo
    "dre": "DRE",        # Demonstração do Resultado
    "dfc_md": "DFC_MD",  # Fluxo de Caixa — Método Direto
    "dfc_mi": "DFC_MI",  # Fluxo de Caixa — Método Indireto
}

# A CVM entrega cada demonstrativo com TODAS as subcontas (inclusive notas
# explicativas bem detalhadas) — isso é o que fez os CSVs passarem de 100MB
# e o GitHub recusar o push. CD_CONTA usa pontos pra indicar profundidade
# ("1" = Ativo Total, "1.01" = Ativo Circulante, "1.01.01.02" = uma nota bem
# específica). Ficando só até profundidade 1 (ex: "1", "1.01", "2.03", "3.01",
# "3.11") a gente mantém as linhas-síntese que a view por ticker usa e os
# principais subtotais, cortando a maior parte do volume (é onde mora quase
# toda a explosão de linhas: notas explicativas bem detalhadas).
MAX_PROFUNDIDADE = 1

TIMEOUT = 60


def baixar_zip(url: str):
    try:
        r = requests.get(url, timeout=TIMEOUT)
        r.raise_for_status()
        return zipfile.ZipFile(io.BytesIO(r.content))
    except Exception as e:
        print(f"  ⚠️  não consegui baixar {url}: {e}")
        return None


def ler_csv_do_zip(zf: zipfile.ZipFile, nome: str):
    if nome not in zf.namelist():
        return None
    with zf.open(nome) as f:
        return pd.read_csv(f, sep=";", encoding="latin1", decimal=",", dtype=str)


def processar_pacote(prefixo: str, base_url: str, ano: int) -> dict:
    """Baixa o zip de um ano (DFP ou ITR) e devolve {chave: DataFrame} filtrado."""
    url = f"{base_url}/{prefixo}_{ano}.zip"
    print(f"→ baixando {url}")
    zf = baixar_zip(url)
    if zf is None:
        return {}

    resultado = {}
    for chave, sigla in STATEMENTS.items():
        partes = []
        for tipo in ("con", "ind"):  # consolidado e individual
            nome = f"{prefixo}_{sigla}_{tipo}_{ano}.csv"
            df = ler_csv_do_zip(zf, nome)
            if df is None:
                continue
            df["TIPO_DEMONSTRATIVO"] = tipo
            partes.append(df)
        if partes:
            df_full = pd.concat(partes, ignore_index=True)
            # a CVM traz o exercício atual e o anterior lado a lado (pra
            # comparação); ficamos só com o mais recente de cada um pra não
            # duplicar período já coberto pelo zip do ano anterior.
            if "ORDEM_EXERC" in df_full.columns:
                df_full = df_full[df_full["ORDEM_EXERC"] == "ÚLTIMO"]
            # descarta subcontas profundas — ver MAX_PROFUNDIDADE acima.
            if "CD_CONTA" in df_full.columns:
                profundidade = df_full["CD_CONTA"].astype(str).str.count(r"\.")
                df_full = df_full[profundidade <= MAX_PROFUNDIDADE]
            resultado[chave] = df_full
    return resultado


def salvar(df: pd.DataFrame, caminho: str):
    df = df.sort_values(["CNPJ_CIA", "DT_FIM_EXERC", "CD_CONTA"])
    df.to_csv(caminho, index=False)
    tamanho_mb = os.path.getsize(caminho) / (1024 * 1024)
    aviso = "  ⚠️ arquivo grande, de olho no limite do GitHub (100MB)" if tamanho_mb > 20 else ""
    print(f"  ✅ {caminho} ({len(df)} linhas, {tamanho_mb:.1f} MB){aviso}")


def montar_serie(prefixo: str, base_url: str, chave: str, anos: list[int]) -> pd.DataFrame | None:
    partes = []
    for ano in anos:
        pacote = processar_pacote(prefixo, base_url, ano)
        if chave in pacote:
            partes.append(pacote[chave])
    if not partes:
        return None
    df = pd.concat(partes, ignore_index=True)
    # se uma empresa retificou um demonstrativo, fica só com a entrega mais
    # recente (maior DT_REFER) pra cada período/conta.
    df = df.sort_values("DT_REFER").drop_duplicates(
        subset=["CNPJ_CIA", "DT_FIM_EXERC", "CD_CONTA", "TIPO_DEMONSTRATIVO"],
        keep="last",
    )
    return df


def normalizar(txt: str) -> str:
    """Maiúsculas e sem acento, pra comparar nomes de coluna com tolerância."""
    if not isinstance(txt, str):
        return ""
    sem_acento = unicodedata.normalize("NFKD", txt).encode("ascii", "ignore").decode()
    return sem_acento.upper()


def montar_tickers_cnpj() -> pd.DataFrame | None:
    """
    Baixa o FCA (Formulário Cadastral) e extrai o de-para ticker B3 ↔ CNPJ,
    a partir do resource "valor_mobiliario" de cada pacote anual.
    """
    partes = []
    for ano in YEARS_DFP:
        url = f"{BASE_FCA}/fca_cia_aberta_{ano}.zip"
        print(f"→ baixando {url}")
        zf = baixar_zip(url)
        if zf is None:
            continue

        # procura, dentro do zip, o csv de valores mobiliários (nome pode
        # variar um pouco entre anos — por isso a busca é por padrão, não
        # por nome fixo)
        candidatos = [
            n for n in zf.namelist()
            if "VALOR_MOBILIARIO" in normalizar(n) and n.lower().endswith(".csv")
        ]
        if not candidatos:
            print(f"  ⚠️  não achei o csv de valores mobiliários dentro de {url}")
            print(f"     arquivos disponíveis: {zf.namelist()}")
            continue

        df = ler_csv_do_zip(zf, candidatos[0])
        if df is None:
            continue

        # localiza as colunas de ticker e CNPJ por conteúdo do nome, não por
        # nome exato (schema da CVM já mudou de leiaute algumas vezes)
        col_ticker = next((c for c in df.columns if "NEGOCIACAO" in normalizar(c)), None)
        col_cnpj = next((c for c in df.columns if "CNPJ" in normalizar(c)), None)

        if not col_ticker or not col_cnpj:
            print(f"  ⚠️  não achei as colunas esperadas em {candidatos[0]}")
            print(f"     colunas encontradas: {list(df.columns)}")
            continue

        sub = df[[col_cnpj, col_ticker]].dropna()
        sub.columns = ["CNPJ", "TICKER"]
        sub["TICKER"] = sub["TICKER"].str.strip().str.upper()
        sub["CNPJ"] = sub["CNPJ"].str.replace(r"\D", "", regex=True)  # só dígitos
        sub = sub[sub["TICKER"].str.len() >= 4]  # descarta linhas vazias/lixo
        partes.append(sub)

    if not partes:
        return None

    tickers = pd.concat(partes, ignore_index=True).drop_duplicates(
        subset=["TICKER"], keep="last"
    )
    return tickers.sort_values("TICKER")


def main():
    for chave in STATEMENTS:
        df_dfp = montar_serie("dfp_cia_aberta", BASE_DFP, chave, YEARS_DFP)
        if df_dfp is not None:
            salvar(df_dfp, os.path.join(OUT_DIR, f"dfp_{chave}.csv"))

        df_itr = montar_serie("itr_cia_aberta", BASE_ITR, chave, YEARS_ITR)
        if df_itr is not None:
            salvar(df_itr, os.path.join(OUT_DIR, f"itr_{chave}.csv"))

    # Cadastro de companhias abertas (CNPJ, razão social, situação etc.) —
    # útil pra cruzar com o ticker da B3 depois.
    try:
        url_cad = f"{BASE_CAD}/cad_cia_aberta.csv"
        print(f"→ baixando {url_cad}")
        cad = pd.read_csv(url_cad, sep=";", encoding="latin1", dtype=str)
        salvar(cad, os.path.join(OUT_DIR, "cadastro_cias.csv"))
    except Exception as e:
        print(f"  ⚠️  não consegui baixar o cadastro de companhias: {e}")

    # Ponte ticker (B3) ↔ CNPJ — necessária pra cruzar com quant/{TICKER}/
    tickers_cnpj = montar_tickers_cnpj()
    if tickers_cnpj is not None:
        salvar_simples = os.path.join(OUT_DIR, "tickers_cnpj.csv")
        tickers_cnpj.to_csv(salvar_simples, index=False)
        print(f"  ✅ {salvar_simples} ({len(tickers_cnpj)} tickers)")
    else:
        print("  ⚠️  não consegui montar tickers_cnpj.csv — a view por ticker "
              "(update_fundamentos_view.py) vai ficar sem dado até isso ser corrigido.")


if __name__ == "__main__":
    main()
