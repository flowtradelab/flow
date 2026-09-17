import hashlib
import json
import re
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from bs4 import BeautifulSoup
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
CENSUS_PAGE = "https://www.census.gov/economic-indicators/calendar-listview.html"
FED_MONTH_URL = "https://www.federalreserve.gov/newsevents/{year}-{month}.htm"
NYFED_MONTH_URL = "https://www.newyorkfed.org/research/calendars/i-{month}{yy}.html"

HEADERS = {
    "User-Agent": "flowtradelab-economic-calendar/2.0 (+https://github.com/flowtradelab/flow)"
}

WEEKDAYS = ["Segunda", "Terça", "Quarta", "Quinta", "Sexta", "Sábado", "Domingo"]
MONTHS_PT = ["Jan", "Fev", "Mar", "Abr", "Mai", "Jun", "Jul", "Ago", "Set", "Out", "Nov", "Dez"]
MONTHS_EN = [
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
]
MONTHS_NYFED = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]

FED_SECTIONS = {
    "Speeches", "Testimony", "FOMC Meetings", "Board Meetings",
    "Beige Book", "Statistical Releases", "Conferences", "Other",
}

TIME_ET_RE = re.compile(r"^(\d{1,2}):(\d{2})\s+(a\.m\.|p\.m\.)$", re.I)
DATE_LIST_RE = re.compile(r"^\d{1,2}(?:\s*,\s*\d{1,2})*$")
NYFED_TIME_RE = re.compile(r"^\((\d{1,2}):(\d{2})\)$")


def load_config():
    return json.loads(CONFIG.read_text(encoding="utf-8"))


def label(dt):
    return f"{WEEKDAYS[dt.weekday()]}, {dt.day:02d} {MONTHS_PT[dt.month - 1]} {dt.year}"


def matching_items(config, source, title):
    title_key = title.casefold()
    matches = []
    for candidate in config["events"]:
        if candidate.get("source") != source:
            continue
        terms = candidate.get("match", [])
        if any(term.casefold() in title_key for term in terms):
            matches.append(candidate)
    return matches


def build_event(item, dt, source_name, source_url, original_title=None, details=None):
    dt = dt.astimezone(TZ_BR)
    name = original_title if item.get("use_original_title") and original_title else item["name"]
    event_id = f"{item['event_id']}_{dt.strftime('%Y%m%d_%H%M')}"
    if item.get("use_original_title") and original_title:
        digest = hashlib.sha1(original_title.encode("utf-8")).hexdigest()[:8]
        event_id = f"{event_id}_{digest}"

    out = {
        "id": event_id,
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
        "name": name,
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


def month_pairs(start, end):
    current = date(start.year, start.month, 1)
    stop = date(end.year, end.month, 1)
    while current <= stop:
        yield current.year, current.month
        if current.month == 12:
            current = date(current.year + 1, 1, 1)
        else:
            current = date(current.year, current.month + 1, 1)


def et_datetime(day_date, hour, minute):
    return datetime.combine(day_date, time(hour, minute), tzinfo=TZ_NY).astimezone(TZ_BR)


def parse_fed_time(value):
    match = TIME_ET_RE.match(value.strip())
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2))
    meridiem = match.group(3).lower()
    if hour == 12:
        hour = 0
    if meridiem.startswith("p"):
        hour += 12
    return hour, minute


def parse_clock_12h(value):
    cleaned = re.sub(r"\s+", " ", value.strip().upper())
    return datetime.strptime(cleaned, "%I:%M %p").time()


def bls_events(config, start, end):
    response = httpx.get(BLS_ICS, timeout=30, follow_redirects=True, headers=HEADERS)
    response.raise_for_status()
    calendar = Calendar.from_ical(response.content)
    result = []

    for component in calendar.walk("VEVENT"):
        summary = str(component.get("summary", "")).strip()
        items = matching_items(config, "bls", summary)
        if not items:
            continue

        raw = component.decoded("dtstart")
        if isinstance(raw, datetime):
            dt = raw if raw.tzinfo else raw.replace(tzinfo=TZ_NY)
        else:
            dt = datetime.combine(raw, time(0), tzinfo=TZ_NY)
        dt = dt.astimezone(TZ_BR)

        if not (start <= dt.date() <= end):
            continue

        for item in items:
            result.append(build_event(item, dt, "U.S. Bureau of Labor Statistics", BLS_PAGE, summary))
    return result


def census_events(config, start, end):
    response = httpx.get(CENSUS_PAGE, timeout=30, follow_redirects=True, headers=HEADERS)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    result = []

    for row in soup.find_all("tr"):
        cells = [cell.get_text(" ", strip=True) for cell in row.find_all(["th", "td"])]
        if len(cells) < 4:
            continue

        indicator, release_text, time_text, period = cells[:4]
        items = matching_items(config, "census", indicator)
        if not items or release_text.casefold() == "suspended":
            continue

        try:
            release_date = datetime.strptime(release_text, "%B %d, %Y").date()
            release_time = parse_clock_12h(time_text)
        except ValueError:
            continue

        if not (start <= release_date <= end):
            continue

        dt = datetime.combine(release_date, release_time, tzinfo=TZ_NY).astimezone(TZ_BR)
        details = {"period": period, "indicator": indicator}
        for item in items:
            result.append(build_event(
                item, dt, "U.S. Census Bureau", CENSUS_PAGE,
                original_title=indicator, details=details,
            ))
    return result


