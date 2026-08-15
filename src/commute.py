from dataclasses import dataclass
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


@dataclass
class CommuteResult:
    lat: float | None
    lon: float | None
    denver_miles: float | None
    denver_minutes: float | None
    medtronic_miles: float | None
    medtronic_minutes: float | None
    geocode_failed: bool


def resolve_destination(
    primary_address: str,
    geocode_fn: Callable[[str], Coordinates | None],
    fallback_address: str | None = None,
) -> tuple[Coordinates, bool]:
    """Geocode a fixed destination once at startup. Unlike per-listing
    addresses, a destination that can't be resolved at all is a setup
    error, not a per-listing skip — it aborts the run."""
    coords = geocode_fn(primary_address)
    if coords is not None:
        return coords, False
    if fallback_address is not None:
        coords = geocode_fn(fallback_address)
        if coords is not None:
            return coords, True
    raise RuntimeError(f"could not geocode destination: {primary_address}")


def compute_commute(
    address: str,
    denver_coords: Coordinates,
    medtronic_coords: Coordinates,
    geocode_fn: Callable[[str], Coordinates | None],
    route_fn: Callable[[Coordinates, Coordinates], tuple[float, float] | None],
) -> CommuteResult:
    origin = geocode_fn(address)
    if origin is None:
        return CommuteResult(None, None, None, None, None, None, geocode_failed=True)

    denver = route_fn(origin, denver_coords)
    medtronic = route_fn(origin, medtronic_coords)
    return CommuteResult(
        lat=origin[0],
        lon=origin[1],
        denver_miles=denver[0] if denver else None,
        denver_minutes=denver[1] if denver else None,
        medtronic_miles=medtronic[0] if medtronic else None,
        medtronic_minutes=medtronic[1] if medtronic else None,
        geocode_failed=denver is None or medtronic is None,
    )
