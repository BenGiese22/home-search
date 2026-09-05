"""The only module in this codebase that knows which routing provider we use.

Everything above it works in (lat, lon), miles and minutes. Swapping Mapbox
for another vendor should mean rewriting this file and nothing else -- which
matters more than usual here, because we run against Mapbox's terms
knowingly (see docs/routing-provider-terms.md) and a revoked key is the
failure this design is sized against.

`http_get` is injected for the same reason it always was: these functions
have to be testable without a network, and the pipeline's retry and pacing
policy belongs to the stage, not to the URL builder.
"""

from dataclasses import dataclass
from typing import Callable
from urllib.parse import quote

Coordinates = tuple[float, float]
# (lat, lon, accuracy) -- accuracy is carried out of this module so the caller
# can log how a match was made, not so it can second-guess the decision.
Geocode = tuple[float, float, str]

METERS_PER_MILE = 1609.34
SECONDS_PER_MINUTE = 60.0

GEOCODE_URL = "https://api.mapbox.com/search/geocode/v6/forward"
# `driving`, not `driving-traffic`: arrive_by is only supported on this
# profile. driving-traffic answers for right now, which for a cron running
# every six hours would make the stored duration depend on what time the run
# happened to fire.
DIRECTIONS_URL = "https://api.mapbox.com/directions/v5/mapbox/driving"

# Accuracy values that mean "this is the building". Anything else -- an
# interpolated point on a street, a postcode or place centroid -- is treated
# as a miss. A centroid match routes successfully and returns a plausible
# number, so accepting one would put a wrong commute in the corpus with
# nothing downstream able to tell. Measured 2026-09-05: all 101 listings
# resolve at rooftop or parcel.
ADDRESS_LEVEL = frozenset({"rooftop", "parcel", "point"})


@dataclass(frozen=True)
class AddressParts:
    """A US street address split the way Mapbox's structured input wants it."""

    address: str
    city: str
    state: str
    zip_code: str


class RoutingError(RuntimeError):
    """A transport failure, with the access token scrubbed out of the message.

    Not decoration. The token travels as a query parameter -- Mapbox does not
    accept it in a header on these endpoints -- and `requests` puts the full
    URL into an HTTPError's message. Letting that propagate writes the
    credential into the pipeline log, which is uploaded to Blob and kept.
    """


def _redact(text: str, token: str) -> str:
    """Remove the token in both the forms it can appear in: as written, and
    percent-encoded the way it went into the URL."""
    for form in (token, quote(token, safe="")):
        if form:
            text = text.replace(form, "<redacted>")
    return text


def _fetch(url: str, token: str, http_get: Callable[[str], dict]) -> dict:
    try:
        return http_get(url)
    except Exception as exc:  # noqa: BLE001 -- re-raised, but scrubbed first
        raise RoutingError(_redact(str(exc), token)) from exc


def geocode_address(
    parts: AddressParts, token: str, http_get: Callable[[str], dict]
) -> Geocode | None:
    """Resolve one address to (lat, lon, accuracy), or None if it is not a
    building-level match.

    Structured parameters rather than a single query string, so Mapbox is
    never left to guess which token is the city -- the corpus has addresses
    like "3963 West 65th Place" whose street number reads as a Denver grid
    coordinate, and free-text matching put one of them 3.7 miles away.
    """
    url = (
        f"{GEOCODE_URL}?address_line1={quote(parts.address)}"
        f"&place={quote(parts.city)}"
        f"&region={quote(parts.state)}"
        f"&postcode={quote(parts.zip_code)}"
        f"&country=US&limit=1&access_token={quote(token)}"
    )
    payload = _fetch(url, token, http_get)
    features = (payload or {}).get("features") or []
    if not features:
        return None
    # accuracy is nested inside `coordinates`, not beside it. Reading
    # properties.accuracy returns None for every result, which reads as a
    # total failure rather than as a bug in the reader.
    coordinates = ((features[0].get("properties") or {}).get("coordinates")) or {}
    accuracy = coordinates.get("accuracy")
    if accuracy not in ADDRESS_LEVEL:
        return None
    try:
        return (
            float(coordinates["latitude"]),
            float(coordinates["longitude"]),
            accuracy,
        )
    except (KeyError, TypeError, ValueError):
        return None


def route(
    origin: Coordinates,
    destination: Coordinates,
    arrive_by: str,
    token: str,
    http_get: Callable[[str], dict],
) -> tuple[float, float] | None:
    """Miles and minutes for a drive arriving at `arrive_by`, or None if
    Mapbox could not route it.

    `arrive_by` is a local wall-clock stamp (`YYYY-MM-DDThh:mm`) with no
    zone: Mapbox reads it in the timezone of the origin, which is what we
    want -- the question is "what does this drive cost at 8:15 in the
    morning where the house is".

    Mapbox takes lon,lat. Everything else in this codebase is lat,lon, and
    swapping them routes between two points in the ocean while still
    returning HTTP 200 -- so the flip happens here, once, next to a test
    that pins it.
    """
    url = (
        f"{DIRECTIONS_URL}/"
        f"{origin[1]},{origin[0]};{destination[1]},{destination[0]}"
        f"?overview=false&arrive_by={quote(arrive_by)}"
        f"&access_token={quote(token)}"
    )
    payload = _fetch(url, token, http_get)
    payload = payload or {}
    if payload.get("code") != "Ok":
        return None
    routes = payload.get("routes") or []
    if not routes:
        return None
    try:
        meters = float(routes[0]["distance"])
        seconds = float(routes[0]["duration"])
    except (KeyError, TypeError, ValueError):
        return None
    return (meters / METERS_PER_MILE, seconds / SECONDS_PER_MINUTE)
