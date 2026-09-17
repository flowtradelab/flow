import csv
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


# ============================================================
# CONFIGURAÇÃO
# ============================================================

TIMEZONE = ZoneInfo("America/Sao_Paulo")

# Este arquivo deve ficar em:
#   scripts/macro_brasil.py
#
# Assim, parent.parent aponta para a raiz do repositório.
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"

JSON_FILE = DATA_DIR / "macro_brasil.json"
CSV_FILE = DATA_DIR / "macro_brasil.csv"
HISTORY_FILE = DATA_DIR / "historico_macro_brasil.csv"


# ============================================================
# FONTES OFICIAIS
# ============================================================

# Banco Central do Brasil - SGS
BCB_BASE_URL = (
    "https://api.bcb.gov.br/dados/serie/"
    "bcdata.sgs.{serie}/dados/ultimos/1?formato=json"
)

# IBGE - API v3 de Agregados
#
# -1 = último período disponível.
#
# IPCA 12 meses:
#   agregado 1737 / variável 2265
#
# PIB trimestral contra trimestre imediatamente anterior:
#   agregado 5932 / variável 6564
#   classificação 11255 / categoria 90707 = PIB a preços de mercado
#
# Desemprego:
#   agregado 6381 / variável 4099
IBGE_URLS = {
    "ipca_12m": (
        "https://servicodados.ibge.gov.br/api/v3/"
        "agregados/1737/periodos/-1/variaveis/2265"
        "?localidades=N1[1]"
    ),
    "pib_qoq": (
        "https://servicodados.ibge.gov.br/api/v3/"
        "agregados/5932/periodos/-1/variaveis/6564"
        "?localidades=N1[1]"
        "&classificacao=11255[90707]"
    ),
    "desemprego": (
        "https://servicodados.ibge.gov.br/api/v3/"
        "agregados/6381/periodos/-1/variaveis/4099"
        "?localidades=N1[1]"
    ),
}


# ============================================================
# HTTP / RETRIES
# ============================================================

def criar_session():
    """
    Cria uma sessão HTTP com retry automático para erros temporários.
    """
    session = requests.Session()

    retry = Retry(
        total=5,
        connect=5,
        read=5,
        status=5,
        backoff_factor=2,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=frozenset(["GET"]),
        raise_on_status=False,
    )

    adapter = HTTPAdapter(max_retries=retry)

    session.mount("https://", adapter)
    session.mount("http://", adapter)

    session.headers.update({
        "User-Agent": (
            "Macro-Brasil-GitHub-Action/2.0 "
            "(coleta automatizada de dados publicos oficiais)"
        ),
        "Accept": "application/json",
    })

    return session


SESSION = criar_session()


# ============================================================
# UTILITÁRIOS
# ============================================================

def numero(valor):
    """
    Converte valores numéricos recebidos das APIs para float.

    Aceita ponto ou vírgula como separador decimal.
    """
    if valor is None:
        raise ValueError("Valor vazio recebido da API.")

    texto = str(valor).strip()

    invalidos = {
        "",
        "..",
        "...",
        "-",
        "x",
        "X",
        "null",
        "None",
    }

    if texto in invalidos:
        raise ValueError(f"Valor inválido recebido: {texto}")

    return float(texto.replace(",", "."))


def get_json(url, nome_fonte):
    """
    Executa GET e devolve JSON, com mensagem de erro mais clara.
    """
    response = SESSION.get(url, timeout=45)

    if not response.ok:
        trecho = response.text[:300].replace("\n", " ")
        raise RuntimeError(
            f"{nome_fonte} retornou HTTP {response.status_code}. "
            f"URL: {url}. Resposta: {trecho}"
        )

    try:
        return response.json()
    except ValueError as erro:
        trecho = response.text[:300].replace("\n", " ")
        raise RuntimeError(
            f"{nome_fonte} não retornou JSON válido. "
            f"URL: {url}. Resposta: {trecho}"
        ) from erro


# ============================================================
# IBGE
# ============================================================

def buscar_ibge(url):
    """
    Busca o último valor disponível usando a API v3 de Agregados do IBGE.

    Formato esperado:
    [
      {
        "id": "...",
        "variavel": "...",
        "unidade": "...",
        "resultados": [
          {
            "series": [
              {
                "localidade": {...},
                "serie": {
                  "PERIODO": "VALOR"
                }
              }
            ]
          }
        ]
      }
    ]
    """
    dados = get_json(url, "IBGE")

    if not isinstance(dados, list) or not dados:
        raise RuntimeError(
            f"Resposta inesperada do IBGE. URL: {url}"
        )

    variavel = dados[0]

    if not isinstance(variavel, dict):
        raise RuntimeError(
            f"Formato inesperado na variável do IBGE. URL: {url}"
        )

    resultados = variavel.get("resultados") or []

    candidatos = []

    for resultado in resultados:
        if not isinstance(resultado, dict):
            continue

        series = resultado.get("series") or []

        for serie_info in series:
            if not isinstance(serie_info, dict):
                continue

            localidade = serie_info.get("localidade") or {}
            serie = serie_info.get("serie") or {}

            if not isinstance(serie, dict):
                continue

            for periodo, valor_bruto in serie.items():
                try:
                    valor = numero(valor_bruto)
                except (ValueError, TypeError):
                    continue

                candidatos.append({
                    "periodo": str(periodo),
                    "valor": valor,
                    "localidade": localidade.get("nome", "Brasil")
                    if isinstance(localidade, dict)
                    else "Brasil",
                })

    if not candidatos:
        raise RuntimeError(
            f"Nenhum valor numérico válido encontrado no IBGE. URL: {url}"
        )

    # Como a chamada usa periodos/-1, normalmente haverá apenas um período.
    # Mesmo assim, ordenamos pelo código do período e usamos o mais recente.
    candidatos.sort(key=lambda item: item["periodo"])
    ultimo = candidatos[-1]

    return {
        "valor": ultimo["valor"],
        "periodo": ultimo["periodo"],
        "unidade_original": variavel.get("unidade"),
        "variavel_original": variavel.get("variavel"),
        "localidade": ultimo["localidade"],
        "url": url,
    }


