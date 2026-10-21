"""Haalt Belgische elektriciteitsprijzen op en bewaart ze als CSV per jaar.

Bronnen:
  - Day-ahead (Belpex / EPEX SDAC): ENTSO-E Transparency Platform, documenttype A44.
    Vereist een API-token in de omgevingsvariabele ENTSOE_TOKEN.
  - Onbalansprijzen per kwartier: Elia Open Data (geen sleutel nodig).
  - Dagwaarnemingen Ukkel: KMI (laatste 60 dagen, geen sleutel nodig).
  - Uurwaarden van de automatische weerstations (AWS) van het KMI, met
    globale instraling: KMI open data (geen sleutel nodig).
  - PV-productie België per kwartier (gemeten en voorspeld): Elia Open Data.

Gebruik:
  python fetch_prices.py daily
  python fetch_prices.py backfill --source dayahead --start 2015-01-01
  python fetch_prices.py backfill --source imbalance --start 2024-01-01
  python fetch_prices.py backfill --source aws --start 2015-01-01
  python fetch_prices.py backfill --source solar --start 2020-10-01
  python fetch_prices.py daily --source weather
"""

import argparse
import csv
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

BRUSSELS = ZoneInfo("Europe/Brussels")
DATA_DIR = Path(__file__).parent / "data"

ENTSOE_URL = "https://web-api.tp.entsoe.eu/api"
ENTSOE_ZONE_BE = "10YBE----------2"

ELIA_URL = "https://opendata.elia.be/api/explore/v2.1/catalog/datasets/{dataset}/exports/json"
# Elia wisselde op 22/05/2024 (MARI) van dataset en methodologie.
ELIA_CUTOVER = datetime(2024, 5, 21, 22, 0, tzinfo=timezone.utc)
ELIA_OLD = "ods047"  # tot 22/05/2024
ELIA_NEW = "ods134"  # vanaf 22/05/2024

DAYAHEAD_FIELDS = ["datetime_utc", "datetime_brussels", "resolution_min", "price_eur_mwh"]
HOURLY_FIELDS = ["datetime_utc", "datetime_brussels", "price_eur_mwh", "source_resolution_min"]
IMBALANCE_FIELDS = [
    "datetime_utc",
    "datetime_brussels",
    "imbalance_price_eur_mwh",
    "system_imbalance_mw",
    "marginal_incremental_price_eur_mwh",
    "marginal_decremental_price_eur_mwh",
    "alpha",
    "quality_status",
    "source_dataset",
]

KMI_UKKEL_URL = "https://www.meteo.be/resources/climatology/uccle_month/Ukkel_waarnemingen.txt"
WEATHER_FIELDS = [
    "date",
    "temp_max_c",
    "temp_min_c",
    "temp_avg_c",
    "precipitation_mm",
    "wind_ms",
    "sunshine_hhmm",
    "sunshine_min",
]

KMI_AWS_URL = "https://opendata.meteo.be/service/aws/wfs"
# KMI-veld -> kolomnaam in onze CSV
AWS_COLUMNS = {
    "temp_dry_shelter_avg": "temp_c",
    "humidity_rel_shelter_avg": "humidity_pct",
    "pressure": "pressure_hpa",
    "precip_quantity": "precipitation_mm",
    "wind_speed_10m": "wind_speed_ms",
    "wind_gusts_speed": "wind_gusts_ms",
    "sun_duration": "sun_duration_min",
    "short_wave_from_sky_avg": "global_radiation_wm2",
    "sun_int_avg": "sun_int_wm2",
}
AWS_FIELDS = (
    ["datetime_utc", "datetime_brussels", "station_code", "station_name"]
    + list(AWS_COLUMNS.values())
    + ["radiation_validated", "temp_validated"]
)
AWS_STATION_FIELDS = ["station_code", "station_name", "latitude", "longitude", "altitude_m", "date_begin", "date_end"]

