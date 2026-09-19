"""Maintain up to 30 published B3 sessions, independently of latest.json.

Install: python -m pip install requests yfinance
Run from any directory: python scripts/update_open_interest_hist_30.py
Optional: --base PETR --end-date 2026-09-18 --lookback-days 90

Backfills dated B3 sources, never repeats today's OI into earlier sessions.
Only completed days (before today in BRT) are eligible. Missing/unpublished
sources are reported; fewer than 30 available sessions produces a partial
history with an explicit session count. Existing dates survive unavailable
sources. Each daily row groups by underlying, expiry, type and strike;
qtd_descoberta is the B3 posDe (uncovered short positions), summed per group.
Legacy snapshots without this field retain null until successfully refreshed.
spot_fechamento maps each underlying to its same-date unadjusted Yahoo Close.
Missing quotes are null, never forward-filled. Re-running retries quotes.
Writes only grid-options/<BASE>/oi-hist-30.json (atomic per file).
"""
import argparse
import json
import math
import os
import re
import tempfile
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

import update_options as b3

WINDOW = 30
OUTPUT = Path(__file__).resolve().parents[1] / "grid-options"


def aggregate(options):
    totals = defaultdict(lambda: [0, 0])
    for option in options:
        key = (option["ativo_objeto"], option["vencimento"],
               option["tipo"], option["strike"])
        oi = option["open_interest"]
        if not math.isfinite(key[3]) or key[3] <= 0 or oi < 0:
            raise ValueError("Invalid strike or open interest")
        naked = option.get("qtd_descoberta")
        if naked is not None and (not math.isfinite(naked) or naked < 0):
            raise ValueError("Invalid uncovered quantity")
        totals[key][0] += oi
        if naked is None:
            totals[key][1] = None
        elif totals[key][1] is not None:
            totals[key][1] += naked
    return [
        dict(ativo_objeto=u, vencimento=e, tipo=t, strike=k, open_interest=oi,
             qtd_descoberta=naked)
        for (u, e, t, k), (oi, naked) in sorted(totals.items())
    ]


def fetch_day(day):
    """Use metadata and positions from exactly the requested date."""
    result = b3.download_positions_json(day.strftime("%Y%m%d"))
    if result is None:
        return None
    token = b3.get_download_token(b3.INSTRUMENTS_FILE, day.isoformat())
    if not token:
        return None
    raw = b3.download_csv_from_token(token, b3.INSTRUMENTS_FILE)
    rows, status = b3.parse_b3_csv(raw)
    if not rows or (status and status.lower() != "final"):
        return None
    instruments = b3.parse_instruments(rows)
    positions, stats = b3.parse_positions_json(result[0])
    options, joined = b3.build_options(instruments, positions)
    b3.validate_sources(len(instruments), len(positions), joined["matched"],
                        stats["sanity_ratio"])
    if stats["duplicates"] or stats["invalid_type"]:
        raise ValueError("Duplicate series or unknown option types in B3 data")
    return {base: aggregate(items) for base, items in options.items()}


def read_histories(root, base=None):
    histories = {}
    paths = [root / base / "oi-hist-30.json"] if base else sorted(
        root.glob("*/oi-hist-30.json"))
    for path in paths:
        if not path.exists():
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("schema_version") != 1 or payload.get("ticker") != path.parent.name:
            raise ValueError(f"Unexpected history schema: {path}")
        days = {}
        for row in payload["historico"]:
            key = date.fromisoformat(row["data"]).isoformat()
            if key in days:
                raise ValueError(f"Duplicate session in {path}: {key}")
            for item in row["strikes"]:
                item.setdefault("qtd_descoberta", None)
            days[key] = row
        histories[path.parent.name] = days
    return histories


def fetch_closes(underlying, start, end):
    import yfinance as yf

    if not re.fullmatch(r"[A-Z0-9]{4}[0-9]{1,2}", underlying):
        print(f"Unresolved underlying: {underlying}; spot=null")
        return {}
    frame = yf.Ticker(underlying + ".SA").history(
        start=start, end=(date.fromisoformat(end) + timedelta(days=1)).isoformat(),
        interval="1d", auto_adjust=False, back_adjust=False, actions=False,
        raise_errors=True,
    )
    if frame.empty:
        return {}
    result = {}
    for stamp, value in frame["Close"].items():
        close = float(value)
        if math.isfinite(close) and close > 0:
            result[stamp.date().isoformat()] = close
    return result


