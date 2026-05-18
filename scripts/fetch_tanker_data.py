#!/usr/bin/env python3
"""Fetch and cache AFE tanker port-call data for the static dashboard.

This script is designed for GitHub Actions. It reads the ArcGIS Daily_Ports_Data
FeatureServer once per scheduled run, aggregates records to cumulative day-of-year
series, and writes a small JSON file that the dashboard can load quickly.

Important implementation details:
- The API truncates country-level queries at ~4000 records (~June 15 for daily data).
  To get the full year, we query each port individually and aggregate the results.
- The API has both `year` and `date` fields. We treat `year` as authoritative
  for the series year and use `date` only to calculate day-of-year. This avoids
  losing a year if the date field is returned in an unexpected format.
- We use maxRecordCountFactor=5 to allow fetching up to 10,000 records per request.
"""

from __future__ import annotations

import json
import re
import ssl
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

BASE_URL = "https://services9.arcgis.com/weJ1QsnbMYJlCHdG/ArcGIS/rest/services/Daily_Ports_Data/FeatureServer/0/query"
OUTFILE = Path("dashboard/data/tanker_data.json")
MIN_YEAR = 2024
MAX_YEAR = datetime.now(timezone.utc).year
YEARS = list(range(MIN_YEAR, MAX_YEAR + 1))
PAGE_SIZE = 2000

AFE_COUNTRIES = {
    "Angola": "AGO",
    "Burundi": "BDI",
    "Botswana": "BWA",
    "Democratic Republic of the Congo": "COD",
    "Comoros": "COM",
    "Eritrea": "ERI",
    "Ethiopia": "ETH",
    "Kenya": "KEN",
    "Lesotho": "LSO",
    "Madagascar": "MDG",
    "Mozambique": "MOZ",
    "Mauritius": "MUS",
    "Malawi": "MWI",
    "Namibia": "NAM",
    "Rwanda": "RWA",
    "Sudan": "SDN",
    "Somalia": "SOM",
    "South Sudan": "SSD",
    "Sao Tome and Principe": "STP",
    "Eswatini": "SWZ",
    "Seychelles": "SYC",
    "Tanzania": "TZA",
    "Uganda": "UGA",
    "South Africa": "ZAF",
    "Zambia": "ZMB",
    "Zimbabwe": "ZWE",
}


def coerce_year(value: Any) -> int | None:
    """Return a sensible year from API values."""
    try:
        year = int(float(value))
        if 1900 <= year <= 2200:
            return year
    except Exception:
        pass
    return None


def parse_day_of_year(value: Any) -> int | None:
    """Return day-of-year from ArcGIS date values.

    Supports epoch milliseconds, epoch seconds, ISO date strings, and compact
    YYYYMMDD values. Returns None when the date cannot be parsed.
    """
    if value in (None, ""):
        return None

    try:
        # Numeric dates may be epoch ms, epoch seconds, or compact YYYYMMDD.
        if isinstance(value, (int, float)) or re.fullmatch(r"\d+(\.0+)?", str(value).strip()):
            number = int(float(value))
            text = str(number)
            if len(text) == 8 and 1900 <= int(text[:4]) <= 2200:
                dt = datetime.strptime(text, "%Y%m%d").replace(tzinfo=timezone.utc)
            else:
                timestamp = number / 1000 if number > 10_000_000_000 else number
                dt = datetime.fromtimestamp(timestamp, tz=timezone.utc)
        else:
            text = str(value).strip().replace("Z", "+00:00")
            dt = datetime.fromisoformat(text)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            else:
                dt = dt.astimezone(timezone.utc)
        day = int(dt.strftime("%j"))
        return day if 1 <= day <= 366 else None
    except Exception:
        return None


def get_json(params: dict[str, Any], *, retries: int = 3, sleep_seconds: float = 2.0) -> dict[str, Any]:
    """Fetch JSON from ArcGIS API with SSL context for corporate networks."""
    ssl_context = ssl._create_unverified_context()
    url = f"{BASE_URL}?{urlencode(params)}"
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            req = Request(url, headers={"User-Agent": "AFE-port-watch-cache/1.1"})
            with urlopen(req, timeout=60, context=ssl_context) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt < retries:
                time.sleep(sleep_seconds * attempt)
    raise RuntimeError(f"Failed after {retries} attempts: {last_error}")


