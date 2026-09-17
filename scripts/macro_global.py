import csv
import io
import json
import zipfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pycountry
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


# ============================================================
# CONFIGURAÇÃO
# ============================================================

TIMEZONE = ZoneInfo("America/Sao_Paulo")

# Este arquivo deve ficar em:
#   scripts/macro_global.py
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"

JSON_FILE = DATA_DIR / "macro_global.json"
CSV_FILE = DATA_DIR / "macro_global.csv"

ANO_ATUAL = datetime.now(TIMEZONE).year

# Consultamos alguns anos em volta do ano atual para permitir
# fallback quando um país não tiver valor exatamente no ano corrente.
IMF_PERIODOS = list(range(ANO_ATUAL - 2, ANO_ATUAL + 2))


# ============================================================
# FONTES
# ============================================================

# IMF DataMapper / World Economic Outlook
IMF_BASE = "https://www.imf.org/external/datamapper/api/v1"

IMF_INDICADORES = {
    "inflacao": {
        "codigo": "PCPIPCH",
        "nome": "Inflação ao consumidor",
        "unidade": "%",
    },
    "desemprego": {
        "codigo": "LUR",
        "nome": "Taxa de desemprego",
        "unidade": "%",
    },
    "divida_pib": {
        "codigo": "GGXWDG_NGDP",
        "nome": "Dívida bruta do governo geral / PIB",
        "unidade": "% do PIB",
    },
}

# World Bank - World Development Indicators
WORLD_BANK_BASE = "https://api.worldbank.org/v2"

WB_GDP_USD = "NY.GDP.MKTP.CD"
WB_FDI_PIB = "BX.KLT.DINV.WD.GD.ZS"
WB_FDI_USD = "BX.KLT.DINV.CD.WD"

# BIS - Central bank policy rates - bulk CSV flat
BIS_CBPOL_ZIP = (
    "https://data.bis.org/static/bulk/WS_CBPOL_csv_flat.zip"
)


# ============================================================
# HTTP
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
            "Macro-Global-GitHub-Action/1.0 "
            "(public economic data collector)"
        ),
        "Accept": "*/*",
    })

    return session


SESSION = criar_session()


def get_json(url, nome_fonte, params=None, timeout=90):
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


# ============================================================
# UTILITÁRIOS
# ============================================================

def numero_ou_none(valor):
    if valor is None:
        return None

    try:
        return float(str(valor).strip().replace(",", "."))
    except (TypeError, ValueError):
        return None


def iso3_para_iso2(iso3):
    """
    Converte ISO3 em ISO2 para cruzar IMF/World Bank com BIS.

    A maior parte das economias do WEO usa ISO3 padrão.
    Alguns territórios/economias especiais podem não possuir
    correspondência BIS; nesses casos retornamos None.
    """
    especiais = {
        "WBG": "PS",  # West Bank and Gaza
        "UVK": "XK",  # Kosovo (código de uso comum, não ISO oficial)
    }

    if iso3 in especiais:
        return especiais[iso3]

    try:
        pais = pycountry.countries.get(alpha_3=iso3)
        return pais.alpha_2 if pais else None
    except Exception:
        return None


def escolher_valor_imf(serie):
    """
    Escolhe preferencialmente o valor do ano atual.
    Se ele não existir, usa o mais recente até o ano atual.
    Se ainda não houver, usa o primeiro valor futuro disponível.
    """
    if not isinstance(serie, dict) or not serie:
        return None

    valores = {}

    for ano_txt, valor_bruto in serie.items():
        try:
            ano = int(ano_txt)
        except (TypeError, ValueError):
            continue

        valor = numero_ou_none(valor_bruto)

        if valor is not None:
            valores[ano] = valor

    if not valores:
        return None

    if ANO_ATUAL in valores:
        return {
            "valor": valores[ANO_ATUAL],
            "ano": ANO_ATUAL,
            "natureza": (
                "WEO: ano corrente; pode incluir estimativa/projeção"
            ),
        }

    passados = [
        ano
        for ano in valores
        if ano <= ANO_ATUAL
    ]

    if passados:
        ano = max(passados)

        return {
            "valor": valores[ano],
            "ano": ano,
            "natureza": "WEO: último ano disponível",
        }

    ano = min(valores)

    return {
        "valor": valores[ano],
        "ano": ano,
        "natureza": "WEO: projeção",
    }


