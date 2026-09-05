"""Pre-flight for the commute rebuild: can Mapbox carry the whole corpus?

Two questions have to be answered before any provider code is written, because
each one decides what gets deleted.

1. **Does Mapbox's geocoder resolve every listing at address precision?**
   Today the pipeline geocodes with Nominatim and falls back to the US Census
   geocoder, which rescued 11 listings Nominatim did not know. The plan is to
   delete both. That is only safe if Mapbox resolves 100% of the corpus at
   `rooftop|parcel|point` -- a city-centroid match would silently become a
   commute, which is worse than no commute at all.

2. **How far ahead does `arrive_by` accept an arrival time?** The stage will
   ask for the next mid-week 08:15. If Mapbox rejects a horizon of a week the
   arrival rule has to change before it is written, not after.

Read-only against Turso, and never imported by the pipeline. Run it as:

    MAPBOX_ACCESS_TOKEN=sk.xxx ./venv/bin/python ops/spikes/mapbox_preflight.py

Quota: roughly one geocode per listing plus six routes -- ~110 requests.
"""

import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.config import load_env  # noqa: E402
from src.turso_db import connect  # noqa: E402

# The two destinations the rebuild pins. Geocoding them here is how their
# coordinates get into the source: the numbers this prints are copied into
# compute_commutes.py as constants, so the run never spends a request on a
# fixed address again.
DESTINATIONS = {
    "MEDTRONIC_LAFAYETTE": ("250 Medtronic Dr", "Lafayette", "CO", "80026"),
    "DENVER_COWORKING": ("3201 Walnut St", "Denver", "CO", "80205"),
}

# Mapbox counts a geocode against a per-minute limit far above anything this
# asks for, but pacing keeps a burst from looking like abuse.
PACE_SECONDS = 0.2

# Accuracy values that mean "this is the building", as opposed to a street
# interpolation or a city centroid.
ADDRESS_LEVEL = {"rooftop", "parcel", "point"}

GEOCODE_URL = "https://api.mapbox.com/search/geocode/v6/forward"
DIRECTIONS_URL = "https://api.mapbox.com/directions/v5/mapbox/driving"

EARTH_RADIUS_M = 6371008.8


def haversine_m(a, b) -> float:
    """Metres between two (lat, lon) pairs. Only used to report how far a
    Mapbox result sits from the coordinate already stored, which is the one
    number that says whether swapping geocoders moves any listing."""
    from math import asin, cos, radians, sin, sqrt

    lat1, lon1, lat2, lon2 = map(radians, (a[0], a[1], b[0], b[1]))
    h = sin((lat2 - lat1) / 2) ** 2 + cos(lat1) * cos(lat2) * sin((lon2 - lon1) / 2) ** 2
    return 2 * EARTH_RADIUS_M * asin(sqrt(h))


def geocode(parts, token):
    """Structured forward geocode. Structured rather than a single string so
    Mapbox is not left to guess which token is the city."""
    address, city, state, postcode = parts
    url = (
        f"{GEOCODE_URL}?address_line1={quote(address)}&place={quote(city)}"
        f"&region={quote(state)}&postcode={quote(postcode)}"
        f"&country=US&limit=1&access_token={quote(token)}"
    )
    time.sleep(PACE_SECONDS)
    r = requests.get(url, timeout=30)
    if not r.ok:
        return None, f"HTTP {r.status_code}", None
    features = r.json().get("features") or []
    if not features:
        return None, "no features", None
    props = features[0].get("properties", {})
    coords = props.get("coordinates") or {}
    lat, lon = coords.get("latitude"), coords.get("longitude")
    if lat is None or lon is None:
        return None, "no coordinates", None
    # accuracy sits inside `coordinates`, not beside it -- reading
    # properties.accuracy returns None for every result and would have made
    # the whole corpus look like it failed the precision check.
    return (
        (float(lat), float(lon)),
        coords.get("accuracy"),
        (props.get("match_code") or {}).get("confidence"),
    )


def route(origin, dest, arrive_by, token):
    url = (
        f"{DIRECTIONS_URL}/{origin[1]},{origin[0]};{dest[1]},{dest[0]}"
        f"?overview=false&access_token={quote(token)}"
    )
    if arrive_by is not None:
        url += f"&arrive_by={quote(arrive_by)}"
    time.sleep(PACE_SECONDS)
    r = requests.get(url, timeout=30)
    headers = {k: v for k, v in r.headers.items() if k.lower().startswith("x-rate")}
    if not r.ok:
        return None, r.status_code, headers, r.text[:160]
    routes = r.json().get("routes") or []
    if not routes:
        return None, r.status_code, headers, "no routes"
    return routes[0]["duration"] / 60.0, r.status_code, headers, ""


