import csv
import json
from datetime import date, datetime, timedelta
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

HISTORICO_DESDE = date(1995, 1, 1)
HISTORICO_CHUNK_ANOS = 9

# Focus: ano atual + próximos 3 anos
FOCUS_QUANTIDADE_ANOS = 4
FOCUS_TOP_POR_INDICADOR = 2000


# ============================================================
# SÉRIES MACRO - BANCO CENTRAL / SGS
# ============================================================

BCB_BASE = "https://api.bcb.gov.br/dados/serie/bcdata.sgs.{serie}"

SERIES = {
    "selic_meta": {
        "sgs": 432,
        "nome": "Selic Meta",
        "unidade": "% a.a.",
        "fonte": "Banco Central do Brasil",
        "historico": True,
    },
    "ipca_12m": {
        "sgs": 13522,
        "nome": "IPCA 12 meses",
        "unidade": "%",
        "fonte": "IBGE via Banco Central do Brasil",
        "historico": True,
    },
    "cdi": {
        "sgs": 4389,
        "nome": "CDI anualizado base 252",
        "unidade": "% a.a.",
        "fonte": "Banco Central do Brasil",
        "historico": True,
    },
    "dolar_ptax": {
        "sgs": 1,
        "nome": "Dólar PTAX venda",
        "unidade": "R$/US$",
        "fonte": "Banco Central do Brasil",
        "historico": True,
    },
    "euro_ptax": {
        "sgs": 21619,
        "nome": "Euro PTAX venda",
        "unidade": "R$/EUR",
        "fonte": "Banco Central do Brasil",
        "historico": False,
    },
    "igpm": {
        "sgs": 189,
        "nome": "IGP-M",
        "unidade": "% no mês",
        "fonte": "FGV via Banco Central do Brasil",
        "historico": True,
    },
    "reservas_internacionais": {
        "sgs": 13621,
        "nome": "Reservas internacionais",
        "unidade": "US$ milhões",
        "fonte": "Banco Central do Brasil",
        "historico": False,
    },
    "balanca_comercial": {
        "sgs": 22704,
        "nome": "Balança comercial",
        "unidade": "US$ milhões",
        "fonte": "Banco Central do Brasil",
        "historico": False,
    },
    "resultado_primario": {
        "sgs": 4649,
        "nome": "Resultado primário - setor público consolidado",
        "unidade": "R$ milhões",
        "fonte": "Banco Central do Brasil",
        "historico": False,
    },
    "divida_pib": {
        "sgs": 13762,
        "nome": "Dívida Bruta do Governo Geral / PIB",
        "unidade": "% do PIB",
        "fonte": "Banco Central do Brasil",
        "historico": False,
    },
}


# ============================================================
# FOCUS - EXPECTATIVAS DE MERCADO
# ============================================================

FOCUS_URL = (
    "https://olinda.bcb.gov.br/olinda/servico/"
    "Expectativas/versao/v1/odata/ExpectativasMercadoAnuais"
)

FOCUS_INDICADORES = {
    "ipca": {
        "api": "IPCA",
        "nome": "IPCA",
        "unidade": "%",
    },
    "selic": {
        "api": "Selic",
        "nome": "Selic",
        "unidade": "% a.a.",
    },
    "pib": {
        "api": "PIB Total",
        "nome": "PIB",
        "unidade": "%",
    },
    "cambio": {
        "api": "Câmbio",
        "nome": "Câmbio",
        "unidade": "R$/US$",
    },
}


# ============================================================
# HTTP / RETRIES
# ============================================================

def criar_session():
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
            "Macro-Brasil-GitHub-Action/3.0 "
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
    if valor is None:
        raise ValueError("Valor vazio recebido da API.")

    texto = str(valor).strip()

    if texto in {"", "..", "...", "-", "x", "X", "null", "None"}:
        raise ValueError(f"Valor inválido recebido: {texto}")

    return float(texto.replace(",", "."))


def numero_ou_none(valor):
    try:
        return numero(valor)
    except (ValueError, TypeError):
        return None


def inteiro_ou_none(valor):
    if valor is None:
        return None

    try:
        return int(valor)
    except (ValueError, TypeError):
        return None


def get_json(url, nome_fonte, params=None, timeout=60):
    response = SESSION.get(
        url,
        params=params,
        timeout=timeout,
    )

    if not response.ok:
        trecho = response.text[:500].replace("\n", " ")
        raise RuntimeError(
            f"{nome_fonte} retornou HTTP {response.status_code}. "
            f"URL final: {response.url}. Resposta: {trecho}"
        )

    try:
        return response.json()
    except ValueError as erro:
        trecho = response.text[:500].replace("\n", " ")
        raise RuntimeError(
            f"{nome_fonte} não retornou JSON válido. "
            f"URL final: {response.url}. Resposta: {trecho}"
        ) from erro


