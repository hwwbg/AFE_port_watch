#!/usr/bin/env python3
"""Fetch and cache AFE tanker port-call data for the static dashboard.

This script is designed for GitHub Actions. It reads the ArcGIS Daily_Ports_Data
FeatureServer once per scheduled run, aggregates records to cumulative day-of-year
series, and writes a small JSON file that the dashboard can load quickly.
"""

from __future__ import annotations

import json
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


def parse_day_of_year(value: Any) -> tuple[int | None, int | None]:
    """Return (year, day_of_year) from ArcGIS date values."""
    if value in (None, ""):
        return None, None

    try:
        if isinstance(value, (int, float)):
            # ArcGIS date fields commonly use epoch milliseconds.
            timestamp = value / 1000 if value > 10_000_000_000 else value
            dt = datetime.fromtimestamp(timestamp, tz=timezone.utc)
        else:
            text = str(value).strip().replace("Z", "+00:00")
            dt = datetime.fromisoformat(text)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            else:
                dt = dt.astimezone(timezone.utc)
        return dt.year, int(dt.strftime("%j"))
    except Exception:
        return None, None


def get_json(params: dict[str, Any], *, retries: int = 3, sleep_seconds: float = 2.0) -> dict[str, Any]:
    url = f"{BASE_URL}?{urlencode(params)}"
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            req = Request(url, headers={"User-Agent": "AFE-port-watch-cache/1.0"})
            with urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt < retries:
                time.sleep(sleep_seconds * attempt)
    raise RuntimeError(f"Failed after {retries} attempts: {last_error}")


def fetch_country_features(iso3: str) -> list[dict[str, Any]]:
    features: list[dict[str, Any]] = []
    offset = 0
    while True:
        params = {
            "where": f"ISO3='{iso3}' AND year >= {MIN_YEAR}",
            "outFields": "date,year,country,ISO3,portcalls_tanker,import_tanker",
            "returnGeometry": "false",
            "f": "json",
            "resultRecordCount": PAGE_SIZE,
            "resultOffset": offset,
            "orderByFields": "date ASC",
        }
        payload = get_json(params)
        if payload.get("error"):
            raise RuntimeError(f"ArcGIS error for {iso3}: {payload['error']}")
        batch = payload.get("features", []) or []
        features.extend(batch)
        if len(batch) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    return features


def aggregate_features(features: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    daily: dict[tuple[int, int], dict[str, float]] = defaultdict(lambda: {"calls": 0.0, "volume": 0.0})

    for feature in features:
        attr = feature.get("attributes", {}) or {}
        year, day = parse_day_of_year(attr.get("date"))
        if year is None:
            try:
                year = int(attr.get("year"))
            except Exception:
                year = None
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

    for year, rows in by_year.items():
        cum_calls = 0.0
        cum_volume = 0.0
        for row in rows:
            cum_calls += row["calls"]
            cum_volume += row["volume"]
            row["cumCalls"] = round(cum_calls, 6)
            row["cumVol"] = round(cum_volume, 6)

    return dict(by_year)


def main() -> int:
    countries: dict[str, Any] = {}
    errors: dict[str, str] = {}

    for country, iso3 in sorted(AFE_COUNTRIES.items()):
        print(f"Fetching {country} ({iso3}) ...")
        try:
            features = fetch_country_features(iso3)
            aggregated = aggregate_features(features)
            if aggregated:
                countries[country] = {"iso3": iso3, "data": aggregated}
            else:
                errors[country] = "No records returned"
        except Exception as exc:  # noqa: BLE001
            errors[country] = str(exc)
            print(f"WARNING: {country} failed: {exc}", file=sys.stderr)

    output = {
        "last_updated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "ArcGIS Daily_Ports_Data FeatureServer",
        "source_url": BASE_URL,
        "min_year": MIN_YEAR,
        "countries": countries,
        "errors": errors,
    }

    OUTFILE.parent.mkdir(parents=True, exist_ok=True)
    OUTFILE.write_text(json.dumps(output, indent=2, sort_keys=True), encoding="utf-8")
    print(f"Wrote {OUTFILE} with {len(countries)} countries and {len(errors)} warnings.")
    return 0 if countries else 1


if __name__ == "__main__":
    raise SystemExit(main())
