#!/usr/bin/env python3
"""Atualiza composições e classificações setoriais dos índices da B3."""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable


INDEX_API = "https://sistemaswebb3-listados.b3.com.br/indexProxy/indexCall"
COMPANY_API = "https://sistemaswebb3-listados.b3.com.br/listedCompaniesProxy/CompanyCall"
PORTAL_URL = "https://sistemaswebb3-listados.b3.com.br/indexPage/stocks"
SCHEMA_VERSION = 1
ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
INDEX_DIR = DATA_DIR / "indices"
HISTORY_DIR = DATA_DIR / "history"
USER_AGENT = os.getenv(
    "B3_USER_AGENT",
    "b3-indices-json/1.0 (+https://github.com/; public-data collector)",
)


def encoded(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.b64encode(raw).decode("ascii")


def fetch_json(url: str, *, attempts: int = 4) -> Any:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                result = json.load(response)
                # Some B3 endpoints return a JSON object serialized inside a JSON string.
                if isinstance(result, str) and result.lstrip().startswith(("{", "[")):
                    result = json.loads(result)
                return result
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            if attempt == attempts - 1:
                raise
            time.sleep(2**attempt)
    raise RuntimeError("Falha inesperada ao consultar a B3")


def api_call(base: str, endpoint: str, payload: dict[str, Any]) -> Any:
    return fetch_json(f"{base}/{endpoint}/{encoded(payload)}")


def parse_b3_number(value: Any, *, integer: bool = False) -> int | float | None:
    if value in (None, "", "-"):
        return None
    if isinstance(value, (int, float)):
        return int(value) if integer else float(value)
    normalized = str(value).strip().replace(".", "").replace(",", ".")
    return int(float(normalized)) if integer else float(normalized)


def parse_b3_date(value: str | None) -> str | None:
    if not value:
        return None
    for fmt in ("%d/%m/%y", "%d/%m/%Y"):
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            pass
    return value


def issuer_from_ticker(ticker: str) -> str:
    match = re.match(r"[A-Z0-9]{4}", ticker.upper())
    return match.group(0) if match else ticker.upper()


def split_classification(value: str | None) -> dict[str, str | None]:
    parts = [part.strip() for part in (value or "").split("/")]
    parts += [None] * (3 - len(parts))
    return {"sector": parts[0] or None, "subsector": parts[1] or None, "segment": parts[2] or None}


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def load_json(path: Path) -> Any | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


@dataclass(frozen=True)
class Period:
    year: int
    start_month: int
    end_month: int

    @property
    def identifier(self) -> str:
        return f"{self.year}-{self.start_month:02d}_{self.end_month:02d}"

    def as_dict(self) -> dict[str, int]:
        return {"year": self.year, "start_month": self.start_month, "end_month": self.end_month}


def fetch_stock_index() -> dict[str, Any]:
    return api_call(
        INDEX_API,
        "GetStockIndex",
        {"language": "pt-br", "pageNumber": 1, "pageSize": 10000},
    )


def fetch_company_catalog() -> dict[str, dict[str, Any]]:
    payload = {"language": "pt-br", "pageNumber": 1, "pageSize": 120}
    first = api_call(COMPANY_API, "GetInitialCompanies", payload)
    pages = int(first.get("page", {}).get("totalPages") or 1)
    rows = list(first.get("results") or [])
    for page_number in range(2, pages + 1):
        payload["pageNumber"] = page_number
        page = api_call(COMPANY_API, "GetInitialCompanies", payload)
        rows.extend(page.get("results") or [])

    # Prefer quoted companies if the same issuing code appears more than once.
    rows.sort(key=lambda row: (row.get("issuingCompany", ""), row.get("dateListing", "")))
    catalog: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = (row.get("issuingCompany") or "").upper()
        if not key:
            continue
        current = catalog.get(key)
        if current is None or row.get("marketIndicator") in {"17", "18"}:
            catalog[key] = row
    return catalog


def fetch_company_detail(code_cvm: str) -> dict[str, Any]:
    return api_call(COMPANY_API, "GetDetail", {"language": "pt-br", "codeCVM": code_cvm}) or {}


def fetch_company_details(
    tickers: Iterable[str], catalog: dict[str, dict[str, Any]], workers: int
) -> dict[str, dict[str, Any]]:
    issuer_codes = sorted({issuer_from_ticker(ticker) for ticker in tickers})
    code_to_issuer = {
        catalog[issuer]["codeCVM"]: issuer
        for issuer in issuer_codes
        if issuer in catalog and catalog[issuer].get("codeCVM")
    }
    details: dict[str, dict[str, Any]] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(fetch_company_detail, cvm): (cvm, issuer) for cvm, issuer in code_to_issuer.items()}
        for future in concurrent.futures.as_completed(futures):
            cvm, issuer = futures[future]
            try:
                result = future.result()
            except Exception as error:  # One unavailable issuer must not discard all index data.
                print(f"aviso: detalhe CVM {cvm} indisponível: {error}")
                result = {}
            if not isinstance(result, dict):
                print(f"aviso: detalhe CVM {cvm} retornou formato inesperado")
                result = {}
            details[issuer] = result
    return details