# ============================================================
# IMF WEO
# ============================================================

def buscar_paises_imf():
    print("Buscando lista de países no FMI...")

    dados = get_json(
        f"{IMF_BASE}/countries",
        "IMF DataMapper - países",
    )

    bruto = dados.get("countries", dados)

    if not isinstance(bruto, dict):
        raise RuntimeError(
            "Formato inesperado da lista de países do IMF DataMapper."
        )

    paises = {}

    for codigo, info in bruto.items():
        if not isinstance(codigo, str):
            continue

        codigo = codigo.upper().strip()

        if len(codigo) != 3:
            continue

        if isinstance(info, dict):
            nome = (
                info.get("label")
                or info.get("name")
                or info.get("country")
                or codigo
            )
        else:
            nome = str(info)

        paises[codigo] = {
            "codigo": codigo,
            "iso2": iso3_para_iso2(codigo),
            "nome": nome,
        }

    if not paises:
        raise RuntimeError(
            "O FMI não retornou países interpretáveis."
        )

    return paises


def buscar_indicador_imf(codigo):
    periodos = ",".join(
        str(ano)
        for ano in IMF_PERIODOS
    )

    url = f"{IMF_BASE}/{codigo}"

    dados = get_json(
        url,
        f"IMF WEO - {codigo}",
        params={"periods": periodos},
    )

    values = dados.get("values", {})

    if not isinstance(values, dict):
        raise RuntimeError(
            f"Resposta inesperada do IMF para {codigo}."
        )

    # Formato esperado:
    # values -> codigo_indicador -> ISO3 -> ano -> valor
    bloco = values.get(codigo)

    if bloco is None and len(values) == 1:
        bloco = next(iter(values.values()))

    if not isinstance(bloco, dict):
        raise RuntimeError(
            f"O IMF não retornou séries para {codigo}."
        )

    return bloco


def coletar_imf(paises):
    for chave, config in IMF_INDICADORES.items():
        codigo = config["codigo"]

        print(
            f"Buscando {config['nome']} no FMI "
            f"({codigo})..."
        )

        series = buscar_indicador_imf(codigo)

        for iso3, pais in paises.items():
            serie = series.get(iso3)
            selecionado = escolher_valor_imf(serie)

            if selecionado is None:
                pais[chave] = None
                continue

            pais[chave] = {
                "valor": selecionado["valor"],
                "unidade": config["unidade"],
                "ano": selecionado["ano"],
                "natureza": selecionado["natureza"],
                "fonte": "IMF World Economic Outlook",
                "codigo_fonte": codigo,
            }


# ============================================================
# WORLD BANK - FDI
# ============================================================

def buscar_world_bank_mrnev(indicador):
    """
    Busca o valor mais recente não vazio para todos os países.
    """
    url = (
        f"{WORLD_BANK_BASE}/country/all/"
        f"indicator/{indicador}"
    )

    dados = get_json(
        url,
        f"World Bank - {indicador}",
        params={
            "format": "json",
            "mrnev": 1,
            "per_page": 25000,
            "source": 2,
        },
    )

    if not isinstance(dados, list) or len(dados) < 2:
        raise RuntimeError(
            f"Resposta inesperada do World Bank para {indicador}."
        )

    metadata = dados[0] if isinstance(dados[0], dict) else {}
    registros = dados[1] if isinstance(dados[1], list) else []

    saida = {}

    for item in registros:
        if not isinstance(item, dict):
            continue

        iso3 = item.get("countryiso3code")

        if not iso3:
            continue

        valor = numero_ou_none(item.get("value"))

        if valor is None:
            continue

        saida[iso3.upper()] = {
            "valor": valor,
            "ano": item.get("date"),
        }

    return {
        "dados": saida,
        "ultima_atualizacao_fonte": metadata.get("lastupdated"),
    }