def get_ports_for_country_year(iso3: str, year: int) -> list[str]:
    """Get unique port names for a country-year."""
    params = {
        "where": f"ISO3='{iso3}' AND year = {year}",
        "outFields": "portname",
        "returnGeometry": "false",
        "f": "json",
        "returnDistinctValues": "true",
        "orderByFields": "portname ASC",
    }
    payload = get_json(params)
    features = payload.get("features", []) or []
    ports = list(set(f.get("attributes", {}).get("portname") for f in features if f.get("attributes", {}).get("portname")))
    ports.sort()
    return ports


def fetch_port_year_features(iso3: str, portname: str, year: int) -> list[dict[str, Any]]:
    """Fetch all records for one port-year."""
    features: list[dict[str, Any]] = []
    offset = 0
    while True:
        params = {
            "where": f"ISO3='{iso3}' AND portname='{portname}' AND year = {year}",
            "outFields": "date,year,country,ISO3,portname,portcalls_tanker,import_tanker",
            "returnGeometry": "false",
            "f": "json",
            "resultRecordCount": PAGE_SIZE,
            "resultOffset": offset,
            "orderByFields": "date ASC",
            "maxRecordCountFactor": 5,
        }
        payload = get_json(params)
        if payload.get("error"):
            raise RuntimeError(f"ArcGIS error for {iso3} {portname} {year}: {payload['error']}")
        batch = payload.get("features", []) or []
        features.extend(batch)
        if len(batch) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
        time.sleep(0.5)  # Be nice to the API
    return features


def aggregate_features(features: list[dict[str, Any]], fallback_year: int | None = None) -> dict[str, list[dict[str, Any]]]:
    daily: dict[tuple[int, int], dict[str, float]] = defaultdict(lambda: {"calls": 0.0, "volume": 0.0})

    for feature in features:
        attr = feature.get("attributes", {}) or {}
        year = coerce_year(attr.get("year")) or fallback_year
        day = parse_day_of_year(attr.get("date"))
        if not year or not day or day < 1 or day > 366:
            continue
        daily[(year, day)]["calls"] += float(attr.get("portcalls_tanker") or 0)
        daily[(year, day)]["volume"] += float(attr.get("import_tanker") or 0)

    by_year: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for (year, day), values in sorted(daily.items()):
        by_year[str(year)].append(
            {
                "dayOfYear": day,
                "calls": round(values["calls"], 6),
                "volume": round(values["volume"], 6),
            }
        )

    for rows in by_year.values():
        cum_calls = 0.0
        cum_volume = 0.0
        for row in rows:
            cum_calls += row["calls"]
            cum_volume += row["volume"]
            row["cumCalls"] = round(cum_calls, 6)
            row["cumVol"] = round(cum_volume, 6)

    return dict(by_year)


def merge_year_data(target: dict[str, list[dict[str, Any]]], source: dict[str, list[dict[str, Any]]]) -> None:
    for year, rows in source.items():
        target[year] = rows


def main() -> int:
    countries: dict[str, Any] = {}
    errors: dict[str, str] = {}

    for country, iso3 in sorted(AFE_COUNTRIES.items()):
        print(f"Fetching {country} ({iso3}) ...")
        country_data: dict[str, list[dict[str, Any]]] = {}
        record_count_by_year: dict[str, int] = {}

        for year in YEARS:
            try:
                ports = get_ports_for_country_year(iso3, year)
                if not ports:
                    print(f"  {year}: no ports found")
                    continue
                
                print(f"  {year}: {len(ports)} port(s)")
                year_record_count = 0
                
                for port in ports:
                    features = fetch_port_year_features(iso3, port, year)
                    year_record_count += len(features)
                    merge_year_data(country_data, aggregate_features(features, fallback_year=year))
                
                record_count_by_year[str(year)] = year_record_count
                
            except Exception as exc:  # noqa: BLE001
                errors[f"{country} {year}"] = str(exc)
                print(f"WARNING: {country} {year} failed: {exc}", file=sys.stderr)

        if country_data:
            countries[country] = {
                "iso3": iso3,
                "record_count_by_year": record_count_by_year,
                "data": country_data,
            }
        else:
            errors[country] = "No records returned for any requested year"

    output = {
        "last_updated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "ArcGIS Daily_Ports_Data FeatureServer",
        "source_url": BASE_URL,
        "min_year": MIN_YEAR,
        "max_year": MAX_YEAR,
        "years": YEARS,
        "countries": countries,
        "errors": errors,
    }

    OUTFILE.parent.mkdir(parents=True, exist_ok=True)
    OUTFILE.write_text(json.dumps(output, indent=2, sort_keys=True), encoding="utf-8")
    print(f"Wrote {OUTFILE} with {len(countries)} countries and {len(errors)} warnings.")
    return 0 if countries else 1


if __name__ == "__main__":
    raise SystemExit(main())