def fetch_portfolio(index: str) -> dict[str, Any]:
    return api_call(
        INDEX_API,
        "GetPortfolioDay",
        {"language": "pt-br", "pageNumber": 1, "pageSize": 1000, "index": index, "segment": "1"},
    )


def make_constituent(
    row: dict[str, Any],
    stock_row: dict[str, Any] | None,
    company_detail: dict[str, Any] | None,
) -> dict[str, Any]:
    ticker = row.get("cod") or ""
    detail = company_detail or {}
    classification = split_classification(detail.get("industryClassification"))
    if not classification["sector"]:
        classification["sector"] = "Não classificado pela B3"
    return {
        "ticker": ticker,
        "company": detail.get("companyName") or (stock_row or {}).get("company") or row.get("asset"),
        "trading_name": detail.get("tradingName") or row.get("asset"),
        "asset_type": (row.get("type") or (stock_row or {}).get("spotlight") or "").strip(),
        "theoretical_quantity": parse_b3_number(row.get("theoricalQty"), integer=True),
        "weight_percent": parse_b3_number(row.get("part")),
        "sector": classification["sector"],
        "subsector": classification["subsector"],
        "segment": classification["segment"],
        "issuer_code": detail.get("issuingCompany") or issuer_from_ticker(ticker),
        "cvm_code": detail.get("codeCVM"),
        "cnpj": detail.get("cnpj"),
    }


def archive_history(index: str, period: Period, new_data: dict[str, Any], reference_date: str) -> None:
    current_path = INDEX_DIR / f"{index}.json"
    previous = load_json(current_path)
    period_path = HISTORY_DIR / index / f"{period.identifier}.json"
    if previous:
        old_symbols = {item["ticker"] for item in previous.get("constituents", [])}
        new_symbols = {item["ticker"] for item in new_data.get("constituents", [])}
    else:
        old_symbols = new_symbols = set()
    if period_path.exists() and previous and old_symbols != new_symbols:
        fallback_date = reference_date or date.today().isoformat()
        change_path = HISTORY_DIR / index / f"{period.identifier}-before-{fallback_date}.json"
        if not change_path.exists():
            write_json(change_path, previous)
    # The period snapshot tracks the latest official state until the period closes.
    write_json(period_path, new_data)