def coletar_world_bank(paises):
    print("Buscando PIB nominal em US$ no World Bank...")

    pib_usd = buscar_world_bank_mrnev(
        WB_GDP_USD
    )

    print("Buscando FDI / PIB no World Bank...")

    fdi_pib = buscar_world_bank_mrnev(
        WB_FDI_PIB
    )

    print("Buscando FDI em US$ no World Bank...")

    fdi_usd = buscar_world_bank_mrnev(
        WB_FDI_USD
    )

    for iso3, pais in paises.items():
        pib = pib_usd["dados"].get(iso3)
        pct = fdi_pib["dados"].get(iso3)
        usd = fdi_usd["dados"].get(iso3)

        if pib:
            pais["pib"] = {
                "valor": pib["valor"],
                "unidade": "US$",
                "ano": pib["ano"],
                "fonte": "World Bank - WDI",
                "codigo_fonte": WB_GDP_USD,
            }
        else:
            pais["pib"] = None

        if pct:
            pais["fdi_pib"] = {
                "valor": pct["valor"],
                "unidade": "% do PIB",
                "ano": pct["ano"],
                "fonte": "World Bank - WDI",
                "codigo_fonte": WB_FDI_PIB,
            }
        else:
            pais["fdi_pib"] = None

        if usd:
            pais["fdi_usd"] = {
                "valor": usd["valor"],
                "unidade": "US$",
                "ano": usd["ano"],
                "fonte": "World Bank - WDI",
                "codigo_fonte": WB_FDI_USD,
            }
        else:
            pais["fdi_usd"] = None

    return {
        "pib_usd": pib_usd["ultima_atualizacao_fonte"],
        "fdi_pib": fdi_pib["ultima_atualizacao_fonte"],
        "fdi_usd": fdi_usd["ultima_atualizacao_fonte"],
    }


# ============================================================
# BIS - CENTRAL BANK POLICY RATES
# ============================================================

def detectar_nome_coluna(cabecalho, candidatos):
    normalizado = {
        str(coluna).strip().upper(): coluna
        for coluna in cabecalho
    }

    for candidato in candidatos:
        if candidato.upper() in normalizado:
            return normalizado[candidato.upper()]

    return None


def baixar_bis_policy_rates():
    print("Baixando taxas básicas do BIS...")

    response = SESSION.get(
        BIS_CBPOL_ZIP,
        timeout=120,
    )

    if not response.ok:
        raise RuntimeError(
            f"BIS retornou HTTP {response.status_code} "
            "ao baixar Central bank policy rates."
        )

    try:
        arquivo_zip = zipfile.ZipFile(
            io.BytesIO(response.content)
        )
    except zipfile.BadZipFile as erro:
        raise RuntimeError(
            "O arquivo de policy rates do BIS não é um ZIP válido."
        ) from erro

    csvs = [
        nome
        for nome in arquivo_zip.namelist()
        if nome.lower().endswith(".csv")
    ]

    if not csvs:
        raise RuntimeError(
            "Nenhum CSV encontrado no ZIP do BIS."
        )

    # Preferimos o CSV flat.
    nome_csv = csvs[0]

    with arquivo_zip.open(nome_csv) as bruto:
        texto = io.TextIOWrapper(
            bruto,
            encoding="utf-8-sig",
            errors="replace",
            newline="",
        )

        reader = csv.DictReader(texto)

        if not reader.fieldnames:
            raise RuntimeError(
                "CSV do BIS sem cabeçalho."
            )

        col_freq = detectar_nome_coluna(
            reader.fieldnames,
            ["FREQ"],
        )
        col_area = detectar_nome_coluna(
            reader.fieldnames,
            ["REF_AREA", "REFERENCE_AREA"],
        )
        col_periodo = detectar_nome_coluna(
            reader.fieldnames,
            ["TIME_PERIOD", "PERIOD"],
        )
        col_valor = detectar_nome_coluna(
            reader.fieldnames,
            ["OBS_VALUE", "VALUE"],
        )

        if not all([
            col_freq,
            col_area,
            col_periodo,
            col_valor,
        ]):
            raise RuntimeError(
                "Não foi possível localizar as colunas "
                "FREQ/REF_AREA/TIME_PERIOD/OBS_VALUE "
                f"no CSV do BIS. Cabeçalho: {reader.fieldnames}"
            )

        por_area = {}

        for linha in reader:
            area = str(
                linha.get(col_area, "")
            ).strip().upper()

            freq = str(
                linha.get(col_freq, "")
            ).strip().upper()

            periodo = str(
                linha.get(col_periodo, "")
            ).strip()

            valor = numero_ou_none(
                linha.get(col_valor)
            )

            if (
                not area
                or not periodo
                or valor is None
            ):
                continue

            # D = diário, M = mensal.
            # Preferimos diário porque reflete a decisão mais recente.
            prioridade = 2 if freq == "D" else 1 if freq == "M" else 0

            if prioridade == 0:
                continue

            atual = por_area.get(area)

            candidato = {
                "valor": valor,
                "data": periodo,
                "frequencia": freq,
                "prioridade": prioridade,
            }

            if atual is None:
                por_area[area] = candidato
                continue

            if prioridade > atual["prioridade"]:
                por_area[area] = candidato
                continue

            if (
                prioridade == atual["prioridade"]
                and periodo > atual["data"]
            ):
                por_area[area] = candidato

    return por_area