# ============================================================
# BANCO CENTRAL
# ============================================================

def buscar_bcb(serie):
    """
    Busca o último valor disponível de uma série SGS do Banco Central.
    """
    url = BCB_BASE_URL.format(serie=serie)

    dados = get_json(url, f"Banco Central - série {serie}")

    if not isinstance(dados, list) or not dados:
        raise RuntimeError(
            f"Nenhum dado retornado pelo Banco Central para a série {serie}."
        )

    registro = dados[-1]

    if not isinstance(registro, dict):
        raise RuntimeError(
            f"Formato inesperado do Banco Central para a série {serie}."
        )

    if "valor" not in registro or "data" not in registro:
        raise RuntimeError(
            f"Banco Central não retornou os campos esperados "
            f"para a série {serie}: {registro}"
        )

    return {
        "valor": numero(registro["valor"]),
        "periodo": registro["data"],
        "url": url,
    }


# ============================================================
# COLETA
# ============================================================

def coletar_dados():
    agora = datetime.now(TIMEZONE)

    print("Buscando IPCA 12 meses no IBGE...")
    ipca = buscar_ibge(IBGE_URLS["ipca_12m"])

    print("Buscando Meta Selic no Banco Central...")
    selic = buscar_bcb(432)

    print("Buscando PIB trimestral no IBGE...")
    pib = buscar_ibge(IBGE_URLS["pib_qoq"])

    print("Buscando taxa de desemprego no IBGE...")
    desemprego = buscar_ibge(IBGE_URLS["desemprego"])

    print("Buscando Dívida Bruta/PIB no Banco Central...")
    divida = buscar_bcb(13762)

    return {
        "pais": "Brasil",
        "coletado_em": agora.isoformat(),
        "timezone": "America/Sao_Paulo",
        "indicadores": {
            "inflacao": {
                "nome": "IPCA acumulado em 12 meses",
                "valor": ipca["valor"],
                "unidade": "%",
                "periodo": ipca["periodo"],
                "fonte": "IBGE",
                "codigo": "Agregado 1737 - variável 2265",
                "api": ipca["url"],
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
                "fonte": "IBGE",
                "codigo": (
                    "Agregado 5932 - variável 6564 - "
                    "classificação 11255 / categoria 90707"
                ),
                "api": pib["url"],
            },
            "desemprego": {
                "nome": "Taxa de desocupação",
                "valor": desemprego["valor"],
                "unidade": "%",
                "periodo": desemprego["periodo"],
                "fonte": "IBGE - PNAD Contínua",
                "codigo": "Agregado 6381 - variável 4099",
                "api": desemprego["url"],
            },
            "divida_pib": {
                "nome": "Dívida Bruta do Governo Geral / PIB",
                "valor": divida["valor"],
                "unidade": "% do PIB",
                "periodo": divida["periodo"],
                "fonte": "Banco Central do Brasil",
                "codigo": "SGS 13762",
                "api": divida["url"],
            },
        },
    }


# ============================================================
# ARQUIVOS
# ============================================================

def salvar_json(dados):
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    with JSON_FILE.open("w", encoding="utf-8") as arquivo:
        json.dump(
            dados,
            arquivo,
            ensure_ascii=False,
            indent=2,
        )

    print(f"JSON salvo em: {JSON_FILE}")


def salvar_csv(dados):
    DATA_DIR.mkdir(parents=True, exist_ok=True)

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

    with CSV_FILE.open(
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

    print(f"CSV salvo em: {CSV_FILE}")


def atualizar_historico(dados):
    """
    Guarda uma fotografia diária dos indicadores.

    Se a rotina for executada mais de uma vez no mesmo dia,
    a fotografia anterior daquele dia é substituída.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)

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
        with HISTORY_FILE.open(
            "r",
            encoding="utf-8-sig",
            newline="",
        ) as arquivo:
            reader = csv.DictReader(arquivo)

            for linha in reader:
                if linha.get("data_coleta") != data_coleta:
                    # Mantém somente as colunas atuais do arquivo.
                    linhas.append({
                        campo: linha.get(campo, "")
                        for campo in campos
                    })

    linhas.append(nova_linha)

    with HISTORY_FILE.open(
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

    print(f"Histórico atualizado em: {HISTORY_FILE}")


# ============================================================
# LOG
# ============================================================

def mostrar_resumo(dados):
    print()
    print("=" * 72)
    print("MACRO BRASIL")
    print("=" * 72)

    for info in dados["indicadores"].values():
        print(
            f"{info['nome']}: "
            f"{info['valor']} {info['unidade']} "
            f"(período: {info['periodo']})"
        )

    print("=" * 72)
    print(f"Coletado em: {dados['coletado_em']}")
    print()


# ============================================================
# MAIN
# ============================================================

def main():
    try:
        dados = coletar_dados()

        # Os arquivos só são gravados depois que TODAS as fontes
        # responderem corretamente. Isso evita salvar atualização parcial.
        salvar_json(dados)
        salvar_csv(dados)
        atualizar_historico(dados)

        mostrar_resumo(dados)

    except Exception as erro:
        print()
        print("=" * 72)
        print("ERRO NA ATUALIZAÇÃO DO MACRO BRASIL")
        print("=" * 72)
        print(str(erro))
        print()

        raise


if __name__ == "__main__":
    main()