def parse_data_bcb(valor):
    return datetime.strptime(valor, "%d/%m/%Y").date()


def parse_data_focus(valor):
    return datetime.strptime(valor, "%Y-%m-%d").date()


# ============================================================
# BANCO CENTRAL - ÚLTIMO VALOR
# ============================================================

def buscar_bcb_ultimo(serie):
    url = (
        BCB_BASE.format(serie=serie)
        + "/dados/ultimos/1?formato=json"
    )

    dados = get_json(
        url,
        f"Banco Central - SGS {serie}",
    )

    if not isinstance(dados, list) or not dados:
        raise RuntimeError(
            f"Nenhum dado retornado pelo BCB para a série {serie}."
        )

    registro = dados[-1]

    if "data" not in registro or "valor" not in registro:
        raise RuntimeError(
            f"Resposta inesperada do BCB para a série {serie}: "
            f"{registro}"
        )

    return {
        "data": parse_data_bcb(registro["data"]).isoformat(),
        "valor": numero(registro["valor"]),
    }


# ============================================================
# BANCO CENTRAL - HISTÓRICO DESDE 1995
# ============================================================

def intervalos_historicos(inicio, fim):
    """
    Divide o período em blocos de até 9 anos.

    O BCB limita consultas JSON/CSV de séries diárias
    a períodos de no máximo 10 anos.
    """
    ano_inicio = inicio.year

    while ano_inicio <= fim.year:
        bloco_inicio = date(ano_inicio, 1, 1)
        bloco_fim = date(
            min(ano_inicio + HISTORICO_CHUNK_ANOS - 1, fim.year),
            12,
            31,
        )

        if bloco_inicio < inicio:
            bloco_inicio = inicio

        if bloco_fim > fim:
            bloco_fim = fim

        yield bloco_inicio, bloco_fim

        ano_inicio += HISTORICO_CHUNK_ANOS


def buscar_bcb_intervalo(serie, data_inicial, data_final):
    url = BCB_BASE.format(serie=serie) + "/dados"

    params = {
        "formato": "json",
        "dataInicial": data_inicial.strftime("%d/%m/%Y"),
        "dataFinal": data_final.strftime("%d/%m/%Y"),
    }

    dados = get_json(
        url,
        f"Banco Central - histórico SGS {serie}",
        params=params,
    )

    if not isinstance(dados, list):
        raise RuntimeError(
            f"Resposta inesperada do BCB para o histórico "
            f"da série {serie}."
        )

    saida = []

    for registro in dados:
        try:
            data_iso = parse_data_bcb(
                registro["data"]
            ).isoformat()

            valor = numero(registro["valor"])

        except (KeyError, ValueError, TypeError):
            continue

        saida.append({
            "data": data_iso,
            "valor": valor,
        })

    return saida


def buscar_bcb_historico(serie, inicio, fim):
    """
    Busca o histórico completo, juntando os blocos e
    removendo eventuais duplicações.
    """
    pontos_por_data = {}

    for bloco_inicio, bloco_fim in intervalos_historicos(
        inicio,
        fim,
    ):
        print(
            f"  SGS {serie}: "
            f"{bloco_inicio.isoformat()} -> {bloco_fim.isoformat()}"
        )

        pontos = buscar_bcb_intervalo(
            serie,
            bloco_inicio,
            bloco_fim,
        )

        for ponto in pontos:
            pontos_por_data[ponto["data"]] = ponto["valor"]

    saida = [
        {
            "data": data_iso,
            "valor": pontos_por_data[data_iso],
        }
        for data_iso in sorted(pontos_por_data)
    ]

    if not saida:
        raise RuntimeError(
            f"Nenhum histórico retornado para a série SGS {serie}."
        )

    return saida


# ============================================================
# FOCUS
# ============================================================

def buscar_focus_indicador(nome_api):
    params = {
        "$filter": (
            f"Indicador eq '{nome_api}' "
            "and baseCalculo eq 0"
        ),
        "$orderby": "Data desc",
        "$top": str(FOCUS_TOP_POR_INDICADOR),
        "$select": (
            "Indicador,Data,DataReferencia,"
            "Media,Mediana,Minimo,Maximo,"
            "numeroRespondentes,baseCalculo"
        ),
        "$format": "json",
    }

    dados = get_json(
        FOCUS_URL,
        f"Focus - {nome_api}",
        params=params,
    )

    registros = dados.get("value")

    if not isinstance(registros, list) or not registros:
        raise RuntimeError(
            f"Nenhum dado Focus retornado para '{nome_api}'."
        )

    saida = []

    for item in registros:
        if not isinstance(item, dict):
            continue

        data_txt = item.get("Data")
        ano_txt = item.get("DataReferencia")
        mediana = numero_ou_none(item.get("Mediana"))

        if not data_txt or not ano_txt or mediana is None:
            continue

        try:
            data_obj = parse_data_focus(data_txt)
            ano_ref = int(str(ano_txt))
        except (ValueError, TypeError):
            continue

        saida.append({
            "data": data_obj,
            "ano": ano_ref,
            "mediana": mediana,
            "media": numero_ou_none(item.get("Media")),
            "minimo": numero_ou_none(item.get("Minimo")),
            "maximo": numero_ou_none(item.get("Maximo")),
            "respondentes": inteiro_ou_none(
                item.get("numeroRespondentes")
            ),
        })

    if not saida:
        raise RuntimeError(
            f"Não foi possível interpretar o Focus de '{nome_api}'."
        )

    return saida