def fed_month_events(config, year, month, start, end):
    month_name = MONTHS_EN[month - 1]
    url = FED_MONTH_URL.format(year=year, month=month_name)
    response = httpx.get(url, timeout=30, follow_redirects=True, headers=HEADERS)
    if response.status_code == 404:
        return []
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    lines = [line.strip() for line in soup.get_text("\n", strip=True).splitlines() if line.strip()]
    result = []
    section = None
    i = 0

    while i < len(lines):
        line = lines[i]
        if line in FED_SECTIONS:
            section = line
            i += 1
            continue

        clock = parse_fed_time(line)
        if not clock or not section:
            i += 1
            continue

        title_idx = i + 1
        while title_idx < len(lines) and lines[title_idx] in {"Time:", "Release Date(s):", "Watch Live", "Video"}:
            title_idx += 1
        if title_idx >= len(lines):
            break

        title = lines[title_idx]
        items = matching_items(config, "fed", title)
        date_idx = title_idx + 1

        while date_idx < len(lines):
            candidate = lines[date_idx]
            if candidate in FED_SECTIONS or parse_fed_time(candidate):
                break
            if DATE_LIST_RE.match(candidate):
                break
            date_idx += 1

        if items and date_idx < len(lines) and DATE_LIST_RE.match(lines[date_idx]):
            days = [int(x.strip()) for x in lines[date_idx].split(",")]
            for day in days:
                try:
                    release_date = date(year, month, day)
                except ValueError:
                    continue
                if not (start <= release_date <= end):
                    continue
                dt = et_datetime(release_date, *clock)
                details = {"section": section}
                for item in items:
                    result.append(build_event(
                        item, dt, "Federal Reserve Board", url,
                        original_title=title, details=details,
                    ))
            i = date_idx + 1
        else:
            i = title_idx + 1

    return result


def fed_events(config, start, end):
    result = []
    for year, month in month_pairs(start, end):
        result.extend(fed_month_events(config, year, month, start, end))
    return result


def nyfed_month_events(config, year, month, start, end):
    slug = MONTHS_NYFED[month - 1]
    url = NYFED_MONTH_URL.format(month=slug, yy=str(year)[-2:])
    response = httpx.get(url, timeout=30, follow_redirects=True, headers=HEADERS)
    if response.status_code == 404:
        return []
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    result = []

    for cell in soup.find_all("td"):
        lines = [x.strip() for x in cell.stripped_strings if x.strip()]
        if not lines:
            continue

        day = None
        for token in lines[:2]:
            if token.isdigit() and 1 <= int(token) <= 31:
                day = int(token)
                break
        if day is None:
            continue

        try:
            release_date = date(year, month, day)
        except ValueError:
            continue
        if not (start <= release_date <= end):
            continue

        for idx, line in enumerate(lines):
            match = NYFED_TIME_RE.match(line)
            if not match or idx == 0:
                continue
            title = lines[idx - 1]
            items = matching_items(config, "nyfed", title)
            if not items:
                continue

            hour = int(match.group(1))
            minute = int(match.group(2))
            dt = et_datetime(release_date, hour, minute)
            for item in items:
                result.append(build_event(
                    item, dt, "Federal Reserve Bank of New York", url,
                    original_title=title,
                ))
    return result


def nyfed_events(config, start, end):
    result = []
    for year, month in month_pairs(start, end):
        result.extend(nyfed_month_events(config, year, month, start, end))
    return result


def focus_events(config, start, end):
    item = next(x for x in config["events"] if x["event_id"] == "BR_FOCUS")
    result = []
    current = start
    while current <= end:
        if current.weekday() == 0:
            dt = datetime.combine(current, time(8, 25), tzinfo=TZ_BR)
            result.append(build_event(item, dt, "Banco Central do Brasil", FOCUS_PAGE))
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
        dt = et_datetime(auction_date, hour, minute)
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
        result.append(build_event(item, dt, "U.S. Treasury", TREASURY_PAGE, details=details))
    return result


def collect(status, events, name, function, *args):
    try:
        found = function(*args)
        events.extend(found)
        status[name] = f"ok ({len(found)} eventos)"
    except Exception as exc:
        status[name] = f"erro: {exc}"
        print(f"AVISO {name}: {exc}")


def main():
    config = load_config()
    now = datetime.now(TZ_BR)
    start = now.date()
    end = start + timedelta(days=int(config.get("window_days", 45)))
    events = []
    status = {}

    collect(status, events, "bls", bls_events, config, start, end)
    collect(status, events, "census", census_events, config, start, end)
    collect(status, events, "fed", fed_events, config, start, end)
    collect(status, events, "nyfed", nyfed_events, config, start, end)
    collect(status, events, "bcb_focus", focus_events, config, start, end)
    collect(status, events, "treasury", treasury_events, start, end)

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