ELIA_PV = "ods032"
# Alle 14 regio's samen zouden ~60 MB per jaar geven; deze vier volstaan meestal.
# Mogelijke extra waarden: Antwerp, East-Flanders, Flemish-Brabant, Limburg,
# West-Flanders, Hainaut, Liège, Luxembourg, Namur, Walloon-Brabant.
PV_REGIONS = ["Belgium", "Flanders", "Wallonia", "Brussels"]
PV_COLUMNS = {
    "measured": "measured_mw",
    "mostrecentforecast": "most_recent_forecast_mw",
    "dayahead11hforecast": "dayahead_11h_forecast_mw",
    "dayaheadforecast": "dayahead_forecast_mw",
    "monitoredcapacity": "monitored_capacity_mw",
    "loadfactor": "load_factor_pct",
}
PV_FIELDS = ["datetime_utc", "datetime_brussels", "region"] + list(PV_COLUMNS.values())

session = requests.Session()
session.headers["User-Agent"] = "belpex-prijzen (github.com)"


def log(msg):
    print(msg, flush=True)


def warn(msg):
    # Op GitHub Actions verschijnt dit als gele waarschuwing in de run.
    print(f"::warning::{msg}" if os.environ.get("GITHUB_ACTIONS") else f"WAARSCHUWING: {msg}", flush=True)


def get(url, params, attempts=4):
    for attempt in range(1, attempts + 1):
        try:
            r = session.get(url, params=params, timeout=120)
            if r.status_code in (429, 500, 502, 503, 504):
                raise requests.HTTPError(f"HTTP {r.status_code}")
            return r
        except (requests.ConnectionError, requests.Timeout, requests.HTTPError) as e:
            if attempt == attempts:
                raise
            wait = 10 * attempt
            log(f"  fout ({e}), nieuwe poging over {wait}s")
            time.sleep(wait)


def local_midnight_utc(d):
    return datetime(d.year, d.month, d.day, tzinfo=BRUSSELS).astimezone(timezone.utc)


