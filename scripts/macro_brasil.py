import csv
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


TIMEZONE = ZoneInfo("America/Sao_Paulo")

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"

JSON_FILE = DATA_DIR / "macro_brasil.json"
CSV_FILE = DATA_DIR / "macro_brasil.csv"
HISTORY_FILE = DATA_DIR / "historico_macro_brasil.csv"


BCB_BASE_URL = (
    "https://api.bcb.gov.br/dados/serie/"
    "bcdata.sgs.{serie}/dados/ultimos/1?formato=json"
)


URLS = {
    "ipca_12m": (
        "https://apisidra.ibge.gov.br/values/"
        "t/1737/n1/all/v/2265/p/last%201"
    ),

    "pib_qoq": (
        "https://apisidra.ibge.gov.br/values/"
        "t/5932/n1/all/v/6564/p/last%201/"
        "c11255/90707/d/v6564%201"
    ),

    "desemprego": (
        "https://apisidra.ibge.gov.br/values/"
        "t/6381/n1/all/v/4099/p/last%201"
    ),
}


def criar_session():
    session = requests.Session()

    retry = Retry(
        total=4,
        backoff_factor=2,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )

    adapter = HTTPAdapter(max_retries=retry)

    session.mount("https://", adapter)
    session.mount("http://", adapter)

    session.headers.update({
        "User-Agent": "Macro-Brasil-GitHub-Action/1.0"
    })

    return session


SESSION = criar_session()


def numero(valor):
    """
    Converte valores numéricos vindos das APIs.
    """

    if valor is None:
        raise ValueError("Valor vazio recebido da API.")

    valor = str(valor).strip()

    invalidos = {
        "",
        "..",
        "...",
        "-",
        "x",
        "X",
    }

    if valor in invalidos:
        raise ValueError(f"Valor inválido recebido: {valor}")

    return float(valor.replace(",", "."))


def buscar_periodo_sidra(header, registro, termos):
    """
    Localiza dinamicamente a dimensão de período retornada pelo SIDRA.

    Exemplos:
    Mês
    Trimestre
    Trimestre móvel
    """

    for chave, descricao in header.items():

        if not chave.endswith("N"):
            continue

        descricao = str(descricao).lower()

        if any(termo.lower() in descricao for termo in termos):
            return registro.get(chave)

    return None


def buscar_sidra(url, termos_periodo):
    response = SESSION.get(url, timeout=30)
    response.raise_for_status()

    dados = response.json()

    if not isinstance(dados, list) or len(dados) < 2:
        raise RuntimeError(
            f"Resposta inesperada do SIDRA: {url}"
        )

    # Primeiro registro contém os nomes das colunas.
    header = dados[0]

    registros_validos = []

    for registro in dados[1:]:

        try:
            valor = numero(registro.get("V"))

            registros_validos.append(
                (registro, valor)
            )

        except ValueError:
            continue

    if not registros_validos:
        raise RuntimeError(
            f"Nenhum valor válido retornado pelo SIDRA: {url}"
        )

    registro, valor = registros_validos[-1]

    periodo = buscar_periodo_sidra(
        header,
        registro,
        termos_periodo,
    )

    unidade = registro.get("MN")

    return {
        "valor": valor,
        "periodo": periodo,
        "unidade_original": unidade,
    }


def buscar_bcb(serie):
    url = BCB_BASE_URL.format(serie=serie)

    response = SESSION.get(url, timeout=30)
    response.raise_for_status()

    dados = response.json()

    if not dados:
        raise RuntimeError(
            f"Nenhum dado retornado pelo BCB para a série {serie}"
        )

    registro = dados[-1]

    return {
        "valor": numero(registro["valor"]),
        "periodo": registro["data"],
        "url": url,
    }


def coletar_dados():

    agora = datetime.now(TIMEZONE)

    print("Buscando IPCA...")
    ipca = buscar_sidra(
        URLS["ipca_12m"],
        ["mês"],
    )

    print("Buscando Selic...")
    selic = buscar_bcb(432)

    print("Buscando PIB...")
    pib = buscar_sidra(
        URLS["pib_qoq"],
        ["trimestre"],
    )

    print("Buscando desemprego...")
    desemprego = buscar_sidra(
        URLS["desemprego"],
        ["trimestre"],
    )

    print("Buscando Dívida/PIB...")
    divida = buscar_bcb(13762)

    dados = {

        "pais": "Brasil",

        "coletado_em": agora.isoformat(),

        "timezone": "America/Sao_Paulo",

        "indicadores": {

            "inflacao": {
                "nome": "IPCA acumulado em 12 meses",
                "valor": ipca["valor"],
                "unidade": "%",
                "periodo": ipca["periodo"],
                "fonte": "IBGE / SIDRA",
                "codigo": "Tabela 1737 - variável 2265",
                "api": URLS["ipca_12m"],
            },

            "selic": {
                "nome": "Meta Selic",
                "valor": selic["valor"],
                "unidade": "% a.a.",
                "periodo": selic["periodo"],
                "fonte": "Banco Central do Brasil",
                "codigo": "SGS 432",
                "api": selic["url"],
            },

            "pib": {
                "nome": (
                    "PIB - variação contra o trimestre "
                    "imediatamente anterior"
                ),
                "valor": pib["valor"],
                "unidade": "%",
                "periodo": pib["periodo"],
                "fonte": "IBGE / SIDRA",
                "codigo": (
                    "Tabela 5932 - variável 6564 - "
                    "PIB a preços de mercado"
                ),
                "api": URLS["pib_qoq"],
            },

            "desemprego": {
                "nome": "Taxa de desemprego",
                "valor": desemprego["valor"],
                "unidade": "%",
                "periodo": desemprego["periodo"],
                "fonte": "IBGE / PNAD Contínua",
                "codigo": "Tabela 6381 - variável 4099",
                "api": URLS["desemprego"],
            },

            "divida_pib": {
                "nome": (
                    "Dívida Bruta do Governo Geral / PIB"
                ),
                "valor": divida["valor"],
                "unidade": "% do PIB",
                "periodo": divida["periodo"],
                "fonte": "Banco Central do Brasil",
                "codigo": "SGS 13762",
                "api": divida["url"],
            },
        },
    }

    return dados


