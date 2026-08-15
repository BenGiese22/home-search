# Baseline Scoring Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Score every listing in `data/listings.db` 0–100 using structural stats and real commute times, with visible sub-scores, before any photo/vision AI work begins.

**Architecture:** Two new pure-logic modules (`src/commute.py`, `src/scoring.py`) mirror the existing store/derive split — `listings` stays the source of truth, `commute` and `scores` are derived SQLite tables that can be cheaply rebuilt. Two new top-level orchestration scripts (`compute_commutes.py`, `score.py`) do the network/DB wiring, following the untested-orchestration convention already established by `scrape.py` and `check.py`.

**Tech Stack:** Python 3.12, stdlib `sqlite3`, `requests` (already a dependency), Nominatim (geocoding) and OSRM (routing) — both free, keyless, public HTTP APIs. No new packages.

**Spec:** `docs/superpowers/specs/2026-08-13-baseline-scoring-design.md`

## Global Constraints

- No new `.env` variables and no new entries in `requirements.txt` — this feature only needs `requests`, already present.
- Nominatim usage policy: max 1 request/second, real `User-Agent` header. This applies only to the *real* HTTP call in the orchestration script — the parsing functions in `src/commute.py` take an injected `http_get` callable and are tested with fakes, never live network.
- Weights and keyword lists live as named module-level constants (`WEIGHT_COMMUTE`, `RENOVATION_KEYWORDS`, etc.), never embedded as magic numbers — the spec expects these to be retuned later.
- A missing input stat (e.g. `year_built == 0`, no commute data) scores as neutral (`50.0`), never `0.0` — one missing field shouldn't tank a composite score.
- The `baths >= 2` / `lot_sqft >= 6000` hard cutoffs are recorded as a `passes_filters` flag on every scored listing. No row is ever deleted or excluded from scoring.
- Test house style, matching `tests/test_db.py` and `tests/test_diff.py`: module-level fixtures, `tmp_path` for anything touching SQLite, no test classes.
- Geocoding and routing are never tested against the live network. Only the parsing/orchestration logic around an injected callable is tested (same pattern as `src/photos.py`'s `download_photos(..., fetch_bytes)`).

---

## File Structure

- **Create `src/commute.py`** — geocode/route parsing (`geocode`, `route_miles_minutes`) and orchestration logic (`resolve_destination`, `compute_commute`, `CommuteResult`). Pure functions; all network I/O is an injected callable.
- **Create `src/scoring.py`** — every `score_*` sub-score function, `passes_filters`, `compute_collection_stats`, `score_listing`, `ScoreResult`, `CollectionStats`. Pure functions, no network, no SQLite.
- **Modify `src/db.py`** — add `commute` and `scores` tables to `_SCHEMA`; add `upsert_commute`, `get_commute`, `get_listing_ids_missing_commute`, `get_amenities`, `upsert_score`, `get_scores`.
- **Create `compute_commutes.py`** (top-level, mirrors `check.py`) — the real Nominatim/OSRM HTTP callers, rate limiting, and the loop that fills in `commute` rows for listings missing them. Untested, same convention as `scrape.py`/`check.py`.
- **Create `score.py`** (top-level, mirrors `check.py`) — reads `listings` + `commute`, computes collection stats, scores every listing, upserts `scores`, prints a ranked report. Untested orchestration.

---

### Task 1: `src/commute.py` — geocode and route parsing

**Files:**
- Create: `src/commute.py`
- Test: `tests/test_commute.py`

**Interfaces:**
- Produces: `geocode(address: str, http_get: Callable[[str], list[dict]]) -> tuple[float, float] | None`
- Produces: `route_miles_minutes(origin: tuple[float, float], destination: tuple[float, float], http_get: Callable[[str], dict]) -> tuple[float, float] | None`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_commute.py
from src.commute import geocode, route_miles_minutes


def test_geocode_returns_lat_lon_from_first_result():
    def fake_http_get(url: str) -> list[dict]:
        return [{"lat": "39.7527", "lon": "-105.0016"}]

    result = geocode("Denver Union Station, Denver, CO", fake_http_get)

    assert result == (39.7527, -105.0016)


def test_geocode_returns_none_when_no_results():
    result = geocode("nowhere at all", lambda url: [])

    assert result is None


def test_geocode_returns_none_on_malformed_result():
    result = geocode("bad data", lambda url: [{"unexpected": "shape"}])

    assert result is None


def test_route_miles_minutes_converts_meters_and_seconds():
    def fake_http_get(url: str) -> dict:
        return {"routes": [{"distance": 16093.4, "duration": 1200.0}]}

    result = route_miles_minutes((39.8, -105.1), (39.99, -105.09), fake_http_get)

    assert result == (10.0, 20.0)


def test_route_miles_minutes_returns_none_when_no_routes():
    result = route_miles_minutes((39.8, -105.1), (39.99, -105.09), lambda url: {"routes": []})

    assert result is None


def test_route_miles_minutes_returns_none_on_malformed_response():
    result = route_miles_minutes((39.8, -105.1), (39.99, -105.09), lambda url: {"routes": [{}]})

    assert result is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_commute.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'src.commute'`

- [ ] **Step 3: Write the minimal implementation**

```python
# src/commute.py
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
        meters = routes[0]["distance"]
        seconds = routes[0]["duration"]
    except (KeyError, TypeError):
        return None
    return (meters / METERS_PER_MILE, seconds / SECONDS_PER_MINUTE)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_commute.py -v`
Expected: PASS (6 passed)

- [ ] **Step 5: Commit**

```bash
git add src/commute.py tests/test_commute.py
git commit -m "feat(commute): add geocode and route parsing"
```

---

### Task 2: `src/commute.py` — destination resolution and per-listing commute

**Files:**
- Modify: `src/commute.py`
- Test: `tests/test_commute.py`

**Interfaces:**
- Consumes: `geocode`, `route_miles_minutes` from Task 1 (same signatures)
- Produces: `CommuteResult` dataclass with fields `lat, lon, denver_miles, denver_minutes, medtronic_miles, medtronic_minutes, geocode_failed`
- Produces: `resolve_destination(primary_address: str, geocode_fn: Callable[[str], Coordinates | None], fallback_address: str | None = None) -> tuple[Coordinates, bool]`
- Produces: `compute_commute(address: str, denver_coords: Coordinates, medtronic_coords: Coordinates, geocode_fn: Callable[[str], Coordinates | None], route_fn: Callable[[Coordinates, Coordinates], tuple[float, float] | None]) -> CommuteResult`

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/test_commute.py
import pytest

from src.commute import CommuteResult, compute_commute, resolve_destination

DENVER = (39.7527, -105.0016)
MEDTRONIC = (39.9997, -105.0908)


def test_resolve_destination_uses_primary_when_it_geocodes():
    coords, used_fallback = resolve_destination("Medtronic, Lafayette, CO", lambda addr: MEDTRONIC)

    assert coords == MEDTRONIC
    assert used_fallback is False


def test_resolve_destination_falls_back_when_primary_fails():
    def fake_geocode(addr: str):
        return None if addr == "Medtronic, Lafayette, CO" else MEDTRONIC

    coords, used_fallback = resolve_destination(
        "Medtronic, Lafayette, CO", fake_geocode, fallback_address="Lafayette, CO"
    )

    assert coords == MEDTRONIC
    assert used_fallback is True


def test_resolve_destination_raises_when_both_fail():
    with pytest.raises(RuntimeError):
        resolve_destination("nowhere", lambda addr: None, fallback_address="also nowhere")


def test_resolve_destination_raises_when_primary_fails_and_no_fallback_given():
    with pytest.raises(RuntimeError):
        resolve_destination("nowhere", lambda addr: None)


def test_compute_commute_returns_geocode_failed_when_address_doesnt_geocode():
    result = compute_commute(
        "1 Nowhere Rd", DENVER, MEDTRONIC, lambda addr: None, lambda o, d: (5.0, 10.0)
    )

    assert result == CommuteResult(None, None, None, None, None, None, geocode_failed=True)


def test_compute_commute_computes_both_legs():
    origin = (39.85, -105.05)

    def fake_route(o, d):
        return (5.0, 12.0) if d == DENVER else (8.0, 22.0)

    result = compute_commute(
        "1 Real St", DENVER, MEDTRONIC, lambda addr: origin, fake_route
    )

    assert result == CommuteResult(
        lat=39.85, lon=-105.05,
        denver_miles=5.0, denver_minutes=12.0,
        medtronic_miles=8.0, medtronic_minutes=22.0,
        geocode_failed=False,
    )


def test_compute_commute_marks_failed_when_a_route_fails():
    origin = (39.85, -105.05)

    def fake_route(o, d):
        return None if d == MEDTRONIC else (5.0, 12.0)

    result = compute_commute("1 Real St", DENVER, MEDTRONIC, lambda addr: origin, fake_route)

    assert result.geocode_failed is True
    assert result.denver_miles == 5.0
    assert result.medtronic_miles is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_commute.py -v`
Expected: FAIL — `ImportError: cannot import name 'CommuteResult' from 'src.commute'`

- [ ] **Step 3: Write the minimal implementation**

```python
# append to src/commute.py

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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_commute.py -v`
Expected: PASS (13 passed)

- [ ] **Step 5: Commit**

```bash
git add src/commute.py tests/test_commute.py
git commit -m "feat(commute): add destination resolution and per-listing commute"
```

---

### Task 3: `src/db.py` — commute table

**Files:**
- Modify: `src/db.py`
- Test: `tests/test_db.py`

**Interfaces:**
- Consumes: `CommuteResult` from `src/commute.py` (Task 2)
- Produces: `upsert_commute(conn: sqlite3.Connection, listing_id: str, result: CommuteResult) -> None`
- Produces: `get_commute(conn: sqlite3.Connection, listing_id: str) -> sqlite3.Row | None`
- Produces: `get_listing_ids_missing_commute(conn: sqlite3.Connection) -> list[str]`

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/test_db.py
from src.commute import CommuteResult
from src.db import get_commute, get_listing_ids_missing_commute, upsert_commute

COMMUTE_SAMPLE = CommuteResult(
    lat=39.85, lon=-105.05,
    denver_miles=12.0, denver_minutes=25.0,
    medtronic_miles=8.0, medtronic_minutes=18.0,
    geocode_failed=False,
)


def test_upsert_commute_then_get_commute(tmp_path: Path):
    conn = get_connection(_db_path(tmp_path))
    upsert_listing(conn, SAMPLE)

    upsert_commute(conn, "abc123", COMMUTE_SAMPLE)

    row = get_commute(conn, "abc123")
    assert row["denver_minutes"] == 25.0
    assert row["medtronic_minutes"] == 18.0
    assert row["geocode_failed"] == 0


def test_get_commute_returns_none_when_absent(tmp_path: Path):
    conn = get_connection(_db_path(tmp_path))
    upsert_listing(conn, SAMPLE)

    assert get_commute(conn, "abc123") is None


def test_upsert_commute_is_idempotent(tmp_path: Path):
    conn = get_connection(_db_path(tmp_path))
    upsert_listing(conn, SAMPLE)
    upsert_commute(conn, "abc123", COMMUTE_SAMPLE)

    updated = CommuteResult(**{**COMMUTE_SAMPLE.__dict__, "denver_minutes": 30.0})
    upsert_commute(conn, "abc123", updated)

    row = get_commute(conn, "abc123")
    assert row["denver_minutes"] == 30.0


def test_get_listing_ids_missing_commute(tmp_path: Path):
    conn = get_connection(_db_path(tmp_path))
    upsert_listing(conn, SAMPLE)
    other = SAMPLE.__class__(**{**SAMPLE.__dict__, "listing_id": "other456"})
    upsert_listing(conn, other)
    upsert_commute(conn, "abc123", COMMUTE_SAMPLE)

    assert get_listing_ids_missing_commute(conn) == ["other456"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_db.py -v`
Expected: FAIL — `ImportError: cannot import name 'upsert_commute' from 'src.db'`

- [ ] **Step 3: Write the minimal implementation**

```python
# src/db.py — add to imports at top
from datetime import datetime, timezone

from src.commute import CommuteResult
```

```python
# src/db.py — add to _SCHEMA, before the closing triple-quote
CREATE TABLE IF NOT EXISTS commute (
    listing_id TEXT PRIMARY KEY REFERENCES listings(listing_id),
    lat REAL,
    lon REAL,
    denver_miles REAL,
    denver_minutes REAL,
    medtronic_miles REAL,
    medtronic_minutes REAL,
    geocode_failed INTEGER NOT NULL,
    computed_at TEXT NOT NULL
);
```

```python
# src/db.py — append at end of file
def upsert_commute(conn: sqlite3.Connection, listing_id: str, result: CommuteResult) -> None:
    """Insert or replace a listing's cached commute data. Safe to call
    repeatedly — a rerun after a rubric change shouldn't need to re-geocode,
    but re-running after a genuine address fix should overwrite cleanly."""
    with conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO commute (
                listing_id, lat, lon, denver_miles, denver_minutes,
                medtronic_miles, medtronic_minutes, geocode_failed, computed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                listing_id,
                result.lat,
                result.lon,
                result.denver_miles,
                result.denver_minutes,
                result.medtronic_miles,
                result.medtronic_minutes,
                int(result.geocode_failed),
                datetime.now(timezone.utc).isoformat(),
            ),
        )


def get_commute(conn: sqlite3.Connection, listing_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM commute WHERE listing_id = ?", (listing_id,)
    ).fetchone()


def get_listing_ids_missing_commute(conn: sqlite3.Connection) -> list[str]:
    """Listings with no commute row yet — a rerun only pays the
    geocode/routing cost for listings it hasn't already covered."""
    rows = conn.execute(
        """
        SELECT listing_id FROM listings
        WHERE listing_id NOT IN (SELECT listing_id FROM commute)
        """
    ).fetchall()
    return [row["listing_id"] for row in rows]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_db.py -v`
Expected: PASS (all `test_db.py` tests, including the 4 new ones)

- [ ] **Step 5: Commit**

```bash
git add src/db.py tests/test_db.py
git commit -m "feat(db): add commute table and accessors"
```

---

### Task 4: `compute_commutes.py` — orchestration script

**Files:**
- Create: `compute_commutes.py`

**Interfaces:**
- Consumes: `geocode`, `route_miles_minutes`, `resolve_destination`, `compute_commute` from `src/commute.py`; `get_connection`, `query_listings`, `get_listing_ids_missing_commute`, `upsert_commute` from `src/db.py`

No test file — this is untested orchestration, matching `scrape.py`/`check.py`/`backfill_db.py` precedent (no `tests/test_scrape.py` exists either).

- [ ] **Step 1: Write the script**

```python
# compute_commutes.py
import time
from pathlib import Path

import requests

from src.commute import compute_commute, geocode, resolve_destination, route_miles_minutes
from src.db import get_connection, get_listing_ids_missing_commute, query_listings, upsert_commute

DATA_DIR = Path("data")
DB_PATH = DATA_DIR / "listings.db"
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
    conn = get_connection(DB_PATH)
    listings_by_id = {row["listing_id"]: row for row in query_listings(conn)}
    missing_ids = get_listing_ids_missing_commute(conn)

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
```

- [ ] **Step 2: Smoke-test against the real database**

Run: `python compute_commutes.py`
Expected: prints one line per listing missing commute data, ending without a traceback. Rerun immediately after — expect `commute table already covers every listing` (idempotent, confirming `get_listing_ids_missing_commute` works against the real DB).

- [ ] **Step 3: Commit**

```bash
git add compute_commutes.py
git commit -m "feat(commute): add commute backfill script"
```

---

### Task 5: `src/scoring.py` — sub-score functions

**Files:**
- Create: `src/scoring.py`
- Test: `tests/test_scoring.py`

**Interfaces:**
- Produces: `score_commute(medtronic_minutes: float | None, denver_minutes: float | None, denver_min: float, denver_max: float) -> float`
- Produces: `score_sqft(sqft: int, sqft_min: int, sqft_max: int) -> float`
- Produces: `score_condition(description: str, amenities: list[str], year_built: int) -> float`
- Produces: `score_outdoor(description: str, amenities: list[str]) -> float`
- Produces: `score_parking(parking_spaces: int) -> float`
- Produces: `passes_filters(baths: float, lot_sqft: int) -> bool`
- Produces: constants `NEUTRAL_SCORE`, `MIN_BATHS`, `MIN_LOT_SQFT`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_scoring.py
from src.scoring import (
    passes_filters,
    score_commute,
    score_condition,
    score_outdoor,
    score_parking,
    score_sqft,
)


def test_score_commute_full_marks_at_or_under_twenty_minutes():
    assert score_commute(20.0, 20.0, 15.0, 30.0) == 100.0


def test_score_commute_linear_slide_between_twenty_and_thirty():
    # medtronic leg only: 25min -> 70; denver leg pinned at its own min -> 100
    result = score_commute(25.0, 15.0, 15.0, 30.0)
    assert result == 0.8 * 70.0 + 0.2 * 100.0


def test_score_commute_forty_minutes_medtronic_leg_is_zero():
    result = score_commute(40.0, 15.0, 15.0, 30.0)
    assert result == 0.8 * 0.0 + 0.2 * 100.0


def test_score_commute_beyond_forty_minutes_stays_zero():
    result = score_commute(55.0, 15.0, 15.0, 30.0)
    assert result == 0.8 * 0.0 + 0.2 * 100.0


def test_score_commute_denver_leg_min_max_normalized():
    # denver leg at the collection's max minutes scores 0
    result = score_commute(20.0, 30.0, 15.0, 30.0)
    assert result == 0.8 * 100.0 + 0.2 * 0.0


def test_score_commute_missing_data_is_neutral():
    result = score_commute(None, None, 15.0, 30.0)
    assert result == 50.0


def test_score_commute_no_variance_in_denver_range_scores_full():
    result = score_commute(20.0, 20.0, 20.0, 20.0)
    assert result == 100.0


def test_score_sqft_min_max_normalizes_across_collection():
    assert score_sqft(2000, 1000, 3000) == 50.0
    assert score_sqft(1000, 1000, 3000) == 0.0
    assert score_sqft(3000, 1000, 3000) == 100.0


def test_score_sqft_missing_is_neutral():
    assert score_sqft(0, 1000, 3000) == 50.0


def test_score_sqft_no_variance_scores_full():
    assert score_sqft(2000, 2000, 2000) == 100.0


def test_score_condition_renovation_keyword_dominates():
    high = score_condition("Beautifully Renovated kitchen", [], 1960)
    low = score_condition("Original condition", [], 1960)
    assert high > low


def test_score_condition_keyword_match_is_case_insensitive_and_checks_amenities():
    result = score_condition("Charming home", ["Fully Remodeled"], 1960)
    assert result == 0.8 * 100.0 + 0.2 * score_condition("", [], 1960) / 0.2 * 0.2  # sanity anchor below


def test_score_condition_missing_year_built_is_neutral_secondary_signal():
    with_keyword_no_year = score_condition("Renovated", [], 0)
    assert with_keyword_no_year == 0.8 * 100.0 + 0.2 * 50.0


def test_score_condition_newer_year_scores_higher_without_keyword():
    older = score_condition("Original condition", [], 1955)
    newer = score_condition("Original condition", [], 2005)
    assert newer > older


def test_score_outdoor_keyword_hit_scores_high():
    result = score_outdoor("Private yard with mature trees", [])
    assert result == 100.0


def test_score_outdoor_checks_amenities_too():
    result = score_outdoor("Charming home", ["Great for Entertaining"])
    assert result == 100.0


def test_score_outdoor_no_keyword_is_weak_not_zero():
    result = score_outdoor("A house", [])
    assert 0.0 < result < 100.0


def test_score_parking_two_or_more_spaces_is_full():
    assert score_parking(2) == 100.0
    assert score_parking(4) == 100.0


def test_score_parking_one_space_is_high_but_not_full():
    assert score_parking(1) == 90.0


def test_score_parking_zero_or_missing_is_zero():
    assert score_parking(0) == 0.0


def test_passes_filters_requires_both_thresholds():
    assert passes_filters(baths=2.0, lot_sqft=6000) is True
    assert passes_filters(baths=1.5, lot_sqft=6000) is False
    assert passes_filters(baths=2.0, lot_sqft=5999) is False
```

Fix the one over-clever test above before running — it's testing the right idea (keyword dominates, year_built fills the rest) but written confusingly. Replace `test_score_condition_keyword_match_is_case_insensitive_and_checks_amenities` with:

```python
def test_score_condition_keyword_match_is_case_insensitive_and_checks_amenities():
    result = score_condition("Charming home", ["Fully Remodeled"], 1980)
    year_component = 0.2 * ((1980 - 1955) / (2005 - 1955) * 100.0)
    assert result == 0.8 * 100.0 + year_component
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_scoring.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'src.scoring'`

- [ ] **Step 3: Write the minimal implementation**

```python
# src/scoring.py
NEUTRAL_SCORE = 50.0

MEDTRONIC_LEG_WEIGHT = 0.8
DENVER_LEG_WEIGHT = 0.2

YEAR_BUILT_MIN = 1955
YEAR_BUILT_MAX = 2005
CONDITION_KEYWORD_WEIGHT = 0.8
CONDITION_YEAR_WEIGHT = 0.2

RENOVATION_KEYWORDS = [
    "renovated",
    "updated kitchen",
    "remodeled",
    "new roof",
    "newly renovated",
    "fully updated",
    "gut renovated",
]

OUTDOOR_KEYWORDS = [
    "mature trees",
    "private yard",
    "backyard",
    "open floor plan",
    "entertaining",
    "outdoor living",
]
OUTDOOR_KEYWORD_HIT_SCORE = 100.0
# Absence of these phrases isn't proof there's no yard — this is an
# explicitly weak placeholder until photo scoring exists, so a miss
# isn't punished as heavily as a real negative signal would be.
OUTDOOR_NO_KEYWORD_SCORE = 40.0

MIN_BATHS = 2.0
MIN_LOT_SQFT = 6000


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def _has_any_keyword(text: str, keywords: list[str]) -> bool:
    lowered = text.lower()
    return any(keyword in lowered for keyword in keywords)


def _medtronic_leg_score(minutes: float) -> float:
    if minutes <= 20:
        return 100.0
    if minutes <= 30:
        return 100.0 - (minutes - 20) * 6.0
    if minutes <= 40:
        return 40.0 - (minutes - 30) * 4.0
    return 0.0


def _denver_leg_score(minutes: float, denver_min: float, denver_max: float) -> float:
    if denver_max <= denver_min:
        return 100.0
    normalized = (denver_max - minutes) / (denver_max - denver_min)
    return _clamp(normalized * 100.0)


def score_commute(
    medtronic_minutes: float | None,
    denver_minutes: float | None,
    denver_min: float,
    denver_max: float,
) -> float:
    medtronic_score = (
        _medtronic_leg_score(medtronic_minutes) if medtronic_minutes is not None else NEUTRAL_SCORE
    )
    denver_score = (
        _denver_leg_score(denver_minutes, denver_min, denver_max)
        if denver_minutes is not None
        else NEUTRAL_SCORE
    )
    return MEDTRONIC_LEG_WEIGHT * medtronic_score + DENVER_LEG_WEIGHT * denver_score


def score_sqft(sqft: int, sqft_min: int, sqft_max: int) -> float:
    if not sqft:
        return NEUTRAL_SCORE
    if sqft_max <= sqft_min:
        return 100.0
    return _clamp((sqft - sqft_min) / (sqft_max - sqft_min) * 100.0)


def score_condition(description: str, amenities: list[str], year_built: int) -> float:
    combined = f"{description} {' '.join(amenities)}"
    keyword_score = 100.0 if _has_any_keyword(combined, RENOVATION_KEYWORDS) else 0.0
    if not year_built:
        year_score = NEUTRAL_SCORE
    else:
        normalized = (year_built - YEAR_BUILT_MIN) / (YEAR_BUILT_MAX - YEAR_BUILT_MIN)
        year_score = _clamp(normalized * 100.0)
    return CONDITION_KEYWORD_WEIGHT * keyword_score + CONDITION_YEAR_WEIGHT * year_score


def score_outdoor(description: str, amenities: list[str]) -> float:
    combined = f"{description} {' '.join(amenities)}"
    return OUTDOOR_KEYWORD_HIT_SCORE if _has_any_keyword(combined, OUTDOOR_KEYWORDS) else OUTDOOR_NO_KEYWORD_SCORE


def score_parking(parking_spaces: int) -> float:
    if parking_spaces >= 2:
        return 100.0
    if parking_spaces == 1:
        return 90.0
    return 0.0


def passes_filters(baths: float, lot_sqft: int) -> bool:
    return baths >= MIN_BATHS and lot_sqft >= MIN_LOT_SQFT
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_scoring.py -v`
Expected: PASS (all tests)

- [ ] **Step 5: Commit**

```bash
git add src/scoring.py tests/test_scoring.py
git commit -m "feat(scoring): add sub-score functions"
```

---

### Task 6: `src/scoring.py` — collection stats and composite score

**Files:**
- Modify: `src/scoring.py`
- Test: `tests/test_scoring.py`

**Interfaces:**
- Consumes: `score_commute`, `score_sqft`, `score_condition`, `score_outdoor`, `score_parking`, `passes_filters` from Task 5; `Listing` from `src/models.py`
- Produces: `CollectionStats` dataclass (`sqft_min, sqft_max, denver_minutes_min, denver_minutes_max`)
- Produces: `compute_collection_stats(sqft_values: list[int], denver_minutes_values: list[float]) -> CollectionStats`
- Produces: `ScoreResult` dataclass (`commute_score, sqft_score, condition_score, outdoor_score, parking_score, composite, passes_filters`)
- Produces: `score_listing(listing: Listing, medtronic_minutes: float | None, denver_minutes: float | None, stats: CollectionStats) -> ScoreResult`
- Produces: constants `WEIGHT_COMMUTE`, `WEIGHT_SQFT`, `WEIGHT_CONDITION`, `WEIGHT_OUTDOOR`, `WEIGHT_PARKING`

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/test_scoring.py
from src.models import Listing
from src.scoring import (
    CollectionStats,
    WEIGHT_COMMUTE,
    WEIGHT_CONDITION,
    WEIGHT_OUTDOOR,
    WEIGHT_PARKING,
    WEIGHT_SQFT,
    compute_collection_stats,
    score_listing,
)

LISTING = Listing(
    listing_id="abc123",
    address="1 Test St",
    city="Testville",
    state="CO",
    zip_code="80020",
    price="$650,000",
    beds=4,
    baths=2.5,
    sqft=2000,
    lot_sqft=6500,
    parking_spaces=2,
    year_built=1980,
    description="Renovated kitchen, private yard with mature trees",
    amenities=["Garage"],
    photo_urls=[],
    listing_url="https://example.com/listing/abc123",
)


def test_compute_collection_stats_returns_min_and_max():
    stats = compute_collection_stats([1000, 2000, 3000], [10.0, 20.0, 30.0])

    assert stats == CollectionStats(
        sqft_min=1000, sqft_max=3000, denver_minutes_min=10.0, denver_minutes_max=30.0
    )


def test_compute_collection_stats_handles_empty_input():
    stats = compute_collection_stats([], [])

    assert stats == CollectionStats(0, 0, 0.0, 0.0)


def test_score_listing_combines_sub_scores_with_named_weights():
    stats = CollectionStats(sqft_min=1000, sqft_max=3000, denver_minutes_min=10.0, denver_minutes_max=30.0)

    result = score_listing(LISTING, medtronic_minutes=18.0, denver_minutes=15.0, stats=stats)

    expected = (
        WEIGHT_COMMUTE * result.commute_score
        + WEIGHT_SQFT * result.sqft_score
        + WEIGHT_CONDITION * result.condition_score
        + WEIGHT_OUTDOOR * result.outdoor_score
        + WEIGHT_PARKING * result.parking_score
    )
    assert result.composite == expected


def test_score_listing_sets_passes_filters_flag():
    stats = CollectionStats(1000, 3000, 10.0, 30.0)

    passing = score_listing(LISTING, 18.0, 15.0, stats)
    failing_listing = LISTING.__class__(**{**LISTING.__dict__, "baths": 1.0})
    failing = score_listing(failing_listing, 18.0, 15.0, stats)

    assert passing.passes_filters is True
    assert failing.passes_filters is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_scoring.py -v`
Expected: FAIL — `ImportError: cannot import name 'CollectionStats' from 'src.scoring'`

- [ ] **Step 3: Write the minimal implementation**

```python
# src/scoring.py — add near the top, after the existing constants
WEIGHT_COMMUTE = 0.35
WEIGHT_SQFT = 0.20
WEIGHT_CONDITION = 0.20
WEIGHT_OUTDOOR = 0.15
WEIGHT_PARKING = 0.10
```

```python
# src/scoring.py — add imports at top
from dataclasses import dataclass

from src.models import Listing
```

```python
# src/scoring.py — append at end of file
@dataclass
class CollectionStats:
    sqft_min: int
    sqft_max: int
    denver_minutes_min: float
    denver_minutes_max: float


def compute_collection_stats(
    sqft_values: list[int], denver_minutes_values: list[float]
) -> CollectionStats:
    return CollectionStats(
        sqft_min=min(sqft_values) if sqft_values else 0,
        sqft_max=max(sqft_values) if sqft_values else 0,
        denver_minutes_min=min(denver_minutes_values) if denver_minutes_values else 0.0,
        denver_minutes_max=max(denver_minutes_values) if denver_minutes_values else 0.0,
    )


@dataclass
class ScoreResult:
    commute_score: float
    sqft_score: float
    condition_score: float
    outdoor_score: float
    parking_score: float
    composite: float
    passes_filters: bool


def score_listing(
    listing: Listing,
    medtronic_minutes: float | None,
    denver_minutes: float | None,
    stats: CollectionStats,
) -> ScoreResult:
    commute_score = score_commute(
        medtronic_minutes, denver_minutes, stats.denver_minutes_min, stats.denver_minutes_max
    )
    sqft_score = score_sqft(listing.sqft, stats.sqft_min, stats.sqft_max)
    condition_score = score_condition(listing.description, listing.amenities, listing.year_built)
    outdoor_score = score_outdoor(listing.description, listing.amenities)
    parking_score = score_parking(listing.parking_spaces)
    composite = (
        WEIGHT_COMMUTE * commute_score
        + WEIGHT_SQFT * sqft_score
        + WEIGHT_CONDITION * condition_score
        + WEIGHT_OUTDOOR * outdoor_score
        + WEIGHT_PARKING * parking_score
    )
    return ScoreResult(
        commute_score=commute_score,
        sqft_score=sqft_score,
        condition_score=condition_score,
        outdoor_score=outdoor_score,
        parking_score=parking_score,
        composite=composite,
        passes_filters=passes_filters(listing.baths, listing.lot_sqft),
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_scoring.py -v`
Expected: PASS (all tests)

- [ ] **Step 5: Commit**

```bash
git add src/scoring.py tests/test_scoring.py
git commit -m "feat(scoring): add collection stats and composite score"
```

---

### Task 7: `src/db.py` — scores table and amenities accessor

**Files:**
- Modify: `src/db.py`
- Test: `tests/test_db.py`

**Interfaces:**
- Consumes: `ScoreResult` from `src/scoring.py` (Task 6)
- Produces: `upsert_score(conn: sqlite3.Connection, listing_id: str, result: ScoreResult) -> None`
- Produces: `get_scores(conn: sqlite3.Connection) -> list[sqlite3.Row]` (ordered by `composite` descending)
- Produces: `get_amenities(conn: sqlite3.Connection, listing_id: str) -> list[str]`

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/test_db.py
from src.db import get_amenities, get_scores, upsert_score
from src.scoring import ScoreResult

SCORE_SAMPLE = ScoreResult(
    commute_score=80.0, sqft_score=50.0, condition_score=90.0,
    outdoor_score=100.0, parking_score=100.0, composite=79.5,
    passes_filters=True,
)


def test_upsert_score_then_get_scores(tmp_path: Path):
    conn = get_connection(_db_path(tmp_path))
    upsert_listing(conn, SAMPLE)

    upsert_score(conn, "abc123", SCORE_SAMPLE)

    rows = get_scores(conn)
    assert len(rows) == 1
    assert rows[0]["listing_id"] == "abc123"
    assert rows[0]["composite"] == 79.5
    assert rows[0]["passes_filters"] == 1


def test_get_scores_orders_by_composite_descending(tmp_path: Path):
    conn = get_connection(_db_path(tmp_path))
    upsert_listing(conn, SAMPLE)
    other = SAMPLE.__class__(**{**SAMPLE.__dict__, "listing_id": "other456"})
    upsert_listing(conn, other)

    upsert_score(conn, "abc123", ScoreResult(**{**SCORE_SAMPLE.__dict__, "composite": 40.0}))
    upsert_score(conn, "other456", ScoreResult(**{**SCORE_SAMPLE.__dict__, "composite": 90.0}))

    rows = get_scores(conn)
    assert [row["listing_id"] for row in rows] == ["other456", "abc123"]


def test_upsert_score_is_idempotent(tmp_path: Path):
    conn = get_connection(_db_path(tmp_path))
    upsert_listing(conn, SAMPLE)
    upsert_score(conn, "abc123", SCORE_SAMPLE)

    updated = ScoreResult(**{**SCORE_SAMPLE.__dict__, "composite": 55.0})
    upsert_score(conn, "abc123", updated)

    rows = get_scores(conn)
    assert len(rows) == 1
    assert rows[0]["composite"] == 55.0


def test_get_amenities_returns_list_for_listing_id(tmp_path: Path):
    conn = get_connection(_db_path(tmp_path))
    upsert_listing(conn, SAMPLE)

    assert get_amenities(conn, "abc123") == ["Central AC", "Garage"]


def test_get_amenities_empty_for_unknown_listing(tmp_path: Path):
    conn = get_connection(_db_path(tmp_path))

    assert get_amenities(conn, "nope") == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_db.py -v`
Expected: FAIL — `ImportError: cannot import name 'upsert_score' from 'src.db'`

- [ ] **Step 3: Write the minimal implementation**

```python
# src/db.py — add import at top
from src.scoring import ScoreResult
```

```python
# src/db.py — add to _SCHEMA, before the closing triple-quote
CREATE TABLE IF NOT EXISTS scores (
    listing_id TEXT PRIMARY KEY REFERENCES listings(listing_id),
    commute_score REAL NOT NULL,
    sqft_score REAL NOT NULL,
    condition_score REAL NOT NULL,
    outdoor_score REAL NOT NULL,
    parking_score REAL NOT NULL,
    composite REAL NOT NULL,
    passes_filters INTEGER NOT NULL,
    computed_at TEXT NOT NULL
);
```

```python
# src/db.py — append at end of file
def upsert_score(conn: sqlite3.Connection, listing_id: str, result: ScoreResult) -> None:
    """Insert or replace a listing's score row. Cheap and rebuildable —
    intended to run every time the scoring rubric changes, not just once."""
    with conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO scores (
                listing_id, commute_score, sqft_score, condition_score,
                outdoor_score, parking_score, composite, passes_filters, computed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                listing_id,
                result.commute_score,
                result.sqft_score,
                result.condition_score,
                result.outdoor_score,
                result.parking_score,
                result.composite,
                int(result.passes_filters),
                datetime.now(timezone.utc).isoformat(),
            ),
        )


def get_scores(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM scores ORDER BY composite DESC").fetchall()


def get_amenities(conn: sqlite3.Connection, listing_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT amenity FROM amenities WHERE listing_id = ? ORDER BY amenity", (listing_id,)
    ).fetchall()
    return [row["amenity"] for row in rows]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_db.py -v`
Expected: PASS (all `test_db.py` tests)

- [ ] **Step 5: Commit**

```bash
git add src/db.py tests/test_db.py
git commit -m "feat(db): add scores table and amenities accessor"
```

---

### Task 8: `score.py` — orchestration script and ranked report

**Files:**
- Create: `score.py`

**Interfaces:**
- Consumes: `get_connection`, `query_listings`, `get_amenities`, `get_commute`, `upsert_score` from `src/db.py`; `compute_collection_stats`, `score_listing` from `src/scoring.py`; `Listing` from `src/models.py`

No test file — untested orchestration, same convention as `scrape.py`/`check.py`/`compute_commutes.py`.

- [ ] **Step 1: Write the script**

```python
# score.py
from pathlib import Path

from src.db import get_amenities, get_commute, get_connection, query_listings, upsert_score
from src.models import Listing
from src.scoring import compute_collection_stats, score_listing

DATA_DIR = Path("data")
DB_PATH = DATA_DIR / "listings.db"


def _row_to_listing(row, amenities: list[str]) -> Listing:
    return Listing(
        listing_id=row["listing_id"],
        address=row["address"],
        city=row["city"],
        state=row["state"],
        zip_code=row["zip_code"],
        price=row["price"],
        beds=row["beds"],
        baths=row["baths"],
        sqft=row["sqft"],
        lot_sqft=row["lot_sqft"],
        parking_spaces=row["parking_spaces"],
        year_built=row["year_built"],
        description=row["description"],
        amenities=amenities,
        photo_urls=[],
        listing_url=row["listing_url"],
    )


def main() -> None:
    conn = get_connection(DB_PATH)
    rows = query_listings(conn)
    listings = [_row_to_listing(row, get_amenities(conn, row["listing_id"])) for row in rows]

    commute_by_id = {listing.listing_id: get_commute(conn, listing.listing_id) for listing in listings}
    sqft_values = [listing.sqft for listing in listings if listing.sqft]
    denver_minutes_values = [
        commute["denver_minutes"]
        for commute in commute_by_id.values()
        if commute is not None and commute["denver_minutes"] is not None
    ]
    stats = compute_collection_stats(sqft_values, denver_minutes_values)

    ranked = []
    for listing in listings:
        commute = commute_by_id[listing.listing_id]
        medtronic_minutes = commute["medtronic_minutes"] if commute else None
        denver_minutes = commute["denver_minutes"] if commute else None
        result = score_listing(listing, medtronic_minutes, denver_minutes, stats)
        upsert_score(conn, listing.listing_id, result)
        ranked.append((listing, result))

    conn.close()

    ranked.sort(key=lambda pair: pair[1].composite, reverse=True)
    for listing, result in ranked:
        flag = "PASS" if result.passes_filters else "    "
        print(
            f"[{flag}] {result.composite:5.1f}  {listing.address:40s} "
            f"commute={result.commute_score:5.1f} sqft={result.sqft_score:5.1f} "
            f"condition={result.condition_score:5.1f} outdoor={result.outdoor_score:5.1f} "
            f"parking={result.parking_score:5.1f}"
        )

    print(f"\nScored {len(ranked)} listings into {DB_PATH}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Smoke-test against the real database**

Run: `python score.py`
Expected: prints one ranked line per listing in `data/listings.db`, highest composite first, ending with the summary line. Listings whose commute hasn't been computed yet (if `compute_commutes.py` hasn't run against the full set) show a commute sub-score of `50.0` (neutral), not a crash.

- [ ] **Step 3: Commit**

```bash
git add score.py
git commit -m "feat(scoring): add score orchestration and ranked report"
```

---

## Self-Review

**Spec coverage:**
- Composite + visible sub-scores → Task 6/8 (`ScoreResult`, printed report). ✓
- Hard cutoffs baths≥2, lot≥6000 as a flag, not a filter → Task 5/6 (`passes_filters`, recorded not deleted). ✓
- Real commute via Nominatim+OSRM, not guessed → Tasks 1–4. ✓
- Weak keyword placeholder for outdoor/hosting → Task 5 (`score_outdoor`, `OUTDOOR_NO_KEYWORD_SCORE` explicitly non-zero, non-full). ✓
- Weights as named constants → `WEIGHT_*` constants in Task 6, `RENOVATION_KEYWORDS`/`OUTDOOR_KEYWORDS` in Task 5. ✓
- Commute curve (100@≤20min, linear to 40@30min, ~0 by 40min; Denver leg min-max normalized) → Task 5 `_medtronic_leg_score`/`_denver_leg_score`, boundary-tested. ✓
- Condition: renovation keyword dominant, year_built secondary, 1955–2005 range → Task 5 `score_condition`. ✓
- Parking step function (2+=100, 1=90, 0=0) → Task 5 `score_parking`, boundary-tested. ✓
- `commute` table schema (listing_id, lat, lon, denver_miles/minutes, medtronic_miles/minutes, geocode_failed, computed_at) → Task 3, matches spec exactly. ✓
- Only computes missing listings, rerun is cheap → Task 3 `get_listing_ids_missing_commute` + Task 4 loop. ✓
- Geocode/route failure logs and skips rather than aborting the batch → Task 4's per-listing `try/except`. ✓
- Missing input stat scores neutral, not zero → `NEUTRAL_SCORE` used in `score_commute`, `score_sqft`, `score_condition` (Task 5). ✓
- No new `.env` variables → confirmed, no config changes anywhere in this plan. ✓
- Test house style (module-level fixtures, `tmp_path`, no test classes) → followed throughout. ✓
- No live-network tests for geocode/routing → Tasks 1–2 tests all use fake injected callables; Task 4/8 orchestration scripts are untested, matching `scrape.py`/`check.py`. ✓

**Placeholder scan:** No "TBD"/"handle appropriately"/"similar to Task N" language; every step has literal code. Fixed the one confusing test in Task 5 (Step 1) by rewriting it before the run-tests step, rather than leaving it ambiguous.

**Type consistency:** `CommuteResult` fields (Task 2) match exactly what Task 3's `upsert_commute`/`get_commute` read and write. `ScoreResult` fields (Task 6) match Task 7's `upsert_score` columns and Task 8's report formatting. `compute_collection_stats` signature (`sqft_values: list[int], denver_minutes_values: list[float]`) is used identically in Task 6's tests and Task 8's script. `score_listing`'s parameter order (`listing, medtronic_minutes, denver_minutes, stats`) is consistent between Task 6 and Task 8.
