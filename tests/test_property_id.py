"""Compass's stable property id, and why it is the real dedupe key.

A listing id (`_lid`) is disposable: take a house off the market, put it back,
and Compass issues a new one. The property id (`_pid`) survives that. Every
`_lid` URL 301-redirects to the canonical `_pid` URL, which makes the mapping
one cheap unauthenticated request.

Everything here is pure -- the HTTP call is injected -- so no test touches the
network.
"""

import pytest

from src.property_id import parse_property_id, resolve_property_id

LID_URL = (
    "https://www.compass.com/homedetails/"
    "12651-James-Cir-Broomfield-CO-80020/2075764477594584425_lid/"
)
PID_URL = (
    "https://www.compass.com/homedetails/"
    "12651-James-Cir-Broomfield-CO-80020/131FZM_pid/"
)


def redirect_to(location, status=301):
    """A fake http_head returning one redirect, recording what it was given."""

    def http_head(url):
        http_head.calls.append(url)
        return status, {"Location": location}

    http_head.calls = []
    return http_head


# --- parsing ------------------------------------------------------------


def test_the_property_id_is_the_segment_before_pid():
    assert parse_property_id(PID_URL) == "131FZM"


def test_a_relative_location_still_parses():
    """A 301 is allowed to send a path rather than an absolute URL, and
    urllib will not complain either way."""
    assert parse_property_id("/homedetails/12651-James-Cir/131FZM_pid/") == "131FZM"


def test_a_trailing_slash_is_optional():
    assert parse_property_id("https://x/homedetails/a/131FZM_pid") == "131FZM"


def test_a_query_string_or_fragment_is_ignored():
    assert parse_property_id(f"{PID_URL}?utm_source=x#gallery") == "131FZM"


def test_a_url_that_is_not_a_property_page_is_not_a_property_id():
    """Compass redirects a dead listing to a search page. Reading "search" as
    a property id would collapse every dead listing into one property and
    delete all but one of them."""
    for url in (
        "https://www.compass.com/homes-for-sale/broomfield-co/",
        "https://www.compass.com/",
        "https://www.compass.com/homedetails/12651-James-Cir/999_lid/",
        "",
    ):
        assert parse_property_id(url) is None


def test_a_non_string_location_is_not_a_property_id():
    for value in (None, 404, {}):
        assert parse_property_id(value) is None


# --- resolving ----------------------------------------------------------


def test_a_redirect_yields_the_property_id():
    http_head = redirect_to(PID_URL)
    assert resolve_property_id(LID_URL, http_head) == "131FZM"
    assert http_head.calls == [LID_URL]


def test_a_302_counts_as_well_as_a_301():
    assert resolve_property_id(LID_URL, redirect_to(PID_URL, status=302)) == "131FZM"


def test_no_redirect_means_no_answer():
    """A 200 means the lid URL is already canonical, or Compass changed how
    it serves these. Either way we learn nothing, and guessing is worse than
    falling back to the address rule."""

    def http_head(url):
        return 200, {}

    assert resolve_property_id(LID_URL, http_head) is None


def test_a_missing_location_header_is_not_an_error():
    def http_head(url):
        return 301, {}

    assert resolve_property_id(LID_URL, http_head) is None


def test_the_location_header_is_found_whatever_its_casing():
    """HTTP header names are case-insensitive and clients differ on how they
    normalise them."""

    def http_head(url):
        return 301, {"location": PID_URL}

    assert resolve_property_id(LID_URL, http_head) == "131FZM"


def test_a_transport_failure_returns_none_rather_than_raising():
    """One unreachable listing must not abort a scrape. An unresolved id
    simply falls back to the address rule, which is what we had before."""

    def http_head(url):
        raise RuntimeError("connection reset")

    assert resolve_property_id(LID_URL, http_head) is None


def test_an_empty_url_is_not_requested_at_all():
    http_head = redirect_to(PID_URL)
    assert resolve_property_id("", http_head) is None
    assert http_head.calls == []
