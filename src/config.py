import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from dotenv import dotenv_values

DEFAULT_ENV_PATH = ".env"


def load_env(dotenv_path: str | Path = DEFAULT_ENV_PATH) -> dict[str, str]:
    """Configuration from `.env`, overridden by the process environment.

    Reading only `.env` means a script cannot run anywhere that supplies
    configuration as process environment variables -- which is every
    containerised or hosted environment, and a hard prerequisite for running
    any stage outside this laptop. Merging is harmless locally: with no
    process env set, every value is exactly what `.env` says.

    A missing `.env` is not an error; dotenv_values returns an empty mapping
    and the process environment supplies everything.

    Keys declared without a value (a bare `KEY` line) parse to None and are
    dropped rather than merged -- injecting None would shadow a real
    process-env value and defeat callers' defaults, both worse than treating
    the line as absent.
    """
    from_file = {
        key: value
        for key, value in dotenv_values(dotenv_path).items()
        if value is not None
    }
    return {**from_file, **os.environ}


# Compass exposes one collection as several UI tabs that all share the same
# collection ID. The tab is chosen by the `listingsFilter` integer in the
# internal API call, NOT by the URL path -- so the path alone can never be
# trusted to say which listings come back. Verified live by intercepting the
# SPA's own requests: /matches issues listingsFilter 0, /favorites issues 1.
COLLECTION_TABS: dict[str, int] = {"matches": 0, "favorites": 1}

# Favorites outranks matches when the same listing is in both. Moving a
# listing into favorites is a deliberate act by Ben or Megan, and only they
# move it back out; matches is just "the saved search matched it". So the
# favorites copy is the one that survives dedup, whatever order the tabs
# were requested in. Lower number wins.
TAB_PRECEDENCE: dict[str, int] = {"favorites": 0, "matches": 1}

DEFAULT_COLLECTION_TABS: tuple[str, ...] = ("favorites", "matches")

# filter 3 / reviewStage 1 -- the deliberately discarded pile. Refused by
# name rather than ignored: a URL naming it means the config is wrong, and
# silently fetching something else is the exact trap this module removes.
NEVER_SCRAPED_TABS = frozenset({"notInterested"})

_COLLECTION_TAB_RE = re.compile(r"/collection/[^/?#]+/([A-Za-z]+)")


def collection_tab_from_url(collection_url: str) -> str | None:
    """The tab segment of a Compass collection URL, or None when it has none.

    Returns the raw segment even when it is not a tab this project will
    scrape -- validating it is load_config's job, and it needs the name to
    put in the error message.
    """
    match = _COLLECTION_TAB_RE.search(collection_url)
    return match.group(1) if match else None


def _ordered_by_precedence(tabs) -> tuple[str, ...]:
    return tuple(sorted(tabs, key=lambda tab: TAB_PRECEDENCE[tab]))


def _parse_collection_tabs(raw: str | None) -> tuple[str, ...] | None:
    """Parse COMPASS_COLLECTION_TABS, or None when it is unset."""
    if raw is None or not raw.strip():
        return None
    tabs = [t.strip() for t in raw.split(",") if t.strip()]
    for tab in tabs:
        if tab in NEVER_SCRAPED_TABS:
            raise ValueError(
                f"COMPASS_COLLECTION_TABS must not include {tab!r} -- that bucket "
                "is deliberately never scraped"
            )
        if tab not in COLLECTION_TABS:
            raise ValueError(
                f"unknown collection tab {tab!r} in COMPASS_COLLECTION_TABS; "
                f"expected one of {sorted(COLLECTION_TABS)}"
            )
    if not tabs:
        return None
    return _ordered_by_precedence(dict.fromkeys(tabs))


@dataclass
class Config:
    compass_email: str
    compass_password: str
    collection_url: str | None
    listing_urls: list[str]
    collection_tabs: tuple[str, ...] = DEFAULT_COLLECTION_TABS


def load_config(env: Mapping[str, str]) -> Config:
    email = (env.get("COMPASS_EMAIL") or "").strip()
    password = (env.get("COMPASS_PASSWORD") or "").strip()
    if not email or not password:
        raise ValueError("COMPASS_EMAIL and COMPASS_PASSWORD must be set in .env")

    collection_url = (env.get("COMPASS_COLLECTION_URL") or "").strip() or None
    raw_listing_urls = (env.get("LISTING_URLS") or "").strip()
    listing_urls = (
        [u.strip() for u in raw_listing_urls.split(",") if u.strip()]
        if raw_listing_urls
        else []
    )

    if not collection_url and not listing_urls:
        raise ValueError(
            "Set at least one of COMPASS_COLLECTION_URL or LISTING_URLS in .env"
        )

    explicit_tabs = _parse_collection_tabs(env.get("COMPASS_COLLECTION_TABS"))
    collection_tabs = explicit_tabs or DEFAULT_COLLECTION_TABS

    # The URL's tab segment does not select what gets fetched -- COLLECTION_TABS
    # does -- so it is validated rather than obeyed. Refusing a bad segment
    # instead of ignoring it is the point: before this, pointing the URL at
    # /favorites silently kept fetching matches, which looked like a working
    # config and quietly cost us every favorite.
    if collection_url:
        url_tab = collection_tab_from_url(collection_url)
        if url_tab in NEVER_SCRAPED_TABS:
            raise ValueError(
                f"COMPASS_COLLECTION_URL points at the {url_tab!r} tab, which is "
                "deliberately never scraped; point it at the collection's "
                "/matches or /favorites URL instead"
            )
        if url_tab is not None and url_tab not in COLLECTION_TABS:
            raise ValueError(
                f"unknown collection tab {url_tab!r} in COMPASS_COLLECTION_URL; "
                f"expected one of {sorted(COLLECTION_TABS)}"
            )
        # An explicit tab list that excludes the tab the URL names is a
        # contradiction. Guessing which one the user meant is how the
        # original bug felt from the outside; fail loudly instead.
        if url_tab is not None and explicit_tabs and url_tab not in explicit_tabs:
            raise ValueError(
                f"COMPASS_COLLECTION_URL names the {url_tab!r} tab but "
                f"COMPASS_COLLECTION_TABS excludes it ({', '.join(explicit_tabs)})"
            )

    return Config(
        compass_email=email,
        compass_password=password,
        collection_url=collection_url,
        listing_urls=listing_urls,
        collection_tabs=collection_tabs,
    )