def enrich(histories):
    dates_by_underlying = defaultdict(set)
    for days in histories.values():
        for day, row in days.items():
            for item in row["strikes"]:
                dates_by_underlying[item["ativo_objeto"]].add(day)
    closes = {}
    for underlying, dates in sorted(dates_by_underlying.items()):
        try:
            closes[underlying] = fetch_closes(underlying, min(dates), max(dates))
        except Exception as exc:
            print(f"Yahoo unavailable for {underlying}: {exc}")
            closes[underlying] = {}
    for days in histories.values():
        for day, row in days.items():
            previous = row.get("spot_fechamento", {})
            row["spot_fechamento"] = {
                underlying: closes[underlying].get(day, previous.get(underlying))
                for underlying in sorted({x["ativo_objeto"] for x in row["strikes"]})
            }


def save_history(path, base, days):
    rows = [days[key] for key in sorted(days)[-WINDOW:]]
    payload = dict(schema_version=1, ticker=base, janela_pregoes=WINDOW,
                   total_pregoes=len(rows), fonte_oi=b3.POSITIONS_JSON,
                   fonte_spot="yfinance Close (auto_adjust=False)",
                   historico=rows)
    content = json.dumps(payload, ensure_ascii=False, allow_nan=False,
                         separators=(",", ":")).encode("utf-8") + b"\n"
    if path.exists() and path.read_bytes() == content:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            temp = Path(stream.name)
            stream.write(content)
        os.replace(temp, path)
    finally:
        if temp is not None and temp.exists():
            temp.unlink()
    return True


def update(root, end, lookback_days=90, base=None):
    histories = read_histories(root, base)
    sessions = set()
    # Always re-fetch available B3 sources, including revisions. Existing
    # snapshots are fallback only, not evidence that another base has no data.
    for offset in range(lookback_days):
        day = end - timedelta(days=offset)
        if day.weekday() >= 5:
            continue
        key = day.isoformat()
        data = fetch_day(day)
        if data is None:
            print(f"{key}: sources unavailable; preserving any stored snapshot")
            if any(key in days for days in histories.values()):
                sessions.add(key)
        else:
            sessions.add(key)
            for ticker, strikes in data.items():
                if base and ticker != base:
                    continue
                days = histories.setdefault(ticker, {})
                previous = days.get(key, {})
                days[key] = dict(data=key, strikes=strikes,
                                 spot_fechamento=previous.get("spot_fechamento", {}))
        if len(sessions) == WINDOW:
            break
    if not sessions:
        raise RuntimeError("No published sessions found; history left untouched")
    cutoff = min(sessions)
    # Keep only sessions in the requested rolling window. Preserve future
    # entries on historical reruns, so a backfill cannot rewind stored history.
    for ticker, days in histories.items():
        retained = {k: v for k, v in days.items() if k >= cutoff}
        histories[ticker] = {k: retained[k] for k in sorted(retained)[-WINDOW:]}
    enrich(histories)
    changed = 0
    for ticker, days in sorted(histories.items()):
        if not days:
            continue
        changed += save_history(root / ticker / "oi-hist-30.json", ticker, days)
        print(f"{ticker}: {len(days)}/{WINDOW} sessions")
    print(f"Updated {changed} history files")
    return changed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=str.upper)
    parser.add_argument("--end-date", type=date.fromisoformat,
                        default=datetime.now(b3.BRT).date() - timedelta(days=1))
    parser.add_argument("--lookback-days", type=int, default=90)
    args = parser.parse_args()
    if args.base and not re.fullmatch(r"[A-Z0-9]{4}", args.base):
        parser.error("--base must contain four letters/digits")
    if args.lookback_days < WINDOW:
        parser.error("--lookback-days must be at least 30")
    if args.end_date >= datetime.now(b3.BRT).date():
        parser.error("--end-date must precede today in BRT (completed sessions only)")
    update(OUTPUT, args.end_date, args.lookback_days, args.base)


if __name__ == "__main__":
    main()
