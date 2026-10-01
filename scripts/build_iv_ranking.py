"""Consolida os históricos IV-DI em um ranking JSON e CSV."""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from pathlib import Path

from iv_rank_di import calculate
from update_di_curve import atomic_json


ROOT = Path(__file__).resolve().parents[1]
HISTORY_DIR = ROOT / "data" / "iv_di_history"
JSON_OUTPUT = ROOT / "data" / "iv_di_ranking.json"
CSV_OUTPUT = ROOT / "data" / "iv_di_ranking.csv"


def coverage_label(sessions: int, window: int) -> str:
    if sessions >= window:
        return "completo"
    if sessions >= 200:
        return "alto"
    if sessions >= 100:
        return "parcial"
    return "curto"


def build_ranking(history_dir: Path = HISTORY_DIR, window: int = 250) -> dict:
    assets = []
    for path in sorted(history_dir.glob("*.json")):
        general = calculate(path, window, "geral")
        call = calculate(path, window, "call")
        put = calculate(path, window, "put")
        sessions = general["pregoes"]
        assets.append({
            "ticker": general["ativo"],
            "data": general["data"],
            "pregoes": sessions,
            "cobertura_pct": round(sessions / window * 100.0, 2),
            "cobertura": coverage_label(sessions, window),
            "iv_geral_pct": general["iv_atual_pct"],
            "iv_rank_geral": general["iv_rank"],
            "iv_percentil_geral": general["iv_percentil"],
            "iv_call_pct": call["iv_atual_pct"],
            "iv_rank_call": call["iv_rank"],
            "iv_percentil_call": call["iv_percentil"],
            "iv_put_pct": put["iv_atual_pct"],
            "iv_rank_put": put["iv_rank"],
            "iv_percentil_put": put["iv_percentil"],
            "diferencial_call_put_pct": round(
                call["iv_atual_pct"] - put["iv_atual_pct"], 4
            ),
            "metodologia": general["metodologia"],
        })

    assets.sort(
        key=lambda row: (
            row["iv_percentil_geral"],
            row["iv_rank_geral"],
            row["pregoes"],
        ),
        reverse=True,
    )
    counts = Counter(row["cobertura"] for row in assets)
    return {
        "schema_version": 1,
        "data_referencia": max((row["data"] for row in assets), default=None),
        "janela_maxima_pregoes": window,
        "total_ativos": len(assets),
        "resumo_cobertura": {
            "completo": counts["completo"],
            "alto": counts["alto"],
            "parcial": counts["parcial"],
            "curto": counts["curto"],
        },
        "ativos": assets,
    }


def write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(rows[0]) if rows else []
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--window", type=int, default=250)
    args = parser.parse_args()
    if args.window < 20:
        parser.error("--window deve ser ao menos 20")
    payload = build_ranking(HISTORY_DIR, args.window)
    atomic_json(JSON_OUTPUT, payload)
    write_csv(CSV_OUTPUT, payload["ativos"])
    print(
        f"Ranking IV-DI: {payload['total_ativos']} ativos | "
        f"JSON: {JSON_OUTPUT} | CSV: {CSV_OUTPUT}"
    )


if __name__ == "__main__":
    main()
