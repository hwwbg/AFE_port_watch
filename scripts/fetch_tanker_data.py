#!/usr/bin/env python3
"""Fetch and cache AFE tanker port-call data for the static dashboard."""

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


BASE_URL = "https://services9.arcgis.com/weJ1QsnbMYJlCHdG/arcgis/rest/services/Daily_Ports_Data/FeatureServer/0/query"
OUTFILE = Path("dashboard/data/tanker_data.json")

MIN_YEAR = 2024
MAX_YEAR = datetime.now(timezone.utc).year
YEARS = list(range(MIN_YEAR, MAX_YEAR + 1))
PAGE_SIZE = 2000


AFE_COUNTRIES = {
    "Angola": "AGO",
    "Botswana": "BWA",
    "Burundi": "BDI",
    "Comoros": "COM",
    "Congo, Dem. Rep.": "COD",
    "Eritrea": "ERI",
    "Eswatini": "SWZ",
    "Ethiopia": "ETH",
    "Kenya": "KEN",
    "Lesotho": "LSO",
    "Madagascar": "MDG",
    "Malawi": "MWI",
    "Mauritius": "MUS",
    "Mozambique": "MOZ",
    "Namibia": "NAM",
    "Rwanda": "RWA",
    "Seychelles": "SYC",
    "Somalia": "SOM",
    "South Africa": "ZAF",
    "South Sudan": "SSD",
    "Sudan": "SDN",
    "Tanzania": "TZA",
    "Uganda": "UGA",
    "Zambia": "ZMB",
    "Zimbabwe": "ZWE",
}


def parse_day_of_year(value: Any) -> int | None:
    if value in (None, ""):
        return None

    try:
        text = str(value).strip()

        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            dt = datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        elif isinstance(value, (int, float)) or re.fullmatch(r"\d+(\.0+)?", text):
            number = int(float(value))
            number_text = str(number)

            if len(number_text) == 8:
                dt = datetime.strptime(number_text, "%Y%m%d").replace(tzinfo=timezone.utc)
            else:
                timestamp = number / 1000 if number > 10_000_000_000 else number
                dt = datetime.fromtimestamp(timestamp, tz=timezone.utc)
        else:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            else:
                dt = dt.astimezone(timezone.utc)

        day = int(dt.strftime("%j"))
        return day if 1 <= day <= 366 else None

    except Exception:
        return None


def get_json(
    params: dict[str, Any],
    retries: int = 3,
    sleep_seconds: float = 2.0,
) -> dict[str, Any]:
    ssl_context = ssl._create_unverified_context()
    url = f"{BASE_URL}?{urlencode(params)}"
    last_error: Exception | None = None

    for attempt in range(1, retries + 1):
        try:
            req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urlopen(req, timeout=120, context=ssl_context) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(sleep_seconds * attempt)

    raise RuntimeError(f"Failed after {retries} attempts: {last_error}")


def fetch_country_year_features(iso3: str, year: int) -> list[dict[str, Any]]:
    features: list[dict[str, Any]] = []
    offset = 0

    while True:
        params = {
            "where": f"ISO3 = '{iso3}' AND year = {year}",
            "outFields": "date,year,country,ISO3,portname,portcalls_tanker,import_tanker",
            "returnGeometry": "false",
            "outSR": "4326",
            "f": "json",
            "resultRecordCount": PAGE_SIZE,
            "resultOffset": offset,
            "orderByFields": "date ASC",
        }

        payload = get_json(params)

        if payload.get("error"):
            raise RuntimeError(f"ArcGIS error for {iso3} {year}: {payload['error']}")

        batch = payload.get("features", []) or []
        features.extend(batch)

        print(f"    offset {offset}: {len(batch)} records")

        if not payload.get("exceededTransferLimit") or len(batch) == 0:
            break

        offset += PAGE_SIZE
        time.sleep(0.3)

    return features


def aggregate_features(
    features: list[dict[str, Any]],
    fallback_year: int,
) -> dict[str, list[dict[str, Any]]]:
    daily: dict[tuple[int, int], dict[str, float]] = defaultdict(
        lambda: {"calls": 0.0, "volume": 0.0}
    )

    for feature in features:
        attr = feature.get("attributes", {}) or {}

        year = int(attr.get("year") or fallback_year)
        day = parse_day_of_year(attr.get("date"))

        if not day:
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


def main() -> int:
    print("=" * 70)
    print("Fetching PortWatch tanker data for AFE countries")
    print("=" * 70)

    countries: dict[str, Any] = {}
    errors: dict[str, str] = {}
    total_records_raw = 0

    for country, iso3 in sorted(AFE_COUNTRIES.items()):
        print(f"\n{country} ({iso3})")

        country_data: dict[str, list[dict[str, Any]]] = {}
        record_count_by_year: dict[str, int] = {}

        for year in YEARS:
            print(f"  {year}:")

            try:
                features = fetch_country_year_features(iso3, year)

                record_count_by_year[str(year)] = len(features)
                total_records_raw += len(features)

                if not features:
                    print("    no data")
                    continue

                country_data.update(aggregate_features(features, fallback_year=year))

                print(f"    fetched {len(features)} raw records")

            except Exception as exc:
                errors[f"{country} {year}"] = str(exc)
                record_count_by_year[str(year)] = 0
                print(f"WARNING: {country} {year} failed: {exc}", file=sys.stderr)

        countries[country] = {
            "iso3": iso3,
            "record_count_by_year": record_count_by_year,
            "data": country_data,
        }

        if not country_data:
            errors[country] = "No records returned for any requested year"

    total_records_aggregated = sum(
        len(rows)
        for country_info in countries.values()
        for rows in country_info["data"].values()
    )

    output = {
        "last_updated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "ArcGIS Daily_Ports_Data FeatureServer",
        "source_url": BASE_URL,
        "min_year": MIN_YEAR,
        "max_year": MAX_YEAR,
        "years": YEARS,
        "countries": countries,
        "errors": errors,
        "total_countries": len(countries),
        "total_records_raw": total_records_raw,
        "total_records": total_records_aggregated,
    }

    OUTFILE.parent.mkdir(parents=True, exist_ok=True)
    OUTFILE.write_text(
        json.dumps(output, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    print("\n" + "=" * 70)
    print(f"Wrote {OUTFILE}")
    print(f"Countries: {len(countries)}")
    print(f"Raw records fetched: {total_records_raw}")
    print(f"Aggregated records written: {total_records_aggregated}")
    print(f"Warnings/errors: {len(errors)}")
    print("=" * 70)

    return 0 if countries else 1


if __name__ == "__main__":
    raise SystemExit(main())