def salvar_json(dados):

    DATA_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    with open(
        JSON_FILE,
        "w",
        encoding="utf-8",
    ) as arquivo:

        json.dump(
            dados,
            arquivo,
            ensure_ascii=False,
            indent=2,
        )

    print(f"JSON salvo: {JSON_FILE}")


def salvar_csv(dados):

    campos = [
        "indicador",
        "nome",
        "valor",
        "unidade",
        "periodo",
        "fonte",
        "codigo",
        "coletado_em",
    ]

    with open(
        CSV_FILE,
        "w",
        newline="",
        encoding="utf-8-sig",
    ) as arquivo:

        writer = csv.DictWriter(
            arquivo,
            fieldnames=campos,
        )

        writer.writeheader()

        for indicador, info in dados["indicadores"].items():

            writer.writerow({
                "indicador": indicador,
                "nome": info["nome"],
                "valor": info["valor"],
                "unidade": info["unidade"],
                "periodo": info["periodo"],
                "fonte": info["fonte"],
                "codigo": info["codigo"],
                "coletado_em": dados["coletado_em"],
            })

    print(f"CSV salvo: {CSV_FILE}")


def atualizar_historico(dados):
    """
    Mantém uma fotografia diária dos indicadores.

    Caso o workflow seja executado novamente no mesmo dia,
    substituímos a observação daquele dia em vez de duplicá-la.
    """

    data_coleta = datetime.fromisoformat(
        dados["coletado_em"]
    ).strftime("%Y-%m-%d")

    indicadores = dados["indicadores"]

    nova_linha = {

        "data_coleta": data_coleta,

        "coletado_em": dados["coletado_em"],

        "ipca_12m": indicadores["inflacao"]["valor"],
        "ipca_periodo": indicadores["inflacao"]["periodo"],

        "selic": indicadores["selic"]["valor"],
        "selic_periodo": indicadores["selic"]["periodo"],

        "pib_qoq": indicadores["pib"]["valor"],
        "pib_periodo": indicadores["pib"]["periodo"],

        "desemprego": indicadores["desemprego"]["valor"],
        "desemprego_periodo": indicadores["desemprego"]["periodo"],

        "divida_pib": indicadores["divida_pib"]["valor"],
        "divida_periodo": indicadores["divida_pib"]["periodo"],
    }

    campos = list(nova_linha.keys())

    linhas = []

    if HISTORY_FILE.exists():

        with open(
            HISTORY_FILE,
            "r",
            encoding="utf-8-sig",
        ) as arquivo:

            reader = csv.DictReader(arquivo)

            for linha in reader:

                # Não duplicar o mesmo dia
                if linha.get("data_coleta") != data_coleta:
                    linhas.append(linha)

    linhas.append(nova_linha)

    with open(
        HISTORY_FILE,
        "w",
        newline="",
        encoding="utf-8-sig",
    ) as arquivo:

        writer = csv.DictWriter(
            arquivo,
            fieldnames=campos,
        )

        writer.writeheader()
        writer.writerows(linhas)

    print(
        f"Histórico atualizado: {HISTORY_FILE}"
    )


def mostrar_resumo(dados):

    print()
    print("=" * 60)
    print("MACRO BRASIL")
    print("=" * 60)

    for info in dados["indicadores"].values():

        print(
            f"{info['nome']}: "
            f"{info['valor']} {info['unidade']} "
            f"({info['periodo']})"
        )

    print("=" * 60)


def main():

    try:

        dados = coletar_dados()

        salvar_json(dados)
        salvar_csv(dados)
        atualizar_historico(dados)

        mostrar_resumo(dados)

    except Exception as erro:

        print()
        print("ERRO NA ATUALIZAÇÃO DO MACRO BRASIL")
        print(str(erro))

        raise


if __name__ == "__main__":
    main()
