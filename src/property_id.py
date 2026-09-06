"""Compass's stable property id — the key that actually identifies a house.

A listing id (`_lid`) is disposable. Take a house off the market and relist
it and Compass issues a new one, which is how one property ends up in the
corpus twice: scored twice, ranked twice, and paid for twice at the vision
API. Address was the free proxy for "same house", and it is a good one, but
it fails in both directions — Compass re-enters an address as "James Cir"
where it was "James Circle", and a genuine duplex shares an address without
being one property.

The property id (`_pid`) survives a relist and is what Compass itself
considers canonical: every `_lid` URL 301-redirects to the `_pid` URL. So the
mapping costs one unauthenticated request per listing, and only once per
listing ever, because it is cached.

`http_head` is injected so this is testable without a network, and so the
caller owns the timeout and user-agent policy.
"""

import re
from typing import Callable

# `.../homedetails/<slug>/<id>_pid/` -- anchored on the _pid suffix rather
# than on position, because Compass has changed the slug format before and
# the id segment is the only part worth depending on.
_PID_RE = re.compile(r"/([A-Za-z0-9]+)_pid(?:/|$|[?#])")

# (status, headers) -- headers being anything with a case-insensitive get,
# or a plain dict, which is what the tests pass.
HttpHead = Callable[[str], tuple[int, object]]

REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


def parse_property_id(location) -> str | None:
    """The `_pid` segment of a Compass URL, or None if there is not one.

    Returning None for a non-property URL is the important half. Compass
    redirects a dead listing to a search page, and reading anything from that
    would collapse every dead listing into a single "property" — which, given
    the caller deletes all but one row per property, would delete the corpus.
    """
    if not isinstance(location, str) or not location:
        return None
    match = _PID_RE.search(location)
    return match.group(1) if match else None


def _location_of(headers) -> str | None:
    """The Location header, whatever case the client normalised it to."""
    if headers is None:
        return None
    getter = getattr(headers, "get", None)
    if getter is None:
        return None
    for name in ("Location", "location"):
        value = getter(name)
        if value:
            return value
    return None


def resolve_property_id(listing_url: str, http_head: HttpHead) -> str | None:
    """Follow one redirect from a listing URL to find its property id.

    None on anything unexpected — no redirect, no Location, a transport
    failure, a redirect somewhere that is not a property page. The caller
    falls back to matching on address, which is what it did before this
    existed, so an unresolved id costs nothing beyond the request.

    Deliberately does not raise: one unreachable listing must not abort a
    scrape that has already downloaded a hundred others.
    """
    if not listing_url:
        return None
    try:
        status, headers = http_head(listing_url)
    except Exception:  # noqa: BLE001 -- see docstring; a miss is not fatal
        return None
    if status not in REDIRECT_STATUSES:
        return None
    return parse_property_id(_location_of(headers))
