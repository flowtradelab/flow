"""Calcula IV Rank e Percentil da série produzida por update_iv_di_history.py."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data" / "iv_di_history"


def calculate(path: Path, window: int = 250, series_type: str = "geral") -> dict:
    fields = {"geral": "iv", "call": "iv_call", "put": "iv_put"}
    if series_type not in fields:
        raise ValueError(f"Tipo de série inválido: {series_type}")
    field = fields[series_type]
    payload = json.loads(path.read_text(encoding="utf-8"))
    series = sorted(payload.get("serie_iv_diaria", []), key=lambda row: row["date"])[-window:]
    if len(series) < 20:
        raise ValueError(f"São necessários ao menos 20 pregões; encontrados {len(series)}")
    if any(field not in row for row in series):
        raise ValueError(f"A série não contém o campo {field}; recalcule o histórico")
    values = [float(row[field]) for row in series]
    current, minimum, maximum = values[-1], min(values), max(values)
    rank = 0.0 if maximum == minimum else (current - minimum) / (maximum - minimum) * 100.0
    percentile = sum(value < current for value in values) / len(values) * 100.0
    return {
        "ativo": payload["ativo"],
        "serie": series_type,
        "data": series[-1]["date"],
        "iv_atual_pct": round(current * 100, 4),
        "iv_rank": round(rank, 2),
        "iv_percentil": round(percentile, 2),
        "iv_min_pct": round(minimum * 100, 4),
        "iv_max_pct": round(maximum * 100, 4),
        "pregoes": len(series),
        "metodologia": payload.get("metodologia"),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ticker", type=str.upper)
    parser.add_argument("--window", type=int, default=250)
    parser.add_argument("--type", choices=("geral", "call", "put"), default="geral")
    args = parser.parse_args()
    print(json.dumps(
        calculate(DATA_DIR / f"{args.ticker}.json", args.window, args.type),
        ensure_ascii=False,
        indent=2,
    ))


if __name__ == "__main__":
    main()
