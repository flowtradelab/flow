"""
Fundamentos CVM — Data Updater
Roda semanalmente via GitHub Actions.

Baixa os dados fundamentalistas (DFP e ITR) de TODAS as companhias abertas
direto do Portal de Dados Abertos da CVM e versiona os CSVs no repositório.

Fonte oficial:
  https://dados.cvm.gov.br/dataset/cia_aberta-doc-dfp   (Demonstrações Financeiras Padronizadas — anual)
  https://dados.cvm.gov.br/dataset/cia_aberta-doc-itr   (Informações Trimestrais — trimestral)
  https://dados.cvm.gov.br/dataset/cia_aberta-cad       (Cadastro de companhias abertas)

Observação: a CVM identifica as empresas por CNPJ/CD_CVM, não pelo ticker da
B3 (ex: PETR4). O arquivo cadastro_cias.csv sai junto pra facilitar esse
cruzamento depois (por razão social/CNPJ), caso você queira montar uma view
por ticker mais pra frente.
"""

import io
import os
import zipfile
from datetime import date

import pandas as pd
import requests

BASE_DFP = "https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/DFP/DADOS"
BASE_ITR = "https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/ITR/DADOS"
BASE_CAD = "https://dados.cvm.gov.br/dados/CIA_ABERTA/CAD/DADOS"

OUT_DIR = os.path.join(os.path.dirname(__file__), "..", "fundamentos", "data")
os.makedirs(OUT_DIR, exist_ok=True)

CURRENT_YEAR = date.today().year
# Cobre o ano corrente + os 2 anteriores — garante que companhias que ainda
# não entregaram o demonstrativo do ano corrente não fiquem sem dado, e que
# a série histórica vá se acumulando a cada rodada.
YEARS = [CURRENT_YEAR, CURRENT_YEAR - 1, CURRENT_YEAR - 2]

# Demonstrativos baixados de cada pacote (sigla usada nos nomes dos CSVs da CVM)
STATEMENTS = {
    "bpa": "BPA",  # Balanço Patrimonial Ativo
    "bpp": "BPP",  # Balanço Patrimonial Passivo
    "dre": "DRE",  # Demonstração do Resultado
}

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
            resultado[chave] = df_full
    return resultado


def salvar(df: pd.DataFrame, caminho: str):
    df = df.sort_values(["CNPJ_CIA", "DT_FIM_EXERC", "CD_CONTA"])
    df.to_csv(caminho, index=False)
    print(f"  ✅ {caminho} ({len(df)} linhas)")


def montar_serie(prefixo: str, base_url: str, chave: str) -> pd.DataFrame | None:
    partes = []
    for ano in YEARS:
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


def main():
    for chave in STATEMENTS:
        df_dfp = montar_serie("dfp_cia_aberta", BASE_DFP, chave)
        if df_dfp is not None:
            salvar(df_dfp, os.path.join(OUT_DIR, f"dfp_{chave}.csv"))

        df_itr = montar_serie("itr_cia_aberta", BASE_ITR, chave)
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


if __name__ == "__main__":
    main()