def coletar_juros_bis(paises):
    taxas = baixar_bis_policy_rates()

    for pais in paises.values():
        iso2 = pais.get("iso2")

        registro = (
            taxas.get(iso2)
            if iso2
            else None
        )

        if registro is None:
            pais["taxa_juros"] = None
            continue

        pais["taxa_juros"] = {
            "valor": registro["valor"],
            "unidade": "% a.a.",
            "data": registro["data"],
            "frequencia": (
                "diária"
                if registro["frequencia"] == "D"
                else "mensal"
            ),
            "fonte": "Bank for International Settlements",
            "dataset": "Central bank policy rates",
        }


# ============================================================
# SAÍDA
# ============================================================

def contar_cobertura(paises, campo):
    return sum(
        1
        for pais in paises.values()
        if pais.get(campo) is not None
    )


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
    DATA_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    campos = [
        "iso3",
        "iso2",
        "pais",
        "pib",
        "pib_ano",
        "inflacao",
        "inflacao_ano",
        "desemprego",
        "desemprego_ano",
        "divida_pib",
        "divida_pib_ano",
        "fdi_pib",
        "fdi_pib_ano",
        "fdi_usd",
        "fdi_usd_ano",
        "taxa_juros",
        "taxa_juros_data",
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

        paises_ordenados = sorted(
            dados["paises"].values(),
            key=lambda item: item["nome"],
        )

        for pais in paises_ordenados:
            def valor(campo):
                bloco = pais.get(campo)
                return (
                    bloco.get("valor")
                    if isinstance(bloco, dict)
                    else ""
                )

            def ano(campo):
                bloco = pais.get(campo)
                return (
                    bloco.get("ano")
                    if isinstance(bloco, dict)
                    else ""
                )

            juros = pais.get("taxa_juros")

            writer.writerow({
                "iso3": pais["codigo"],
                "iso2": pais.get("iso2") or "",
                "pais": pais["nome"],
                "pib": valor("pib"),
                "pib_ano": ano("pib"),
                "inflacao": valor("inflacao"),
                "inflacao_ano": ano("inflacao"),
                "desemprego": valor("desemprego"),
                "desemprego_ano": ano("desemprego"),
                "divida_pib": valor("divida_pib"),
                "divida_pib_ano": ano("divida_pib"),
                "fdi_pib": valor("fdi_pib"),
                "fdi_pib_ano": ano("fdi_pib"),
                "fdi_usd": valor("fdi_usd"),
                "fdi_usd_ano": ano("fdi_usd"),
                "taxa_juros": (
                    juros.get("valor")
                    if isinstance(juros, dict)
                    else ""
                ),
                "taxa_juros_data": (
                    juros.get("data")
                    if isinstance(juros, dict)
                    else ""
                ),
            })

    print(f"CSV salvo em: {CSV_FILE}")


# ============================================================
# MAIN
# ============================================================

def main():
    agora = datetime.now(TIMEZONE)

    print("=" * 80)
    print("MACRO GLOBAL")
    print("=" * 80)

    paises = buscar_paises_imf()

    print(f"Países/economias encontrados no FMI: {len(paises)}")

    coletar_imf(paises)

    atualizacoes_wb = coletar_world_bank(paises)

    # BIS possui policy rates para um subconjunto de economias.
    # Falha do BIS não deve apagar todos os demais dados globais.
    erro_bis = None

    try:
        coletar_juros_bis(paises)
    except Exception as erro:
        erro_bis = str(erro)
        print()
        print("AVISO: não foi possível atualizar o BIS.")
        print(erro_bis)
        print(
            "Os demais indicadores serão salvos; "
            "taxa_juros ficará nula nesta execução."
        )

        for pais in paises.values():
            pais["taxa_juros"] = None

    cobertura = {
        "total_paises_imf": len(paises),
        "pib": contar_cobertura(paises, "pib"),  # PIB nominal em US$
        "inflacao": contar_cobertura(paises, "inflacao"),
        "desemprego": contar_cobertura(paises, "desemprego"),
        "divida_pib": contar_cobertura(paises, "divida_pib"),
        "fdi_pib": contar_cobertura(paises, "fdi_pib"),
        "fdi_usd": contar_cobertura(paises, "fdi_usd"),
        "taxa_juros": contar_cobertura(paises, "taxa_juros"),
    }

    dados = {
        "atualizado_em": agora.isoformat(),
        "timezone": "America/Sao_Paulo",
        "ano_referencia_weo": ANO_ATUAL,
        "metodologia": {
            "pib": {
                "descricao": "PIB nominal em US$ correntes",
                "fonte": "World Bank - WDI",
                "codigo": WB_GDP_USD,
            },
            "inflacao": {
                "descricao": "Inflação ao consumidor",
                "fonte": "IMF World Economic Outlook",
                "codigo": "PCPIPCH",
            },
            "desemprego": {
                "descricao": "Taxa de desemprego",
                "fonte": "IMF World Economic Outlook",
                "codigo": "LUR",
            },
            "divida_pib": {
                "descricao": (
                    "Dívida bruta do governo geral "
                    "como percentual do PIB"
                ),
                "fonte": "IMF World Economic Outlook",
                "codigo": "GGXWDG_NGDP",
            },
            "fdi_pib": {
                "descricao": (
                    "Investimento estrangeiro direto, "
                    "entradas líquidas, % do PIB"
                ),
                "fonte": "World Bank - WDI",
                "codigo": WB_FDI_PIB,
            },
            "fdi_usd": {
                "descricao": (
                    "Investimento estrangeiro direto, "
                    "entradas líquidas, US$"
                ),
                "fonte": "World Bank - WDI",
                "codigo": WB_FDI_USD,
            },
            "taxa_juros": {
                "descricao": "Taxa de política monetária do banco central",
                "fonte": "Bank for International Settlements",
                "dataset": "Central bank policy rates",
            },
        },
        "fontes": {
            "imf": {
                "api": IMF_BASE,
                "observacao": (
                    "WEO é uma base semestral; inflação, desemprego "
                    "e dívida/PIB do ano corrente podem conter "
                    "estimativas/projeções."
                ),
            },
            "world_bank": {
                "api": WORLD_BANK_BASE,
                "ultima_atualizacao_pib_usd": (
                    atualizacoes_wb.get("pib_usd")
                ),
                "ultima_atualizacao_fdi_pib": (
                    atualizacoes_wb.get("fdi_pib")
                ),
                "ultima_atualizacao_fdi_usd": (
                    atualizacoes_wb.get("fdi_usd")
                ),
            },
            "bis": {
                "bulk_download": BIS_CBPOL_ZIP,
                "erro_na_execucao": erro_bis,
            },
        },
        "cobertura": cobertura,
        "paises": {
            codigo: paises[codigo]
            for codigo in sorted(paises)
        },
    }

    salvar_json(dados)
    salvar_csv(dados)

    print()
    print("=" * 80)
    print("COBERTURA")
    print("=" * 80)

    for chave, valor in cobertura.items():
        print(f"{chave}: {valor}")

    print()
    print(f"JSON: {JSON_FILE}")
    print(f"CSV:  {CSV_FILE}")
    print("=" * 80)


if __name__ == "__main__":
    main()