def observacao_em_ou_antes(registros, data_alvo):
    candidatos = [
        item
        for item in registros
        if item["data"] <= data_alvo
    ]

    if not candidatos:
        return None

    return max(
        candidatos,
        key=lambda item: item["data"],
    )


def montar_focus_ano(registros, ano):
    por_ano = [
        item
        for item in registros
        if item["ano"] == ano
    ]

    if not por_ano:
        return None

    atual = max(
        por_ano,
        key=lambda item: item["data"],
    )

    semana_1 = observacao_em_ou_antes(
        por_ano,
        atual["data"] - timedelta(days=7),
    )

    semanas_4 = observacao_em_ou_antes(
        por_ano,
        atual["data"] - timedelta(days=28),
    )

    def bloco_anterior(item):
        if item is None:
            return None

        return {
            "valor": item["mediana"],
            "data": item["data"].isoformat(),
        }

    valor_1s = (
        semana_1["mediana"]
        if semana_1 is not None
        else None
    )

    valor_4s = (
        semanas_4["mediana"]
        if semanas_4 is not None
        else None
    )

    return {
        "valor": atual["mediana"],
        "data": atual["data"].isoformat(),
        "media": atual["media"],
        "minimo": atual["minimo"],
        "maximo": atual["maximo"],
        "respondentes": atual["respondentes"],
        "semana_1": bloco_anterior(semana_1),
        "semanas_4": bloco_anterior(semanas_4),
        "variacao_1s": (
            round(atual["mediana"] - valor_1s, 6)
            if valor_1s is not None
            else None
        ),
        "variacao_4s": (
            round(atual["mediana"] - valor_4s, 6)
            if valor_4s is not None
            else None
        ),
    }


def coletar_focus():
    agora = datetime.now(TIMEZONE)
    ano_atual = agora.year

    anos = list(
        range(
            ano_atual,
            ano_atual + FOCUS_QUANTIDADE_ANOS,
        )
    )

    dados_brutos = {}

    for chave, config in FOCUS_INDICADORES.items():
        print(f"Buscando Focus - {config['nome']}...")

        dados_brutos[chave] = buscar_focus_indicador(
            config["api"]
        )

    expectativas = {}

    for ano in anos:
        expectativas[str(ano)] = {}

        for chave, config in FOCUS_INDICADORES.items():
            bloco = montar_focus_ano(
                dados_brutos[chave],
                ano,
            )

            if bloco is None:
                expectativas[str(ano)][chave] = None
                continue

            bloco["nome"] = config["nome"]
            bloco["unidade"] = config["unidade"]

            expectativas[str(ano)][chave] = bloco

    datas_recentes = []

    for registros in dados_brutos.values():
        if registros:
            datas_recentes.append(
                max(
                    item["data"]
                    for item in registros
                )
            )

    return {
        "fonte": (
            "Banco Central do Brasil - "
            "Expectativas de Mercado (Focus)"
        ),
        "estatistica_principal": "Mediana",
        "base_calculo": 0,
        "data_focus_mais_recente": (
            max(datas_recentes).isoformat()
            if datas_recentes
            else None
        ),
        "anos": anos,
        "expectativas": expectativas,
    }


# ============================================================
# COLETA MACRO COMPLETA
# ============================================================