def build_index_document(
    index: str,
    portfolio: dict[str, Any],
    period: Period,
    generated_at: str,
    stock_by_ticker: dict[str, dict[str, Any]],
    company_details: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    header = portfolio.get("header") or {}
    constituents = []
    for row in portfolio.get("results") or []:
        ticker = row.get("cod") or ""
        constituents.append(
            make_constituent(
                row,
                stock_by_ticker.get(ticker),
                company_details.get(issuer_from_ticker(ticker)),
            )
        )
    constituents.sort(key=lambda item: item["ticker"])
    return {
        "schema_version": SCHEMA_VERSION,
        "index": index,
        "generated_at": generated_at,
        "reference_date": parse_b3_date(header.get("date")),
        "portfolio_period": period.as_dict(),
        "source": {"provider": "B3", "url": f"https://sistemaswebb3-listados.b3.com.br/indexPage/day/{index}?language=pt-br"},
        "totals": {
            "weight_percent": parse_b3_number(header.get("part")),
            "theoretical_quantity": parse_b3_number(header.get("theoricalQty"), integer=True),
            "reductor": parse_b3_number(header.get("reductor")),
        },
        "constituent_count": len(constituents),
        "constituents": constituents,
    }


def update(*, workers: int = 8, selected_indexes: set[str] | None = None) -> None:
    generated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    stock_index = fetch_stock_index()
    header = stock_index.get("header") or {}
    period = Period(
        year=int(header["year"]),
        start_month=int(header["startMonth"]),
        end_month=int(header["endMonth"]),
    )
    stock_rows = stock_index.get("results") or []
    stock_by_ticker = {row["code"]: row for row in stock_rows if row.get("code")}
    indexes = sorted(
        {
            code.strip()
            for row in stock_rows
            for code in (row.get("indexes") or "").split(",")
            if code.strip()
        }
    )
    if selected_indexes:
        indexes = [index for index in indexes if index in selected_indexes]
    if not indexes:
        raise RuntimeError("Nenhum índice encontrado na fonte oficial da B3")

    catalog = fetch_company_catalog()
    company_details = fetch_company_details(stock_by_ticker.keys(), catalog, workers)
    companies_document = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "source": {"provider": "B3", "url": "https://www.b3.com.br/pt_br/produtos-e-servicos/negociacao/renda-variavel/empresas-listadas.htm"},
        "companies": {
            issuer: {
                "issuer_code": detail.get("issuingCompany") or issuer,
                "cvm_code": detail.get("codeCVM"),
                "cnpj": detail.get("cnpj"),
                "company": detail.get("companyName"),
                "trading_name": detail.get("tradingName"),
                **(
                    lambda classification: {
                        **classification,
                        "sector": classification["sector"] or "Não classificado pela B3",
                    }
                )(split_classification(detail.get("industryClassification"))),
            }
            for issuer, detail in sorted(company_details.items())
        },
    }
    write_json(DATA_DIR / "companies.json", companies_document)

    documents = []
    index_summaries = []
    for position, index in enumerate(indexes, start=1):
        print(f"[{position}/{len(indexes)}] {index}")
        portfolio = fetch_portfolio(index)
        document = build_index_document(
            index, portfolio, period, generated_at, stock_by_ticker, company_details
        )
        if not document["constituents"]:
            print(f"aviso: {index} não retornou constituintes; arquivo anterior preservado")
            continue
        reference_date = document.get("reference_date") or date.today().isoformat()
        archive_history(index, period, document, reference_date)
        write_json(INDEX_DIR / f"{index}.json", document)
        documents.append(document)
        index_summaries.append(
            {
                "code": index,
                "constituent_count": document["constituent_count"],
                "reference_date": document["reference_date"],
                "file": f"indices/{index}.json",
            }
        )

    indexes_document = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "source": {"provider": "B3", "url": PORTAL_URL},
        "portfolio_period": period.as_dict(),
        "index_count": len(index_summaries),
        "indexes": index_summaries,
    }
    write_json(DATA_DIR / "indexes.json", indexes_document)
    write_json(
        DATA_DIR / "consolidated.json",
        {
            "schema_version": SCHEMA_VERSION,
            "generated_at": generated_at,
            "source": {"provider": "B3", "url": PORTAL_URL},
            "portfolio_period": period.as_dict(),
            "index_count": len(documents),
            "indices": documents,
        },
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, default=8, help="consultas paralelas de companhias")
    parser.add_argument("--index", action="append", help="limita a atualização a um código (repetível)")
    args = parser.parse_args()
    update(workers=max(1, args.workers), selected_indexes=set(args.index or []) or None)


if __name__ == "__main__":
    main()
