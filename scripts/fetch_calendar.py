"""
scripts/fetch_calendar.py
Roda no GitHub Actions — busca calendário do Investing.com e salva em news/calendar.json
"""

import httpx
import re
import json
import os
from datetime import datetime, timedelta, timezone
from bs4 import BeautifulSoup

# ── Mapeamento país ───────────────────────────────────────────────────────────
COUNTRY_MAP = {
    "BR": {"name": "Brasil",       "flag": "🇧🇷"},
    "US": {"name": "EUA",          "flag": "🇺🇸"},
    "EU": {"name": "Zona do Euro", "flag": "🇪🇺"},
}

FLAG_NAME_MAP = {
    "brazil": "BR", "brasil": "BR",
    "united_states": "US", "unitedstates": "US", "usa": "US",
    "euro_zone": "EU", "eurozone": "EU",
}

def iso_to_label(iso: str) -> str:
    try:
        d = datetime.strptime(iso, "%Y-%m-%d")
        DAYS = ["Segunda","Terça","Quarta","Quinta","Sexta","Sábado","Domingo"]
        MONTHS = ["","Jan","Fev","Mar","Abr","Mai","Jun","Jul","Ago","Set","Out","Nov","Dez"]
        return f"{DAYS[d.weekday()]}, {d.day:02d} {MONTHS[d.month]} {d.year}"
    except:
        return iso

def fetch_calendar():
    today    = datetime.now()
    date_from = (today - timedelta(days=today.weekday())).strftime("%Y-%m-%d")
    date_to   = (today + timedelta(days=6 - today.weekday())).strftime("%Y-%m-%d")

    headers = {
        "User-Agent":       "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
        "Accept":           "application/json, text/javascript, */*; q=0.01",
        "Accept-Language":  "pt-BR,pt;q=0.9,en;q=0.8",
        "X-Requested-With": "XMLHttpRequest",
        "Referer":          "https://br.investing.com/economic-calendar/",
        "Origin":           "https://br.investing.com",
    }

    print(f"📅 Buscando calendário {date_from} → {date_to}...")

    resp = httpx.post(
        "https://br.investing.com/economic-calendar/Service/getCalendarFilteredData",
        headers=headers,
        data={
            "country[]":     ["32", "5"],
            "dateFrom":      date_from,
            "dateTo":        date_to,
            "timeZone":      "12",
            "timeFilter":    "timeOnly",
            "currentTab":    "custom",
            "submitFilters": "1",
            "limit_from":    "0",
        },
        timeout=30,
        follow_redirects=True,
    )
    resp.raise_for_status()
    html = resp.json().get("data", "")
    if not html:
        raise ValueError("Resposta vazia do Investing.com")

    soup        = BeautifulSoup(html, "html.parser")
    events      = []
    current_iso = ""
    current_lbl = ""

    for row in soup.select("tr"):
        row_id = row.get("id", "")

        # ── Separador de dia ──────────────────────────────────────────────
        if row_id.startswith("theDay"):
            raw = row_id.replace("theDay_", "").replace("theDay", "").strip()
            current_iso = ""
            m8 = re.fullmatch(r"(\d{4})(\d{2})(\d{2})", raw)
            if m8:
                current_iso = f"{m8.group(1)}-{m8.group(2)}-{m8.group(3)}"
            elif re.fullmatch(r"\d{9,11}", raw):
                dt = datetime.fromtimestamp(int(raw), tz=timezone.utc)
                current_iso = dt.strftime("%Y-%m-%d")
            current_lbl = iso_to_label(current_iso) if current_iso else raw
            continue

        if not row.select_one("td.time"):
            continue

        try:
            time_el     = row.select_one("td.time")
            country_el  = row.select_one("td.flagCur span")
            impact_el   = row.select_one("td.sentiment")
            name_el     = row.select_one("td.event a") or row.select_one("td.event")
            actual_el   = row.select_one("td.bold.act") or row.select_one("td[class*='act']")
            forecast_el = row.select_one("td.fore") or row.select_one("td[class*='fore']")
            previous_el = row.select_one("td.prev")

            # Impacto
            img_key    = impact_el.get("data-img_key", "") if impact_el else ""
            impact_str = {"bull3": "high", "bull2": "medium", "bull1": "low"}.get(img_key, "low")

            # País
            raw_classes  = country_el.get("class", []) if country_el else []
            country_code = ""
            for cls in raw_classes:
                c = cls.lower().replace("ceflags", "").strip()
                if not c: continue
                if len(c) == 2: country_code = c.upper(); break
                mapped = FLAG_NAME_MAP.get(c.replace("-","_"))
                if mapped: country_code = mapped; break
            country_info = COUNTRY_MAP.get(country_code, {"name": country_code or "—", "flag": "🌐"})

            def extract_val(el):
                if not el: return ""
                for tag in el.find_all(["i", "ins"]): tag.decompose()
                return el.get_text(strip=True)

            time_str = time_el.get_text(strip=True) if time_el else ""
            if time_str and ":" not in time_str and len(time_str) == 4:
                time_str = f"{time_str[:2]}:{time_str[2:]}"

            name = name_el.get_text(strip=True) if name_el else ""
            if not name: continue

            events.append({
                "id":           row.get("event_attr_id") or row_id,
                "date_iso":     current_iso,
                "date_label":   current_lbl,
                "time":         time_str,
                "sort_key":     f"{current_iso} {time_str.zfill(5)}",
                "country":      country_info["name"],
                "country_code": country_code,
                "flag":         country_info["flag"],
                "impact":       impact_str,
                "name":         name,
                "actual":       extract_val(actual_el),
                "forecast":     extract_val(forecast_el),
                "previous":     extract_val(previous_el),
            })

        except Exception as ex:
            continue

    events.sort(key=lambda e: e["sort_key"])
    print(f"✅ {len(events)} eventos coletados")
    return events

def main():
    events     = fetch_calendar()
    output     = {
        "events":     events,
        "updated_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source":     "Investing.com via GitHub Actions",
        "next_update": "~30 minutos",
    }

    os.makedirs("news", exist_ok=True)
    with open("news/calendar.json", "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"💾 Salvo em news/calendar.json ({len(events)} eventos)")

if __name__ == "__main__":
    main()