def iso_utc(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_local(dt):
    return dt.astimezone(BRUSSELS).isoformat()


def month_chunks(start, end):
    """Splitst [start, end) in stukken van maximaal één kalendermaand."""
    cur = start
    while cur < end:
        nxt = date(cur.year + (cur.month == 12), cur.month % 12 + 1, 1)
        yield cur, min(nxt, end)
        cur = nxt


def year_chunks(start, end):
    """Splitst [start, end) in stukken van maximaal één kalenderjaar."""
    cur = start
    while cur < end:
        nxt = date(cur.year + 1, 1, 1)
        yield cur, min(nxt, end)
        cur = nxt


# ---------------------------------------------------------------- opslag


def year_file(prefix, year):
    return DATA_DIR / f"{prefix}_{year}.csv"


def read_csv(path):
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write_csv(path, fields, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    tmp.replace(path)


def merge_into_year_files(prefix, fields, rows, key, replace_keys=None):
    """Voegt rijen toe aan data/<prefix>_<jaar>.csv (jaar volgens Brusselse tijd).

    Bestaande rijen met dezelfde sleutel worden overschreven. Rijen waarvan
    replace_keys(row) True geeft, worden eerst verwijderd.
    """
    by_year = defaultdict(list)
    for row in rows:
        by_year[int((row.get("datetime_brussels") or row["date"])[:4])].append(row)
    for year, new_rows in sorted(by_year.items()):
        path = year_file(prefix, year)
        existing = read_csv(path)
        before = len(existing)
        if replace_keys:
            existing = [r for r in existing if not replace_keys(r)]
        merged = {key(r): r for r in existing}
        merged.update((key(r), r) for r in new_rows)
        out = sorted(
            merged.values(),
            key=lambda r: (
                r.get("datetime_utc") or r["date"],
                r.get("resolution_min", ""),
                r.get("station_code", ""),
                r.get("region", ""),
            ),
        )
        write_csv(path, fields, out)
        log(f"  {path.name}: {len(out)} rijen ({len(out) - before:+d})")


# ---------------------------------------------------------------- ENTSO-E day-ahead


def parse_duration_min(res):
    # PT15M, PT30M, PT60M, PT1H
    res = res.removeprefix("PT")
    if res.endswith("H"):
        return int(res[:-1]) * 60
    return int(res[:-1])


def strip_ns(root):
    for el in root.iter():
        if "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    return root


def parse_a44(xml_text):
    """Geeft {(resolution_min, start_utc): price} terug.

    Curve type A03: ontbrekende posities hebben dezelfde prijs als de vorige.
    """
    root = strip_ns(ET.fromstring(xml_text))
    points = {}
    for ts in root.iter("TimeSeries"):
        seq = ts.findtext("classificationSequence_AttributeInstanceComponent.position")
        if seq not in (None, "1"):
            continue
        for period in ts.iter("Period"):
            start = datetime.fromisoformat(period.findtext("timeInterval/start").replace("Z", "+00:00"))
            end = datetime.fromisoformat(period.findtext("timeInterval/end").replace("Z", "+00:00"))
            step = parse_duration_min(period.findtext("resolution"))
            raw = {int(p.findtext("position")): float(p.findtext("price.amount")) for p in period.iter("Point")}
            n = int((end - start).total_seconds() // 60 // step)
            last = None
            for pos in range(1, n + 1):
                last = raw.get(pos, last)
                if last is not None:
                    points[(step, start + timedelta(minutes=step * (pos - 1)))] = last
    return points


def fetch_dayahead(token, start, end):
    """Day-ahead prijzen voor Brusselse dagen [start, end)."""
    params = {
        "securityToken": token,
        "documentType": "A44",
        "in_Domain": ENTSOE_ZONE_BE,
        "out_Domain": ENTSOE_ZONE_BE,
        "periodStart": local_midnight_utc(start).strftime("%Y%m%d%H%M"),
        "periodEnd": local_midnight_utc(end).strftime("%Y%m%d%H%M"),
    }
    r = get(ENTSOE_URL, params)
    if r.status_code == 401:
        raise RuntimeError("ENTSO-E weigert het token (401). Controleer de secret ENTSOE_TOKEN.")
    if "Acknowledgement_MarketDocument" in r.text:
        reason = strip_ns(ET.fromstring(r.text)).findtext(".//Reason/text") or r.text[:200]
        if "No matching data" in reason:
            return {}
        raise RuntimeError(f"ENTSO-E: {reason}")
    r.raise_for_status()
    return parse_a44(r.text)


def store_dayahead(points):
    if not points:
        return
    # Waar kwartierprijzen bestaan, is dat de echte marktprijs; een eventuele
    # uurwaarde voor hetzelfde uur laten we dan weg.
    quarter_hours = {t.replace(minute=0) for (step, t) in points if step < 60}
    rows = []
    for (step, t), price in sorted(points.items(), key=lambda kv: (kv[0][1], kv[0][0])):
        if step >= 60 and t in quarter_hours:
            continue
        rows.append(
            {
                "datetime_utc": iso_utc(t),
                "datetime_brussels": iso_local(t),
                "resolution_min": step,
                "price_eur_mwh": f"{price:g}",
            }
        )
    covered = {r["datetime_utc"][:13] for r in rows}
    merge_into_year_files(
        "dayahead",
        DAYAHEAD_FIELDS,
        rows,
        key=lambda r: (r["datetime_utc"], str(r["resolution_min"])),
        replace_keys=lambda r: r["datetime_utc"][:13] in covered,
    )

    # Uurgemiddelden
    hourly = defaultdict(list)
    for r in rows:
        t = datetime.fromisoformat(r["datetime_utc"].replace("Z", "+00:00"))
        step = int(r["resolution_min"])
        if step <= 60:
            hourly[t.replace(minute=0)].append((step, float(r["price_eur_mwh"])))
    hourly_rows = []
    for h, vals in sorted(hourly.items()):
        step = min(s for s, _ in vals)
        prices = [p for s, p in vals if s == step]
        if len(prices) * step != 60:
            continue  # onvolledig uur
        hourly_rows.append(
            {
                "datetime_utc": iso_utc(h),
                "datetime_brussels": iso_local(h),
                "price_eur_mwh": f"{round(sum(prices) / len(prices), 3):g}",
                "source_resolution_min": step,
            }
        )
    merge_into_year_files("dayahead_hourly", HOURLY_FIELDS, hourly_rows, key=lambda r: r["datetime_utc"])


def run_dayahead(start, end):
    token = os.environ.get("ENTSOE_TOKEN", "").strip()
    if not token:
        warn("Geen ENTSOE_TOKEN ingesteld: day-ahead prijzen worden overgeslagen.")
        return
    log(f"Day-ahead (ENTSO-E) {start} tot {end}")
    # ENTSO-E staat tot een jaar per verzoek toe; dat geeft veel minder verzoeken.
    for a, b in year_chunks(start, end):
        points = fetch_dayahead(token, a, b)
        log(f" {a} - {b}: {len(points)} prijzen")
        store_dayahead(points)
        time.sleep(1)


# ---------------------------------------------------------------- Elia onbalans


def fetch_elia(dataset, start_utc, end_utc):
    where = f'datetime >= "{iso_utc(start_utc)}" and datetime < "{iso_utc(end_utc)}"'
    r = get(ELIA_URL.format(dataset=dataset), {"where": where, "order_by": "datetime", "timezone": "UTC"})
    r.raise_for_status()
    return r.json()


def elia_row(rec, dataset):
    t = datetime.fromisoformat(rec["datetime"])
    if dataset == ELIA_OLD:
        # Voor 22/05/2024: aparte prijs voor positieve en negatieve onbalans,
        # in de praktijk (single pricing) bijna altijd gelijk.
        price = rec.get("positiveimbalanceprice")
    else:
        price = rec.get("imbalanceprice")

    def fmt(v):
        return "" if v is None else f"{v:g}"

    return {
        "datetime_utc": iso_utc(t),
        "datetime_brussels": iso_local(t),
        "imbalance_price_eur_mwh": fmt(price),
        "system_imbalance_mw": fmt(rec.get("systemimbalance")),
        "marginal_incremental_price_eur_mwh": fmt(rec.get("marginalincrementalprice")),
        "marginal_decremental_price_eur_mwh": fmt(rec.get("marginaldecrementalprice")),
        "alpha": fmt(rec.get("alpha")),
        "quality_status": rec.get("qualitystatus") or "",
        "source_dataset": dataset,
    }


def run_imbalance(start, end):
    log(f"Onbalansprijzen (Elia) {start} tot {end}")
    for a, b in month_chunks(start, end):
        a_utc, b_utc = local_midnight_utc(a), local_midnight_utc(b)
        rows = []
        for dataset, lo, hi in ((ELIA_OLD, a_utc, min(b_utc, ELIA_CUTOVER)), (ELIA_NEW, max(a_utc, ELIA_CUTOVER), b_utc)):
            if lo < hi:
                rows += [elia_row(rec, dataset) for rec in fetch_elia(dataset, lo, hi)]
        log(f" {a} - {b}: {len(rows)} kwartieren")
        if rows:
            merge_into_year_files("imbalance", IMBALANCE_FIELDS, rows, key=lambda r: r["datetime_utc"])


# ---------------------------------------------------------------- KMI weer Ukkel


def parse_kmi_ukkel(text):
    """Leest de vaste-breedte tabel van het KMI: één regel per dag."""
    rows = []
    for line in text.splitlines():
        parts = line.split()
        if not parts or not re.fullmatch(r"\d{2}-\d{2}-\d{4}", parts[0]):
            continue
        if len(parts) != 7:
            warn(f"KMI: onverwachte regel overgeslagen: {line.strip()!r}")
            continue
        d, tmax, tmin, tavg, rain, wind, sun = parts
        # Ontbrekende waarden (bv. "-") laten we leeg, zodat Power BI ze als leeg leest.
        tmax, tmin, tavg, rain, wind = (v if re.fullmatch(r"-?\d+(\.\d+)?", v) else "" for v in (tmax, tmin, tavg, rain, wind))
        day, month, year = d.split("-")
        sun_min = ""
        if re.fullmatch(r"\d{1,2}:\d{2}", sun):
            h, m = sun.split(":")
            sun_min = str(int(h) * 60 + int(m))
        else:
            sun = ""
        rows.append(
            {
                "date": f"{year}-{month}-{day}",
                "temp_max_c": tmax,
                "temp_min_c": tmin,
                "temp_avg_c": tavg,
                "precipitation_mm": rain,
                "wind_ms": wind,
                "sunshine_hhmm": sun,
                "sunshine_min": sun_min,
            }
        )
    return rows


def run_weather():
    log("Weer Ukkel (KMI), laatste 60 dagen")
    r = get(KMI_UKKEL_URL, {})
    r.raise_for_status()
    r.encoding = "utf-8"
    rows = parse_kmi_ukkel(r.text)
    log(f" {len(rows)} dagen")
    if not rows:
        warn("KMI: geen gegevens gevonden, is het formaat van het bestand veranderd?")
        return
    merge_into_year_files("weer_ukkel", WEATHER_FIELDS, rows, key=lambda r: r["date"])


# ---------------------------------------------------------------- KMI weerstations (AWS)


def fetch_aws_stations():
    r = get(
        KMI_AWS_URL,
        {"service": "WFS", "version": "2.0.0", "request": "GetFeature", "typeNames": "aws:aws_station", "outputFormat": "application/json"},
    )
    r.raise_for_status()
    stations = {}
    for feat in r.json()["features"]:
        p = feat["properties"]
        # GeoJSON: (lengte, breedte)
        lon, lat = feat["geometry"]["coordinates"]
        stations[str(p["code"])] = {
            "station_code": str(p["code"]),
            "station_name": p["name"].title(),
            "latitude": lat,
            "longitude": lon,
            "altitude_m": p.get("altitude"),
            "date_begin": (p.get("date_begin") or "")[:10],
            "date_end": (p.get("date_end") or "")[:10],
        }
    return stations


def fetch_aws_hours(start_utc, end_utc):
    # Het KMI geeft per uur de EINDtijd; uur "08:00" gaat over 07:00-08:00 UTC.
    # Daarom vragen we (start, end] op en zetten we om naar begintijd.
    cql = f"timestamp > '{iso_utc(start_utc)}' AND timestamp <= '{iso_utc(end_utc)}'"
    r = get(
        KMI_AWS_URL,
        {
            "service": "WFS",
            "version": "2.0.0",
            "request": "GetFeature",
            "typeNames": "aws:aws_1hour",
            "outputFormat": "application/json",
            "propertyName": ",".join(["code", "timestamp", "qc_flags", *AWS_COLUMNS]),
            "CQL_FILTER": cql,
        },
    )
    r.raise_for_status()
    return [f["properties"] for f in r.json()["features"]]


def aws_row(p, stations):
    end = datetime.fromisoformat(p["timestamp"].replace("Z", "+00:00"))
    start = end - timedelta(hours=1)
    try:
        validated = json.loads(p.get("qc_flags") or "{}").get("validated", {})
    except ValueError:
        validated = {}
    code = str(p["code"])
    row = {
        "datetime_utc": iso_utc(start),
        "datetime_brussels": iso_local(start),
        "station_code": code,
        "station_name": stations.get(code, {}).get("station_name", ""),
        "radiation_validated": str(validated.get("SHORT_WAVE_FROM_SKY_AVG", "")).lower(),
        "temp_validated": str(validated.get("TEMP_DRY_SHELTER_AVG", "")).lower(),
    }
    for src, col in AWS_COLUMNS.items():
        v = p.get(src)
        row[col] = "" if v is None else f"{v:g}"
    return row


def run_aws(start, end):
    log(f"Weerstations (KMI AWS) {start} tot {end}")
    stations = fetch_aws_stations()
    write_csv(DATA_DIR / "kmi_aws_stations.csv", AWS_STATION_FIELDS, sorted(stations.values(), key=lambda s: s["station_code"]))
    for a, b in month_chunks(start, end):
        rows = [aws_row(p, stations) for p in fetch_aws_hours(local_midnight_utc(a), local_midnight_utc(b))]
        log(f" {a} - {b}: {len(rows)} stationsuren")
        if rows:
            merge_into_year_files("kmi_aws_hourly", AWS_FIELDS, rows, key=lambda r: (r["datetime_utc"], r["station_code"]))


# ---------------------------------------------------------------- Elia PV-productie


def pv_row(rec):
    t = datetime.fromisoformat(rec["datetime"])
    row = {"datetime_utc": iso_utc(t), "datetime_brussels": iso_local(t), "region": rec["region"]}
    for src, col in PV_COLUMNS.items():
        v = rec.get(src)
        row[col] = "" if v is None else f"{v:g}"
    return row


def run_solar(start, end):
    log(f"PV-productie (Elia) {start} tot {end}, regio's: {', '.join(PV_REGIONS)}")
    regions = " or ".join(f'region = "{r}"' for r in PV_REGIONS)
    for a, b in month_chunks(start, end):
        where = (
            f'datetime >= "{iso_utc(local_midnight_utc(a))}" and datetime < "{iso_utc(local_midnight_utc(b))}"'
            f" and ({regions})"
        )
        r = get(ELIA_URL.format(dataset=ELIA_PV), {"where": where, "order_by": "datetime", "timezone": "UTC"})
        r.raise_for_status()
        rows = [pv_row(rec) for rec in r.json()]
        log(f" {a} - {b}: {len(rows)} kwartieren")
        if rows:
            merge_into_year_files("solar_pv", PV_FIELDS, rows, key=lambda r: (r["datetime_utc"], r["region"]))


# ---------------------------------------------------------------- main


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("mode", choices=["daily", "backfill"])
    p.add_argument("--source", choices=["all", "dayahead", "imbalance", "weather", "aws", "solar"], default="all")
    p.add_argument("--start", type=date.fromisoformat, help="eerste dag (JJJJ-MM-DD), Brusselse tijd")
    p.add_argument("--end", type=date.fromisoformat, help="dag na de laatste dag (standaard: overmorgen)")
    args = p.parse_args()

    today = datetime.now(BRUSSELS).date()
    end = args.end or today + timedelta(days=2)

    if args.mode == "daily":
        # Een paar dagen overlap: zo worden gemiste runs en latere correcties
        # (bv. Elia-validatie) automatisch bijgewerkt.
        da_start = args.start or today - timedelta(days=3)
        im_start = aws_start = pv_start = args.start or today - timedelta(days=10)
    else:
        da_start = args.start or date(2015, 1, 5)
        im_start = args.start or date(2024, 1, 1)
        aws_start = args.start or date(2015, 1, 1)
        pv_start = args.start or date(2020, 10, 1)  # begin van de Elia-dataset

    until_today = min(end, today + timedelta(days=1))
    jobs = {
        "dayahead": lambda: run_dayahead(da_start, end),
        "imbalance": lambda: run_imbalance(im_start, until_today),
        # Het KMI-bestand bevat altijd enkel de laatste 60 dagen.
        "weather": run_weather,
        "aws": lambda: run_aws(aws_start, until_today),
        "solar": lambda: run_solar(pv_start, until_today),
    }
    # Een storing bij één bron mag de andere niet tegenhouden.
    failed = []
    for name, job in jobs.items():
        if args.source not in ("all", name):
            continue
        try:
            job()
        except Exception as e:
            warn(f"{name} mislukt: {e}")
            failed.append(name)
    if failed:
        sys.exit(f"Mislukt: {', '.join(failed)}")


if __name__ == "__main__":
    main()
