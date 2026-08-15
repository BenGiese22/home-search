from typing import Callable
from urllib.parse import quote

Coordinates = tuple[float, float]

METERS_PER_MILE = 1609.34
SECONDS_PER_MINUTE = 60.0


def geocode(address: str, http_get: Callable[[str], list[dict]]) -> Coordinates | None:
    """Resolve an address to (lat, lon) via a Nominatim-shaped search response.
    http_get is injected so this stays testable without a live network call."""
    url = f"https://nominatim.openstreetmap.org/search?q={quote(address)}&format=json&limit=1"
    results = http_get(url)
    if not results:
        return None
    try:
        return (float(results[0]["lat"]), float(results[0]["lon"]))
    except (KeyError, ValueError, TypeError):
        return None


def route_miles_minutes(
    origin: Coordinates,
    destination: Coordinates,
    http_get: Callable[[str], dict],
) -> tuple[float, float] | None:
    """Road-network distance/duration via an OSRM-shaped route response.
    OSRM addresses are lon,lat (not lat,lon) — origin/destination here stay
    lat,lon like everywhere else in this module; the URL flips them."""
    url = (
        "https://router.project-osrm.org/route/v1/driving/"
        f"{origin[1]},{origin[0]};{destination[1]},{destination[0]}?overview=false"
    )
    data = http_get(url)
    routes = data.get("routes") or []
    if not routes:
        return None
    try:
        meters = float(routes[0]["distance"])
        seconds = float(routes[0]["duration"])
        return (meters / METERS_PER_MILE, seconds / SECONDS_PER_MINUTE)
    except (KeyError, ValueError, TypeError):
        return None
