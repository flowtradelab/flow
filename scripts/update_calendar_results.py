import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
CALENDAR = ROOT / "calendaario-economico" / "calendar.json"
CACHE = ROOT / "calendaario-economico" / "calendar-results.json"
TZ_BR = ZoneInfo("America/Sao_Paulo")

HEADERS = {
    "User-Agent": "flowtradelab-economic-calendar/3.0 (+https://github.com/flowtradelab/flow)"
}

BLS_RELEASES = {
    "US_CPI": "https://www.bls.gov/news.release/cpi.nr0.htm",
    "US_PPI": "https://www.bls.gov/news.release/ppi.nr0.htm",
    "US_PAYROLL": "https://www.bls.gov/news.release/empsit.nr0.htm",
    "US_JOLTS": "https://www.bls.gov/news.release/jolts.nr0.htm",
}

FED_G17_IDS = {
    "US_INDUSTRIAL_PRODUCTION",
    "US_CAPACITY_UTILIZATION",
    "US_MANUFACTURING_PRODUCTION",
}
FED_G17_URL = "https://www.federalreserve.gov/releases/g17/current/table0.htm"

MONTHS = (
    "January|February|March|April|May|June|July|August|September|October|November|December"
)


def load_json(path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def normalize_text(html):
    soup = BeautifulSoup(html, "html.parser")
    return re.sub(r"\s+", " ", soup.get_text(" ", strip=True)).strip(), soup


def release_date_from_text(text):
    pattern = rf"(?:For release|embargoed until).*?(({MONTHS})\s+\d{{1,2}},\s+\d{{4}})"
    match = re.search(pattern, text, re.I)
    if not match:
        return None
    return datetime.strptime(match.group(1), "%B %d, %Y").date()


def release_date_fed(text):
    match = re.search(rf"Release Date:\s*(({MONTHS})\s+\d{{1,2}},\s+\d{{4}})", text, re.I)
    if not match:
        return None
    return datetime.strptime(match.group(1), "%B %d, %Y").date()


def signed_value(verb, value, suffix="%"):
    negative_words = ("decreas", "fell", "fall", "declin", "moved down", "dropped")
    number = float(value.replace(",", ""))
    if any(word in verb.casefold() for word in negative_words):
        number = -abs(number)
    else:
        number = abs(number)
    if number == 0:
        number = 0.0
    return f"{number:g}{suffix}"


def signed_thousands(value):
    number = int(value.replace(",", ""))
    thousands = number / 1000
    sign = "-" if thousands < 0 else ""
    absolute = abs(thousands)
    formatted = f"{absolute:g}"
    return f"{sign}{formatted}K"


def parser_cpi(text):
    current = re.search(
        r"CPI-U\)\s+(increased|decreased|rose|fell)\s+([0-9.]+)\s+percent.*?"
        r"\bin\s+[A-Za-z]+\s+after\s+(rising|increasing|falling|decreasing|declining)\s+"
        r"([0-9.]+)\s+percent\s+in\s+[A-Za-z]+",
        text,
        re.I,
    )
    if not current:
        return None
    return {
        "actual": signed_value(current.group(1), current.group(2)),
        "previous": signed_value(current.group(3), current.group(4)),
    }


def parser_ppi(text):
    current = re.search(
        r"Producer Price Index for final demand\s+"
        r"(moved up|moved down|rose|fell|increased|decreased)\s+([0-9.]+)\s+percent\s+in\s+[A-Za-z]+",
        text,
        re.I,
    )
    previous = re.search(
        r"Final demand prices\s+(rose|fell|increased|decreased)\s+([0-9.]+)\s+percent\s+in\s+[A-Za-z]+",
        text,
        re.I,
    )
    if not current:
        return None
    return {
        "actual": signed_value(current.group(1), current.group(2)),
        "previous": signed_value(previous.group(1), previous.group(2)) if previous else "",
    }


def parser_payroll(text):
    current = re.search(
        r"Total nonfarm payroll employment\s+(increased|decreased|rose|fell)\s+by\s+([0-9,]+)",
        text,
        re.I,
    )
    if not current:
        return None

    current_number = int(current.group(2).replace(",", ""))
    if current.group(1).casefold() in {"decreased", "fell"}:
        current_number *= -1

    revisions = re.findall(
        r"change for\s+[A-Za-z]+\s+was revised.*?\s+to\s+([+-]?[0-9,]+)",
        text,
        re.I,
    )
    previous = signed_thousands(revisions[-1]) if revisions else ""
    return {"actual": signed_thousands(str(current_number)), "previous": previous}


def parser_jolts(text):
    current = re.search(
        r"number of job openings.*?\bat\s+([0-9.]+)\s+million\s+in\s+[A-Za-z]+",
        text,
        re.I,
    )
    if not current:
        return None
    previous = re.search(
        r"number of job openings for\s+[A-Za-z]+\s+was revised.*?\s+to\s+([0-9.]+)\s+million",
        text,
        re.I,
    )
    return {
        "actual": f"{current.group(1)}M",
        "previous": f"{previous.group(1)}M" if previous else "",
    }


BLS_PARSERS = {
    "US_CPI": parser_cpi,
    "US_PPI": parser_ppi,
    "US_PAYROLL": parser_payroll,
    "US_JOLTS": parser_jolts,
}


def fetch_bls_result(event):
    event_id = event["event_id"]
    url = BLS_RELEASES.get(event_id)
    parser = BLS_PARSERS.get(event_id)
    if not url or not parser:
        return None

    response = httpx.get(url, timeout=30, follow_redirects=True, headers=HEADERS)
    response.raise_for_status()
    text, _ = normalize_text(response.text)
    release_date = release_date_from_text(text)
    if release_date != date.fromisoformat(event["date_iso"]):
        return None

    result = parser(text)
    if not result:
        return None
    result["result_source_url"] = url
    result["release_date"] = release_date.isoformat()
    return result


def row_numbers(soup, prefix):
    for row in soup.find_all("tr"):
        cells = [cell.get_text(" ", strip=True) for cell in row.find_all(["th", "td"])]
        if not cells:
            continue
        if not cells[0].casefold().startswith(prefix.casefold()):
            continue
        numbers = []
        for cell in cells[1:]:
            match = re.fullmatch(r"\s*([+-]?(?:\d+(?:\.\d+)?|\.\d+))\s*", cell)
            if match:
                numbers.append(float(match.group(1)))
        if numbers:
            return numbers
    return []


def fmt_pct(value):
    return f"{value:g}%"


def fetch_fed_g17_results(events):
    response = httpx.get(FED_G17_URL, timeout=30, follow_redirects=True, headers=HEADERS)
    response.raise_for_status()
    text, soup = normalize_text(response.text)
    release_date = release_date_fed(text)
    if not release_date:
        return {}

    total_index = row_numbers(soup, "Total index")
    manufacturing = row_numbers(soup, "Manufacturing")
    total_industry = row_numbers(soup, "Total industry")
    output = {}

    for event in events:
        if release_date != date.fromisoformat(event["date_iso"]):
            continue
        event_id = event["event_id"]
        numbers = []
        if event_id == "US_INDUSTRIAL_PRODUCTION":
            numbers = total_index
        elif event_id == "US_MANUFACTURING_PRODUCTION":
            numbers = manufacturing
        elif event_id == "US_CAPACITY_UTILIZATION":
            numbers = total_industry
        if len(numbers) < 3:
            continue

        output[event_id] = {
            "actual": fmt_pct(numbers[-2]),
            "previous": fmt_pct(numbers[-3]),
            "result_source_url": FED_G17_URL,
            "release_date": release_date.isoformat(),
        }
    return output


def event_key(event):
    return f"{event['event_id']}|{event['date_iso']}"


def apply_result(event, result, now):
    changed = False
    for field in ("actual", "previous", "result_source_url", "release_date"):
        value = result.get(field)
        if value is None:
            continue
        if event.get(field) != value:
            if field == "previous" and event.get(field) and event.get(field) != value:
                event.setdefault("previous_original", event[field])
            event[field] = value
            changed = True

    if event.get("status") != "released":
        event["status"] = "released"
        changed = True
    if changed:
        event["result_updated_at"] = now.isoformat()
    return changed


def due(event, now):
    if event.get("data_policy") != "open":
        return False
    try:
        scheduled = datetime.fromisoformat(event["datetime"])
    except (KeyError, ValueError):
        return False
    if scheduled.tzinfo is None:
        scheduled = scheduled.replace(tzinfo=TZ_BR)
    return scheduled <= now <= scheduled + timedelta(minutes=120)


def cached_payload(event):
    return {
        field: event[field]
        for field in (
            "actual", "previous", "previous_original", "status", "result_source_url",
            "release_date", "result_updated_at",
        )
        if event.get(field) not in (None, "")
    }


def main():
    if not CALENDAR.exists():
        print("calendar.json ainda não existe; rode primeiro o workflow de agenda.")
        return

    calendar = load_json(CALENDAR, {"events": []})
    cache = load_json(CACHE, {"results": {}})
    cache.setdefault("results", {})
    now = datetime.now(TZ_BR)

    calendar_before = json.dumps(calendar, ensure_ascii=False, sort_keys=True)
    cache_before = json.dumps(cache, ensure_ascii=False, sort_keys=True)

    # Reaplica resultados persistidos depois de cada regeneração da agenda.
    for event in calendar.get("events", []):
        saved = cache["results"].get(event_key(event))
        if saved:
            apply_result(event, saved, now)

    pending = [event for event in calendar.get("events", []) if due(event, now) and not event.get("actual")]

    # BLS: consulta somente as páginas dos eventos efetivamente na janela de divulgação.
    for event in pending:
        if event.get("event_id") not in BLS_RELEASES:
            continue
        try:
            result = fetch_bls_result(event)
        except Exception as exc:
            print(f"AVISO BLS {event.get('event_id')}: {exc}")
            continue
        if result and apply_result(event, result, now):
            cache["results"][event_key(event)] = cached_payload(event)

    # Federal Reserve G.17: uma única consulta atualiza os três componentes do release.
    fed_pending = [event for event in pending if event.get("event_id") in FED_G17_IDS]
    if fed_pending:
        try:
            fed_results = fetch_fed_g17_results(fed_pending)
        except Exception as exc:
            print(f"AVISO FED G17: {exc}")
            fed_results = {}
        for event in fed_pending:
            result = fed_results.get(event.get("event_id"))
            if result and apply_result(event, result, now):
                cache["results"][event_key(event)] = cached_payload(event)

    calendar_after = json.dumps(calendar, ensure_ascii=False, sort_keys=True)
    cache_after = json.dumps(cache, ensure_ascii=False, sort_keys=True)

    if calendar_after != calendar_before:
        calendar["results_updated_at"] = now.isoformat()
        write_json(CALENDAR, calendar)
        print("calendar.json atualizado com resultados.")
    else:
        print("Nenhum novo resultado para calendar.json.")

    if cache_after != cache_before:
        cache["updated_at"] = now.isoformat()
        write_json(CACHE, cache)
        print("Cache de resultados atualizado.")


if __name__ == "__main__":
    main()