def nth_wednesday_at(base: datetime, n: int) -> datetime:
    """The n-th Wednesday strictly after `base`, at 08:15.

    Counting Wednesdays rather than adding days: "base + 1 day" and "base + 3
    days" both roll forward to the same Wednesday on most weekdays, which
    silently collapses a four-point horizon test into two.
    """
    d = base + timedelta(days=1)
    while d.weekday() != 2:
        d += timedelta(days=1)
    d += timedelta(weeks=n - 1)
    return d.replace(hour=8, minute=15, second=0, microsecond=0)


def main() -> int:
    token = os.environ.get("MAPBOX_ACCESS_TOKEN") or load_env().get(
        "MAPBOX_ACCESS_TOKEN"
    )
    if not token:
        print("set MAPBOX_ACCESS_TOKEN", file=sys.stderr)
        return 2

    print("=" * 78)
    print("DESTINATIONS -- copy these coordinates into compute_commutes.py")
    dest_coords = {}
    for name, parts in DESTINATIONS.items():
        coords, accuracy, confidence = geocode(parts, token)
        print(f"  {name:22} {coords}  accuracy={accuracy} confidence={confidence}")
        dest_coords[name] = coords
    if any(c is None for c in dest_coords.values()):
        print("\nA destination did not resolve. Stop here.", file=sys.stderr)
        return 1

    conn = connect()
    rows = conn.execute(
        """
        SELECT l.listing_id, l.address, l.city, l.state, l.zip_code,
               c.lat AS stored_lat, c.lon AS stored_lon
        FROM listings l
        LEFT JOIN commute c ON c.listing_id = l.listing_id
        ORDER BY l.listing_id
        """
    ).fetchall()

    print("\n" + "=" * 78)
    print(f"CORPUS -- {len(rows)} listings")
    print(f"{'listing_id':14} {'accuracy':14} {'conf':10} {'moved_m':>9}  address")
    misses, below_address, moved = [], [], []
    for row in rows:
        parts = (row["address"], row["city"], row["state"], row["zip_code"])
        coords, accuracy, confidence = geocode(parts, token)
        if coords is None:
            misses.append((row["listing_id"], row["address"], accuracy))
            print(f"{row['listing_id'][:12]:14} {'MISS':14} {'-':10} {'-':>9}  "
                  f"{row['address']}  ({accuracy})")
            continue
        if accuracy not in ADDRESS_LEVEL:
            below_address.append((row["listing_id"], row["address"], accuracy))
        d = ""
        if row["stored_lat"] is not None and row["stored_lon"] is not None:
            metres = haversine_m(coords, (row["stored_lat"], row["stored_lon"]))
            moved.append((metres, row["listing_id"], row["address"]))
            d = f"{metres:.0f}"
        print(f"{row['listing_id'][:12]:14} {str(accuracy):14} {str(confidence):10} "
              f"{d:>9}  {row['address']}")

    print("\n" + "=" * 78)
    print("ARRIVE_BY HORIZON -- how far ahead does Mapbox accept an arrival?")
    origin = (rows[0]["stored_lat"], rows[0]["stored_lon"]) if rows else None
    if origin is None or origin[0] is None:
        origin = (39.99819, -105.10544)
    base = datetime.now()
    baseline, status, headers, err = route(
        origin, dest_coords["MEDTRONIC_LAFAYETTE"], None, token
    )
    print(f"  no arrive_by      -> {baseline}  HTTP {status} {err}")
    for n in (1, 2, 3, 5):
        when = nth_wednesday_at(base, n)
        stamp = when.strftime("%Y-%m-%dT%H:%M")
        minutes, status, headers, err = route(
            origin, dest_coords["MEDTRONIC_LAFAYETTE"], stamp, token
        )
        got = f"{minutes:.1f} min" if minutes is not None else f"REJECTED {err}"
        ahead = (when.date() - base.date()).days
        print(f"  {stamp} (+{ahead:>2}d, {when:%a}) -> {got}  HTTP {status}")
    print(f"  rate-limit headers: {headers or '(none returned)'}")

    print("\n" + "=" * 78)
    print("VERDICT")
    print(f"  listings                 : {len(rows)}")
    print(f"  geocoder misses          : {len(misses)}")
    print(f"  below address precision  : {len(below_address)}")
    for lid, addr, acc in below_address:
        print(f"      {lid}  {acc}  {addr}")
    if moved:
        moved.sort(reverse=True)
        print(f"  max move from stored     : {moved[0][0]:.0f} m  ({moved[0][2]})")
        mid = moved[len(moved) // 2][0]
        print(f"  median move from stored  : {mid:.0f} m")
    if not misses and not below_address:
        print("\n  -> Mapbox resolves the whole corpus at address precision.")
        print("     T4 may delete the Nominatim + Census chain.")
    else:
        print("\n  -> Mapbox does NOT carry the whole corpus.")
        print("     T4 must keep geocode_census as the fallback behind Mapbox.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