def coletar_macro():
    agora = datetime.now(TIMEZONE)
    hoje = agora.date()

    indicadores = {}
    historico = {}

    # Primeiro: séries que terão histórico completo.
    for chave, config in SERIES.items():
        if not config["historico"]:
            continue

        print()
        print(
            f"Buscando histórico de {config['nome']} "
            f"(SGS {config['sgs']})..."
        )

        pontos = buscar_bcb_historico(
            config["sgs"],
            HISTORICO_DESDE,
            hoje,
        )

        historico[chave] = pontos

        ultimo = pontos[-1]

        indicadores[chave] = {
            "nome": config["nome"],
            "valor": ultimo["valor"],
            "unidade": config["unidade"],
            "data": ultimo["data"],
            "fonte": config["fonte"],
            "sgs": config["sgs"],
        }

    # Depois: séries em que precisamos somente do último valor.
    for chave, config in SERIES.items():
        if config["historico"]:
            continue

        print(
            f"Buscando último valor de {config['nome']} "
            f"(SGS {config['sgs']})..."
        )

        ultimo = buscar_bcb_ultimo(
            config["sgs"]
        )

        indicadores[chave] = {
            "nome": config["nome"],
            "valor": ultimo["valor"],
            "unidade": config["unidade"],
            "data": ultimo["data"],
            "fonte": config["fonte"],
            "sgs": config["sgs"],
        }

    print()
    print("Buscando expectativas Focus...")
    focus = coletar_focus()

    return {
        "pais": "Brasil",
        "atualizado_em": agora.isoformat(),
        "timezone": "America/Sao_Paulo",
        "indicadores": indicadores,
        "historico": {
            "desde": HISTORICO_DESDE.isoformat(),
            "observacao": (
                "Selic Meta começa em 05/03/1999, "
                "pois a série SGS 432 não possui dados anteriores."
            ),
            "series": historico,
        },
        "focus": focus,
    }


# ============================================================
# SAÍDA
# ============================================================

def salvar_json(dados):
    DATA_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    with JSON_FILE.open(
        "w",
        encoding="utf-8",
    ) as arquivo:
        json.dump(
            dados,
            arquivo,
            ensure_ascii=False,
            indent=2,
        )

    print(f"JSON salvo em: {JSON_FILE}")


def salvar_csv(dados):
    """
    CSV resumido com:
    - valores atuais dos indicadores macro
    - valores atuais do Focus por ano

    O histórico completo fica no macro_brasil.json.
    """
    DATA_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    campos = [
        "tipo",
        "indicador",
        "nome",
        "ano_referencia",
        "valor",
        "unidade",
        "data",
        "fonte",
        "codigo",
        "variacao_1s",
        "variacao_4s",
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

        for chave, info in dados["indicadores"].items():
            writer.writerow({
                "tipo": "macro",
                "indicador": chave,
                "nome": info["nome"],
                "ano_referencia": "",
                "valor": info["valor"],
                "unidade": info["unidade"],
                "data": info["data"],
                "fonte": info["fonte"],
                "codigo": f"SGS {info['sgs']}",
                "variacao_1s": "",
                "variacao_4s": "",
            })

        for ano, expectativas in (
            dados["focus"]["expectativas"].items()
        ):
            for chave, info in expectativas.items():
                if info is None:
                    continue

                writer.writerow({
                    "tipo": "focus",
                    "indicador": chave,
                    "nome": info["nome"],
                    "ano_referencia": ano,
                    "valor": info["valor"],
                    "unidade": info["unidade"],
                    "data": info["data"],
                    "fonte": "Banco Central - Focus",
                    "codigo": "ExpectativasMercadoAnuais",
                    "variacao_1s": info["variacao_1s"],
                    "variacao_4s": info["variacao_4s"],
                })

    print(f"CSV salvo em: {CSV_FILE}")


# ============================================================
# LOG
# ============================================================

def mostrar_resumo(dados):
    print()
    print("=" * 80)
    print("MACRO BRASIL")
    print("=" * 80)

    print("\nINDICADORES ATUAIS")

    for info in dados["indicadores"].values():
        print(
            f"  {info['nome']}: "
            f"{info['valor']} {info['unidade']} "
            f"({info['data']})"
        )

    print("\nHISTÓRICOS")

    for chave, pontos in (
        dados["historico"]["series"].items()
    ):
        if pontos:
            print(
                f"  {chave}: "
                f"{len(pontos)} pontos "
                f"({pontos[0]['data']} -> "
                f"{pontos[-1]['data']})"
            )

    print("\nFOCUS")

    for ano, expectativas in (
        dados["focus"]["expectativas"].items()
    ):
        valores = []

        for info in expectativas.values():
            if info is None:
                continue

            valores.append(
                f"{info['nome']}={info['valor']}"
            )

        print(
            f"  {ano}: "
            + " | ".join(valores)
        )

    print()
    print(f"JSON: {JSON_FILE}")
    print(f"CSV:  {CSV_FILE}")
    print(f"Atualizado em: {dados['atualizado_em']}")
    print("=" * 80)


# ============================================================
# MAIN
# ============================================================

def main():
    try:
        dados = coletar_macro()

        # Só grava os arquivos depois que todas as fontes
        # responderem corretamente.
        salvar_json(dados)
        salvar_csv(dados)

        mostrar_resumo(dados)

    except Exception as erro:
        print()
        print("=" * 80)
        print("ERRO NA ATUALIZAÇÃO DO MACRO BRASIL")
        print("=" * 80)
        print(str(erro))
        print()

        raise


if __name__ == "__main__":
    main()
