import sys
import time
from pathlib import Path

import requests

from src.commute import compute_commute, geocode, resolve_destination, route_miles_minutes
from src.turso_db import stage_connection
from src.db import get_listing_ids_missing_commute, query_listings, upsert_commute

DATA_DIR = Path("data")
USER_AGENT = "home-search/1.0 (bengiese22@gmail.com)"
NOMINATIM_RATE_LIMIT_SECONDS = 1.0

DENVER_UNION_STATION = "Denver Union Station, Denver, CO"
MEDTRONIC_LAFAYETTE = "Medtronic, Lafayette, CO"
MEDTRONIC_FALLBACK = "Lafayette, CO"


def nominatim_get(url: str) -> list[dict]:
    response = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
    response.raise_for_status()
    return response.json()


def osrm_get(url: str) -> dict:
    response = requests.get(url, timeout=30)
    response.raise_for_status()
    return response.json()


def rate_limited_geocode(address: str):
    time.sleep(NOMINATIM_RATE_LIMIT_SECONDS)
    return geocode(address, nominatim_get)


def route_fn(origin, destination):
    return route_miles_minutes(origin, destination, osrm_get)


def main() -> None:
    conn = stage_connection()
    listings_by_id = {row["listing_id"]: row for row in query_listings(conn)}
    # --only-new skips listings whose previous attempt failed; the default
    # retries them, since a failure is usually transient (rate limit, blip)
    # and leaving it unretried permanently neutralizes the commute factor.
    retry_failed = "--only-new" not in sys.argv
    missing_ids = get_listing_ids_missing_commute(conn, retry_failed=retry_failed)

    if not missing_ids:
        print("commute table already covers every listing")
        conn.close()
        return

    denver_coords, _ = resolve_destination(DENVER_UNION_STATION, rate_limited_geocode)
    medtronic_coords, used_fallback = resolve_destination(
        MEDTRONIC_LAFAYETTE, rate_limited_geocode, fallback_address=MEDTRONIC_FALLBACK
    )
    if used_fallback:
        print(f"Medtronic address didn't geocode; using {MEDTRONIC_FALLBACK} instead")

    for listing_id in missing_ids:
        row = listings_by_id[listing_id]
        address = f"{row['address']}, {row['city']}, {row['state']}"
        try:
            result = compute_commute(
                address, denver_coords, medtronic_coords, rate_limited_geocode, route_fn
            )
        except Exception as exc:
            print(f"skip commute (failed for {address}): {exc}")
            continue
        upsert_commute(conn, listing_id, result)
        status = "geocode/route failed" if result.geocode_failed else "ok"
        print(f"{listing_id} ({address}): {status}")

    conn.close()


if __name__ == "__main__":
    main()
