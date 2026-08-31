import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from src.commute import CommuteResult
from src.models import Listing
from src.scoring import ScoreResult
from src.vision import VisualScoreResult

_SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    listing_id TEXT PRIMARY KEY,
    address TEXT NOT NULL,
    city TEXT NOT NULL,
    state TEXT NOT NULL,
    zip_code TEXT NOT NULL,
    price TEXT NOT NULL,
    price_numeric REAL,
    beds INTEGER NOT NULL,
    baths REAL NOT NULL,
    sqft INTEGER NOT NULL,
    lot_sqft INTEGER NOT NULL,
    parking_spaces INTEGER NOT NULL,
    year_built INTEGER NOT NULL,
    description TEXT NOT NULL,
    listing_url TEXT NOT NULL,
    is_pinned INTEGER NOT NULL DEFAULT 0,
    property_type TEXT NOT NULL DEFAULT '',
    localized_status TEXT NOT NULL DEFAULT '',
    hoa_annual REAL,
    tax_annual REAL,
    sqft_above_grade INTEGER,
    sqft_below_grade INTEGER,
    outdoor_spaces TEXT
);

CREATE TABLE IF NOT EXISTS amenities (
    listing_id TEXT NOT NULL REFERENCES listings(listing_id),
    amenity TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS photo_urls (
    listing_id TEXT NOT NULL REFERENCES listings(listing_id),
    position INTEGER NOT NULL,
    url TEXT NOT NULL
);

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

CREATE TABLE IF NOT EXISTS scores (
    listing_id TEXT PRIMARY KEY REFERENCES listings(listing_id),
    commute_score REAL NOT NULL,
    sqft_score REAL NOT NULL,
    condition_score REAL NOT NULL,
    outdoor_score REAL NOT NULL,
    room_count_score REAL NOT NULL DEFAULT 0,
    parking_score REAL NOT NULL,
    hoa_score REAL NOT NULL DEFAULT 0,
    composite REAL NOT NULL,
    passes_filters INTEGER NOT NULL,
    has_incomplete_data INTEGER NOT NULL,
    computed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS visual_scores (
    listing_id TEXT PRIMARY KEY REFERENCES listings(listing_id),
    condition_photo_score REAL,
    outdoor_photo_score REAL,
    has_layout_plan INTEGER NOT NULL DEFAULT 0,
    layout_plan_clarity_score REAL,
    garage_attached INTEGER,
    watermarked_staging_detected INTEGER NOT NULL DEFAULT 0,
    suspected_unwatermarked_staging INTEGER NOT NULL DEFAULT 0,
    staging_notes TEXT,
    photo_score_unavailable INTEGER NOT NULL,
    raw_response TEXT,
    computed_at TEXT NOT NULL
);
"""

_PRICE_RE = re.compile(r"[\d.]+")


_CREATE_TABLE_RE = re.compile(r"CREATE TABLE IF NOT EXISTS\s+(\w+)\s*\((.*?)\)\s*;", re.DOTALL)
_REFERENCES_RE = re.compile(r"REFERENCES\s+(\w+)\s*\(", re.IGNORECASE)


def tables_parent_first(schema_sql: str | None = None, extra_tables: tuple[str, ...] = ()) -> list[str]:
    """Every table in the schema, ordered so a table always follows the
    tables it references -- i.e. safe INSERT order (parents before children).

    Reverse it for safe DELETE order; see tables_child_first().

    Deleting rows for a listing means deleting children before the parent,
    and inserting means the reverse. Both orders were previously hand-
    maintained in three places, and one of them (publish.py's
    PRUNABLE_TABLES) had it backwards -- silently, because local SQLite does
    not check foreign keys. Deriving the order from the schema removes the
    chance to get it wrong again: add a table with a REFERENCES clause and
    it sorts itself.

    extra_tables covers tables that exist only in the Turso mirror
    (hosted_photos) and so are absent from this schema. They declare no
    foreign keys of their own, but they are keyed by listing_id and must
    still be pruned before listings, so they are treated as children of it.
    """
    schema_sql = _SCHEMA if schema_sql is None else schema_sql
    dependencies: dict[str, set[str]] = {}
    for match in _CREATE_TABLE_RE.finditer(schema_sql):
        table, body = match.group(1), match.group(2)
        dependencies[table] = {
            ref for ref in _REFERENCES_RE.findall(body) if ref != table
        }
    for table in extra_tables:
        dependencies.setdefault(table, {"listings"} if "listings" in dependencies else set())

    ordered: list[str] = []
    remaining = dict(dependencies)
    while remaining:
        free = sorted(t for t, deps in remaining.items() if not (deps & remaining.keys()))
        if not free:  # a reference cycle; emit the rest deterministically
            ordered.extend(sorted(remaining))
            break
        ordered.extend(free)
        for table in free:
            del remaining[table]
    return ordered


def tables_child_first(schema_sql: str | None = None, extra_tables: tuple[str, ...] = ()) -> list[str]:
    """Safe DELETE order: a table always precedes the tables it references,
    so children are removed before the parent row they point at."""
    return list(reversed(tables_parent_first(schema_sql, extra_tables)))


def parse_price(price: str) -> float | None:
    """Best-effort numeric price for range queries, e.g. "$650,000" -> 650000.0.
    Returns None for anything that doesn't contain a number (e.g. "Contact agent")
    rather than raising, since price is display text, not a guaranteed number."""
    match = _PRICE_RE.search(price.replace(",", ""))
    return float(match.group()) if match else None


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    # CREATE TABLE IF NOT EXISTS won't add columns to tables that already
    # existed before a column was introduced — patch pre-existing dbs
    # (e.g. data/listings.db) in place so they pick new columns up too.
    existing_score_columns = {row[1] for row in conn.execute("PRAGMA table_info(scores)")}
    if "has_incomplete_data" not in existing_score_columns:
        conn.execute(
            "ALTER TABLE scores ADD COLUMN has_incomplete_data INTEGER NOT NULL DEFAULT 0"
        )
    if "room_count_score" not in existing_score_columns:
        conn.execute("ALTER TABLE scores ADD COLUMN room_count_score REAL NOT NULL DEFAULT 0")
    # Unlike listings.hoa_annual, a DEFAULT here is harmless: score.py
    # rewrites every scores row on each run, so the default is only ever
    # visible between the migration and the next scoring pass.
    if "hoa_score" not in existing_score_columns:
        conn.execute("ALTER TABLE scores ADD COLUMN hoa_score REAL NOT NULL DEFAULT 0")
    existing_listing_columns = {row[1] for row in conn.execute("PRAGMA table_info(listings)")}
    if "is_pinned" not in existing_listing_columns:
        conn.execute("ALTER TABLE listings ADD COLUMN is_pinned INTEGER NOT NULL DEFAULT 0")
    if "property_type" not in existing_listing_columns:
        conn.execute("ALTER TABLE listings ADD COLUMN property_type TEXT NOT NULL DEFAULT ''")
    if "localized_status" not in existing_listing_columns:
        conn.execute("ALTER TABLE listings ADD COLUMN localized_status TEXT NOT NULL DEFAULT ''")
    # Deliberately nullable with no default: NULL means "HOA unknown",
    # which is scored differently from a confirmed 0.0. A DEFAULT 0 here
    # would silently tell the scorer every un-backfilled listing has no
    # HOA and hand out the no-HOA bonus across the board.
    if "hoa_annual" not in existing_listing_columns:
        conn.execute("ALTER TABLE listings ADD COLUMN hoa_annual REAL")
    # All nullable with no DEFAULT: NULL is meaningful for each. For
    # sqft_below_grade specifically, NULL means "no basement" (Compass omits
    # the key exactly when it reports Basement: No), which a DEFAULT 0 would
    # collapse into "basement with no finished area".
    if "tax_annual" not in existing_listing_columns:
        conn.execute("ALTER TABLE listings ADD COLUMN tax_annual REAL")
    if "sqft_above_grade" not in existing_listing_columns:
        conn.execute("ALTER TABLE listings ADD COLUMN sqft_above_grade INTEGER")
    if "sqft_below_grade" not in existing_listing_columns:
        conn.execute("ALTER TABLE listings ADD COLUMN sqft_below_grade INTEGER")
    if "outdoor_spaces" not in existing_listing_columns:
        conn.execute("ALTER TABLE listings ADD COLUMN outdoor_spaces TEXT")
    conn.commit()


def get_connection(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    # Turso enforces foreign keys; local SQLite defaults them OFF. That
    # divergence made FK bugs production-only -- publish.py pruned the
    # parent listings row before its children for as long as that code
    # existed, and every local run and test passed regardless, because the
    # constraint simply was not checked. Enforcing here means the same
    # mistake fails in dev. Verified against the real db: 0 violations.
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def upsert_listing(conn: sqlite3.Connection, listing: Listing, is_pinned: bool = False) -> None:
    """Insert or fully replace a listing's row and its amenities/photo_urls.
    Safe to call repeatedly for the same listing_id — re-scraping a listing
    should reflect its current state, not accumulate stale child rows.

    is_pinned marks a listing as individually tracked (scraped via a
    LISTING_URLS entry rather than discovered through the collection),
    which exempts it from delisting. Because this is a full row replace,
    every caller must pass the listing's CURRENT pin status on every call
    — omitting it silently un-pins a previously pinned listing the next
    time it's upserted from a different source (e.g. the collection).
    Look it up via get_pinned_listing_ids() first if you're not the one
    setting the pin."""
    with conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO listings (
                listing_id, address, city, state, zip_code,
                price, price_numeric, beds, baths, sqft, lot_sqft,
                parking_spaces, year_built, description, listing_url, is_pinned,
                property_type, localized_status, hoa_annual, tax_annual,
                sqft_above_grade, sqft_below_grade, outdoor_spaces
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                listing.listing_id,
                listing.address,
                listing.city,
                listing.state,
                listing.zip_code,
                listing.price,
                parse_price(listing.price),
                listing.beds,
                listing.baths,
                listing.sqft,
                listing.lot_sqft,
                listing.parking_spaces,
                listing.year_built,
                listing.description,
                listing.listing_url,
                int(is_pinned),
                listing.property_type,
                listing.localized_status,
                listing.hoa_annual,
                listing.tax_annual,
                listing.sqft_above_grade,
                listing.sqft_below_grade,
                json.dumps(listing.outdoor_spaces),
            ),
        )
        conn.execute("DELETE FROM amenities WHERE listing_id = ?", (listing.listing_id,))
        conn.executemany(
            "INSERT INTO amenities (listing_id, amenity) VALUES (?, ?)",
            [(listing.listing_id, amenity) for amenity in listing.amenities],
        )
        conn.execute("DELETE FROM photo_urls WHERE listing_id = ?", (listing.listing_id,))
        conn.executemany(
            "INSERT INTO photo_urls (listing_id, position, url) VALUES (?, ?, ?)",
            [(listing.listing_id, i, url) for i, url in enumerate(listing.photo_urls)],
        )


def get_pinned_listing_ids(conn: sqlite3.Connection) -> frozenset[str]:
    """Listing_ids currently marked is_pinned — tracked individually via
    LISTING_URLS rather than discovered through the collection, and
    therefore exempt from delisting regardless of whether they show up in
    a collection fetch."""
    rows = conn.execute("SELECT listing_id FROM listings WHERE is_pinned = 1").fetchall()
    return frozenset(row["listing_id"] for row in rows)


def get_price_snapshot(conn: sqlite3.Connection) -> dict[str, tuple[str, float | None]]:
    """Maps listing_id -> (price, price_numeric) for every listing currently
    in the db. Taken before upserting fresh fetch data so change-detection
    can tell what a listing's price was before this run touched it."""
    rows = conn.execute("SELECT listing_id, price, price_numeric FROM listings").fetchall()
    return {row["listing_id"]: (row["price"], row["price_numeric"]) for row in rows}


def query_listings(
    conn: sqlite3.Connection,
    min_parking_spaces: int | None = None,
    min_price: float | None = None,
    max_price: float | None = None,
) -> list[sqlite3.Row]:
    clauses = []
    params: list[object] = []
    if min_parking_spaces is not None:
        clauses.append("parking_spaces >= ?")
        params.append(min_parking_spaces)
    if min_price is not None:
        clauses.append("price_numeric >= ?")
        params.append(min_price)
    if max_price is not None:
        clauses.append("price_numeric <= ?")
        params.append(max_price)

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    return conn.execute(
        f"SELECT * FROM listings {where} ORDER BY price_numeric", params
    ).fetchall()


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


def get_listing_ids_missing_commute(
    conn: sqlite3.Connection, retry_failed: bool = True
) -> list[str]:
    """Listings whose commute still needs computing — no row at all, or a
    row that never produced a usable result.

    A rerun still skips every listing already covered, so it only pays the
    geocode/routing cost for work that is actually outstanding.

    retry_failed exists because the original version selected only listings
    with NO commute row, which quietly made failures permanent: a row
    written with geocode_failed=1 (or with no medtronic_minutes) counted as
    "covered" forever, so a transient geocoding hiccup left that listing
    scoring on the NEUTRAL_SCORE fallback for the heaviest-weighted factor
    in the rubric, with nothing to signal it. Eight listings were sitting in
    exactly that state. Pass retry_failed=False for a run that should only
    pick up genuinely new listings.
    """
    if retry_failed:
        rows = conn.execute(
            """
            SELECT l.listing_id FROM listings l
            LEFT JOIN commute c ON c.listing_id = l.listing_id
            WHERE c.listing_id IS NULL
               OR c.geocode_failed = 1
               OR c.medtronic_minutes IS NULL
            """
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT listing_id FROM listings
            WHERE listing_id NOT IN (SELECT listing_id FROM commute)
            """
        ).fetchall()
    return [row["listing_id"] for row in rows]


def upsert_score(conn: sqlite3.Connection, listing_id: str, result: ScoreResult) -> None:
    """Insert or replace a listing's score row. Cheap and rebuildable —
    intended to run every time the scoring rubric changes, not just once."""
    with conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO scores (
                listing_id, commute_score, sqft_score, condition_score,
                outdoor_score, room_count_score, parking_score, hoa_score,
                composite, passes_filters, has_incomplete_data, computed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                listing_id,
                result.commute_score,
                result.sqft_score,
                result.condition_score,
                result.outdoor_score,
                result.room_count_score,
                result.parking_score,
                result.hoa_score,
                result.composite,
                int(result.passes_filters),
                int(result.has_incomplete_data),
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


def delete_listing(conn: sqlite3.Connection, listing_id: str) -> None:
    """Permanently removes a listing and every child row referencing it
    (amenities, photo_urls, commute, scores, visual_scores). Used when a
    listing drops out of the live Compass collection — delisted listings are
    hard-deleted, not archived, since they're never expected to be referenced
    again. Safe to call for a listing_id that doesn't exist.

    Keep this list in step with _SCHEMA: visual_scores was added to the schema
    long after this function and went unnoticed here for months, orphaning one
    row per delisting."""
    with conn:
        # Child-first, derived from the schema rather than hand-listed --
        # see tables_child_first().
        for table in tables_child_first():
            conn.execute(f"DELETE FROM {table} WHERE listing_id = ?", (listing_id,))


def delete_orphaned_rows(conn: sqlite3.Connection) -> dict[str, int]:
    """Removes child rows whose listing no longer exists, and reports how many
    went from each table.

    Cleanup for orphans that accumulated before delete_listing handled every
    child table -- 67 visual_scores rows had built up, one per delisting.
    Turso enforces the foreign keys the local database only declares, so each
    orphan failed to sync to the hosted viewer on every run.

    Discovers child tables from the schema rather than a hardcoded list, for
    the same reason delete_listing's hardcoded list is what went stale."""
    child_tables = [
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name != 'listings'"
        )
        if any(
            info[1] == "listing_id"
            for info in conn.execute(f"PRAGMA table_info({row[0]})")
        )
    ]

    removed: dict[str, int] = {}
    with conn:
        for table in child_tables:
            cursor = conn.execute(
                f"DELETE FROM {table} WHERE listing_id NOT IN "
                "(SELECT listing_id FROM listings)"
            )
            if cursor.rowcount:
                removed[table] = cursor.rowcount
    return removed


def _bool_or_none_to_int(value: bool | None) -> int | None:
    return None if value is None else int(value)


def upsert_visual_score(
    conn: sqlite3.Connection,
    listing_id: str,
    result: VisualScoreResult | None,
    raw_response: str | None = None,
) -> None:
    """Insert or replace a listing's visual score. Pass result=None when the
    listing has too few photos or the vision call failed -- scores are stored
    as NULL and photo_score_unavailable=True, signaling scoring.py to fall
    back to the v1 keyword-only computation. garage_attached/staging flags
    are informational only -- stored for Ben to glance at, never read by
    scoring.py."""
    with conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO visual_scores (
                listing_id, condition_photo_score, outdoor_photo_score,
                has_layout_plan, layout_plan_clarity_score, garage_attached,
                watermarked_staging_detected, suspected_unwatermarked_staging,
                staging_notes, photo_score_unavailable, raw_response, computed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                listing_id,
                result.condition_photo_score if result else None,
                result.outdoor_photo_score if result else None,
                int(result.has_layout_plan) if result else 0,
                result.layout_plan_clarity_score if result else None,
                _bool_or_none_to_int(result.garage_attached) if result else None,
                int(result.watermarked_staging_detected) if result else 0,
                int(result.suspected_unwatermarked_staging) if result else 0,
                result.staging_notes if result else None,
                int(result is None),
                raw_response,
                datetime.now(timezone.utc).isoformat(),
            ),
        )


def get_visual_score(conn: sqlite3.Connection, listing_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM visual_scores WHERE listing_id = ?", (listing_id,)
    ).fetchone()


def get_listing_ids_missing_visual_score(conn: sqlite3.Connection) -> list[str]:
    """Listings with no visual_scores row yet -- a rerun only pays the vision
    API cost for listings it hasn't already covered."""
    rows = conn.execute(
        """
        SELECT listing_id FROM listings
        WHERE listing_id NOT IN (SELECT listing_id FROM visual_scores)
        """
    ).fetchall()
    return [row["listing_id"] for row in rows]
