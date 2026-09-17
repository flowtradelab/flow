import json
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from icalendar import Calendar

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "economic_events.json"
OUTPUT = ROOT / "calendaario-economico" / "calendar.json"
TREASURY = ROOT / "calendaario-economico" / "treasury-auctions.json"

TZ_BR = ZoneInfo("America/Sao_Paulo")
TZ_NY = ZoneInfo("America/New_York")
BLS_ICS = "https://www.bls.gov/schedule/news_release/bls.ics"
BLS_PAGE = "https://www.bls.gov/schedule/"
FOCUS_PAGE = "https://www.bcb.gov.br/publicacoes/focus"
TREASURY_PAGE = "https://treasurydirect.gov/auctions/upcoming/"

WEEKDAYS = ["Segunda", "Terça", "Quarta", "Quinta", "Sexta", "Sábado", "Domingo"]
MONTHS = ["Jan", "Fev", "Mar", "Abr", "Mai", "Jun", "Jul", "Ago", "Set", "Out", "Nov", "Dez"]


def load_config():
    return json.loads(CONFIG.read_text(encoding="utf-8"))


def label(dt):
    return f"{WEEKDAYS[dt.weekday()]}, {dt.day:02d} {MONTHS[dt.month - 1]} {dt.year}"


def event(item, dt, source_name, source_url, original_title=None, details=None):
    dt = dt.astimezone(TZ_BR)
    out = {
        "id": f"{item['event_id']}_{dt.strftime('%Y%m%d_%H%M')}",
        "event_id": item["event_id"],
        "date_iso": dt.date().isoformat(),
        "date_label": label(dt),
        "time": dt.strftime("%H:%M"),
        "sort_key": dt.strftime("%Y-%m-%d %H:%M"),
        "datetime": dt.isoformat(),
        "country": item["country"],
        "country_code": item["country_code"],
        "currency": item["currency"],
        "flag": item["flag"],
        "impact": item["impact"],
        "importance": item["importance"],
        "name": item["name"],
        "category": item["category"],
        "actual": "",
        "forecast": "",
        "previous": "",
        "status": "scheduled",
        "data_policy": item["data_policy"],
        "source_name": source_name,
        "source_url": source_url,
    }
    if original_title:
        out["original_title"] = original_title
    if details:
        out["details"] = details
    return out


def bls_events(config, start, end):
    response = httpx.get(
        BLS_ICS,
        timeout=30,
        follow_redirects=True,
        headers={"User-Agent": "flowtradelab-economic-calendar/1.0"},
    )
    response.raise_for_status()
    calendar = Calendar.from_ical(response.content)
    result = []

    for component in calendar.walk("VEVENT"):
        summary = str(component.get("summary", "")).strip()
        summary_lower = summary.casefold()
        item = None
        for candidate in config["events"]:
            if candidate.get("source") != "bls":
                continue
            if any(term.casefold() in summary_lower for term in candidate.get("match", [])):
                item = candidate
                break
        if not item:
            continue

        raw = component.decoded("dtstart")
        if isinstance(raw, datetime):
            dt = raw if raw.tzinfo else raw.replace(tzinfo=TZ_NY)
        else:
            dt = datetime.combine(raw, time(0), tzinfo=TZ_NY)
        dt = dt.astimezone(TZ_BR)

        if start <= dt.date() <= end:
            result.append(event(item, dt, "U.S. Bureau of Labor Statistics", BLS_PAGE, summary))
    return result


def focus_events(config, start, end):
    item = next(x for x in config["events"] if x["event_id"] == "BR_FOCUS")
    result = []
    current = start
    while current <= end:
        if current.weekday() == 0:
            dt = datetime.combine(current, time(8, 25), tzinfo=TZ_BR)
            result.append(event(item, dt, "Banco Central do Brasil", FOCUS_PAGE))
        current += timedelta(days=1)
    return result


def treasury_events(start, end):
    if not TREASURY.exists():
        return []
    payload = json.loads(TREASURY.read_text(encoding="utf-8"))
    result = []

    for auction in payload.get("auctions", []):
        if not auction.get("auction_date") or not auction.get("time_et"):
            continue
        auction_date = date.fromisoformat(auction["auction_date"])
        if not (start <= auction_date <= end):
            continue
        hour, minute = map(int, auction["time_et"].split(":"))
        dt = datetime.combine(auction_date, time(hour, minute), tzinfo=TZ_NY).astimezone(TZ_BR)
        term = auction.get("term", "")
        security_type = auction.get("type", "")
        item = {
            "event_id": f"US_TREASURY_{auction.get('id', auction['auction_date'])}",
            "country": "EUA",
            "country_code": "US",
            "currency": "USD",
            "flag": "🇺🇸",
            "impact": "low",
            "importance": 1,
            "name": f"Leilão do Tesouro dos EUA - {term} {security_type}".strip(),
            "category": "treasury_auction",
            "data_policy": "open",
        }
        details = {k: auction.get(k) for k in ("type", "term", "offering_bn", "fred")}
        result.append(event(item, dt, "U.S. Treasury", TREASURY_PAGE, details=details))
    return result


def main():
    config = load_config()
    now = datetime.now(TZ_BR)
    start = now.date()
    end = start + timedelta(days=int(config.get("window_days", 45)))
    events = []
    status = {}

    try:
        found = bls_events(config, start, end)
        events.extend(found)
        status["bls"] = f"ok ({len(found)} eventos)"
    except Exception as exc:
        status["bls"] = f"erro: {exc}"
        print("AVISO BLS:", exc)

    found = focus_events(config, start, end)
    events.extend(found)
    status["bcb_focus"] = f"ok ({len(found)} eventos)"

    found = treasury_events(start, end)
    events.extend(found)
    status["treasury"] = f"ok ({len(found)} eventos)"

    events = list({item["id"]: item for item in events}.values())
    events.sort(key=lambda item: item["datetime"])

    payload = {
        "generated_at": now.isoformat(),
        "timezone": "America/Sao_Paulo",
        "window": {"from": start.isoformat(), "to": end.isoformat()},
        "source_status": status,
        "events": events,
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Calendário atualizado: {len(events)} eventos")


if __name__ == "__main__":
    main()
