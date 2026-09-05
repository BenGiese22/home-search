import json
import re
import sqlite3
from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from src.backfill import dedupe_by_listing_id
from src.commute import CommuteResult
from src.models import Listing
from src.scoring import ScoreResult
# chunk_size lives with the rest of the batched-write machinery in
# turso_db. That module's import of src.db is function-local, so this is
# not a cycle.
from src.turso_db import chunk_size
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


_LISTING_COLUMNS = (
    "listing_id", "address", "city", "state", "zip_code", "price",
    "price_numeric", "beds", "baths", "sqft", "lot_sqft", "parking_spaces",
    "year_built", "description", "listing_url", "is_pinned", "property_type",
    "localized_status", "hoa_annual", "tax_annual", "sqft_above_grade",
    "sqft_below_grade", "outdoor_spaces",
)


def _listing_values(listing: Listing, is_pinned: bool) -> tuple:
    return (
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
    )


def _insert_in_chunks(conn, table: str, columns: tuple[str, ...], values: list[tuple]) -> None:
    """One multi-row INSERT OR REPLACE per chunk, sized to the table's width.

    Deliberately not executemany(): turso_serverless's executemany loops over
    its parameter sets and issues one HTTP round-trip each, so it is a
    per-row write wearing a batch-shaped API. That is what made a single
    upsert_listing cost ~65 round-trips against Turso.
    """
    if not values:
        return
    col_list = ", ".join(columns)
    one = "(" + ", ".join("?" for _ in columns) + ")"
    per_statement = chunk_size(len(columns))
    for start in range(0, len(values), per_statement):
        chunk = values[start:start + per_statement]
        flat: list[object] = []
        for row in chunk:
            flat.extend(row)
        conn.execute(
            f"INSERT OR REPLACE INTO {table} ({col_list}) VALUES "
            + ", ".join(one for _ in chunk),
            tuple(flat),
        )


def _delete_where_listing_in(conn, table: str, listing_ids: list[str]) -> None:
    """DELETE ... WHERE listing_id IN (...), chunked so a large id list never
    exceeds SQLite's bound-variable limit."""
    per_statement = chunk_size(1)
    for start in range(0, len(listing_ids), per_statement):
        chunk = listing_ids[start:start + per_statement]
        placeholders = ", ".join("?" for _ in chunk)
        conn.execute(
            f"DELETE FROM {table} WHERE listing_id IN ({placeholders})", tuple(chunk)
        )


def bulk_upsert_listings(
    conn, listings: list[Listing], pinned_ids: Collection[str] = ()
) -> None:
    """Insert or replace many listings and their children in a handful of
    statements. The set-at-a-time counterpart of upsert_listing().

    Against local SQLite the difference is invisible. Against Turso every
    statement is a ~240ms HTTP round-trip, and upsert_listing costs about 65
    of them per listing -- one for the row, one DELETE and one INSERT PER
    AMENITY, the same per photo URL, plus BEGIN/COMMIT. Across the
    129-listing corpus that is ~8,385 round-trips, roughly 33 minutes.

    Order matters: listings first, then children. Turso enforces the foreign
    keys local SQLite ignores, so writing an amenity before its listing row
    aborts the whole write.

    pinned_ids must carry the CURRENT pin status of every listing in the
    batch, for the same reason upsert_listing takes is_pinned: this is a full
    row replace, so a listing absent from pinned_ids is actively un-pinned.

    Duplicates are removed first. `listings` is INSERT OR REPLACE on a primary
    key so a repeat is harmless there, but `amenities` and `photo_urls` have
    no unique constraint -- the same listing twice would silently double both,
    with no error and no constraint violation to notice. That is not a
    hypothetical: a listing can sit in the favorites and matches tabs at once,
    and both copies arrive in one batch. Callers deduping first is not enough
    protection for a failure this quiet. First-wins, matching
    dedupe_by_listing_id, so the two dedup paths can never disagree about
    which copy survives.
    """
    if not listings:
        return
    listings = dedupe_by_listing_id(listings)
    pinned = set(pinned_ids)
    listing_ids = [listing.listing_id for listing in listings]

    with conn:
        # Parents first.
        _insert_in_chunks(
            conn, "listings", _LISTING_COLUMNS,
            [_listing_values(l, l.listing_id in pinned) for l in listings],
        )
        # Then children: clear the whole set in one statement per table
        # rather than one per listing, then insert the current set.
        _delete_where_listing_in(conn, "amenities", listing_ids)
        _insert_in_chunks(
            conn, "amenities", ("listing_id", "amenity"),
            [(l.listing_id, amenity) for l in listings for amenity in l.amenities],
        )
        _delete_where_listing_in(conn, "photo_urls", listing_ids)
        _insert_in_chunks(
            conn, "photo_urls", ("listing_id", "position", "url"),
            [
                (l.listing_id, i, url)
                for l in listings
                for i, url in enumerate(l.photo_urls)
            ],
        )


def bulk_delete_listings(conn, listing_ids: list[str]) -> None:
    """Permanently remove many listings and every child row referencing
    them, in one statement per table rather than one per table per listing.

    Child-first, derived from the schema via tables_child_first() -- Turso
    enforces the foreign keys local SQLite ignores, so deleting the parent
    listings row before its children aborts the whole delete. That exact
    ordering bug shipped twice here before the order was derived rather than
    hand-maintained.

    hosted_photos is included explicitly. It lives only in the hosted schema
    and declares no foreign key, so orphaning it fails silently: the rows
    linger, their blobs are never reclaimed, and the upload skip set goes on
    believing those photos are hosted. publish.py pruned it via its own
    PRUNABLE_TABLES, and that behaviour had to survive publish.py's deletion.

    Tables are filtered to the ones that actually exist, so the local schema
    (which has no hosted_photos) still works -- one extra read, on a path
    that runs only when listings actually drop out of the collection.
    """
    if not listing_ids:
        return
    existing = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    with conn:
        for table in tables_child_first(extra_tables=("hosted_photos",)):
            if table in existing:
                _delete_where_listing_in(conn, table, listing_ids)


def hosted_photo_index(conn) -> dict[tuple[str, int], str | None]:
    """Every hosted photo's identity in ONE statement:
    `(listing_id, position) -> source_url`.

    A NULL source_url is preserved as None rather than dropped. NULL means
    the row predates the column, i.e. identity UNKNOWN -- which must not be
    mistaken either for a real URL or for the row being absent. Callers treat
    unknown as "does not match", so the photo re-uploads rather than being
    assumed current.
    """
    return {
        (row[0], int(row[1])): row[2]
        for row in conn.execute(
            "SELECT listing_id, position, source_url FROM hosted_photos"
        )
    }


def needs_photo_work(listing: Listing, hosted_index: dict) -> bool:
    """True when any of this listing's CURRENT photo URLs is not already
    hosted under that exact URL. Pure -- the caller does the one read.

    This replaces `is_scraped()`, which asked whether a JSON file existed on
    disk. That question is unanswerable in a sandbox with no persistent
    disk, where every listing looks unscraped and the run re-downloads the
    whole corpus. The question that survives is "does the database already
    have this listing's current photos", which both execution homes can ask.

    Driven by the listing's URLs, not by the hosted rows: a hosted row at a
    position the listing no longer serves is stale, and stale rows must not
    make a listing look complete.
    """
    return any(
        hosted_index.get((listing.listing_id, position)) != url
        for position, url in enumerate(listing.photo_urls, start=1)
    )


def get_photo_urls_by_listing(conn) -> dict[str, list[str]]:
    """Every listing's photo URLs in ONE statement, in position order.

    LEFT JOIN from listings so a listing with no photo_urls rows still gets a
    key with an empty list -- callers index this dict directly, and a missing
    key would be a KeyError mid-scrape rather than "no photos". Same idiom as
    get_amenities_by_listing.
    """
    by_listing: dict[str, list[str]] = {}
    for row in conn.execute(
        """
        SELECT l.listing_id AS listing_id, p.url AS url
        FROM listings l
        LEFT JOIN photo_urls p ON p.listing_id = l.listing_id
        ORDER BY l.listing_id, p.position
        """
    ):
        urls = by_listing.setdefault(row["listing_id"], [])
        if row["url"] is not None:
            urls.append(row["url"])
    return by_listing


def listing_ids_with_any_hosted_or_no_urls(conn) -> frozenset[str]:
    """Listings that need no photo pre-fetch: either something is already
    hosted for them, or they have no photo URLs at all.

    A deliberately WEAK gate, and only for the explicit-URL loop, which has
    to decide whether to open a listing's detail page before it knows the
    listing's URLs. It cannot use needs_photo_work -- that needs the URLs
    this fetch would obtain. Absence from this set means "worth fetching",
    never "definitely incomplete"; the real decision is made afterwards.
    """
    return frozenset(
        row[0]
        for row in conn.execute(
            """
            SELECT l.listing_id
            FROM listings l
            LEFT JOIN hosted_photos h ON h.listing_id = l.listing_id
            LEFT JOIN photo_urls p ON p.listing_id = l.listing_id
            GROUP BY l.listing_id
            HAVING COUNT(h.listing_id) > 0 OR COUNT(p.listing_id) = 0
            """
        )
    )


def get_listing_ids_missing_fields(
    conn, fields: tuple[str, ...]
) -> list[str]:
    """Listings where ANY of `fields` is NULL, in ONE statement.

    Replaces needs_field_backfill(), which read each listing's stored JSON
    off disk -- one file open per listing, and nothing at all in a sandbox.

    Field names are interpolated into SQL because a column name cannot be a
    bound parameter, so they are validated against the schema's own column
    list first. That is the whole defence: anything not literally a column of
    `listings` raises before a statement is built.
    """
    valid = _parse_listing_columns()
    unknown = [f for f in fields if f not in valid]
    if unknown:
        raise ValueError(
            f"unknown listings column(s): {unknown}. "
            f"Fields are interpolated into SQL and must be real column names."
        )
    if not fields:
        return []
    where = " OR ".join(f"{f} IS NULL" for f in fields)
    return [
        row[0]
        for row in conn.execute(
            f"SELECT listing_id FROM listings WHERE {where} ORDER BY listing_id"
        )
    ]


def _parse_listing_columns() -> frozenset[str]:
    """The `listings` column names, read out of _SCHEMA rather than from a
    live connection, so validation does not cost a round-trip."""
    body = re.search(
        r"CREATE TABLE IF NOT EXISTS\s+listings\s*\((.*?)\n\s*\)", _SCHEMA, re.DOTALL
    ).group(1)
    names = set()
    for part in body.split(","):
        token = part.strip().split(None, 1)
        if token and token[0].upper() not in {
            "PRIMARY", "FOREIGN", "UNIQUE", "CHECK", "CONSTRAINT"
        }:
            names.add(token[0])
    return frozenset(names)


def listings_from_rows(
    rows,
    amenities_by_id: dict[str, list[str]],
    photo_urls_by_id: dict[str, list[str]],
) -> list[Listing]:
    """Rebuild Listing objects from a listings SELECT plus two set-at-a-time
    child reads. Pure: makes no queries of its own.

    Moved from score.py's _row_to_listing, which left photo_urls empty
    because scoring never needed them. scrape.py's gates do, so this fills
    them -- along with property_type and localized_status, which the scoring
    version also dropped.
    """
    return [
        Listing(
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
            amenities=amenities_by_id.get(row["listing_id"], []),
            photo_urls=photo_urls_by_id.get(row["listing_id"], []),
            listing_url=row["listing_url"],
            property_type=row["property_type"] or "",
            localized_status=row["localized_status"] or "",
            hoa_annual=row["hoa_annual"],
            tax_annual=row["tax_annual"],
            sqft_above_grade=row["sqft_above_grade"],
            sqft_below_grade=row["sqft_below_grade"],
            outdoor_spaces=json.loads(row["outdoor_spaces"] or "[]"),
        )
        for row in rows
    ]


# The vision checkpoint. It lives in the database rather than a file because
# both execution homes share one database and a file is invisible across
# them: a desktop run that submits a batch and then has its lid closed leaves
# its checkpoint on local disk, and a cloud run sees listings with no
# visual_scores row, no checkpoint it can read, and resubmits. That is real
# money at the vision API, and it will happen once both homes are live.
#
# Deliberately NOT keyed on listing_id. A batch spans many listings, so one
# being delisted must not delete the record of an in-flight batch the others
# are still waiting on -- which is also why it is invisible to
# tables_child_first and to delete_orphaned_rows.


def load_vision_batches(conn) -> list[dict]:
    """Every in-flight batch, in ONE statement.

    garage_expected_by_id is stored per batch and read back as-is, never
    recomputed from the database's current state: the listing set that
    produced a batch is the only thing that can interpret its results, and
    that set has already changed by the time the results arrive.
    """
    return [
        {
            "batch_id": row["batch_id"],
            "garage_expected_by_id": json.loads(row["garage_expected_by_id"]),
            "submitted_at": row["submitted_at"],
            "submitted_by": row["submitted_by"],
        }
        for row in conn.execute(
            "SELECT batch_id, garage_expected_by_id, submitted_at, submitted_by "
            "FROM vision_batches ORDER BY submitted_at, batch_id"
        )
    ]


def record_vision_batch(
    conn, batch_id: str, garage_expected_by_id: dict[str, bool], submitted_by: str
) -> None:
    """Record a submitted batch, in ONE statement.

    Called the instant the batch is created, so the window in which money is
    spent but nothing remembers it is as close to zero as it can be.
    """
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO vision_batches "
            "(batch_id, garage_expected_by_id, submitted_at, submitted_by) "
            "VALUES (?, ?, ?, ?)",
            (
                batch_id,
                json.dumps(garage_expected_by_id),
                datetime.now(timezone.utc).isoformat(),
                submitted_by,
            ),
        )


def clear_vision_batch(conn, batch_id: str) -> None:
    """Forget one consumed batch, in ONE statement.

    Per batch, at the moment its results are processed -- not all of them at
    the end of the run. A crash between two batches must not make the first
    look unprocessed and get resubmitted.
    """
    with conn:
        conn.execute("DELETE FROM vision_batches WHERE batch_id = ?", (batch_id,))


# The cross-home run lock.
#
# pipeline.py's fcntl.flock is per-machine. It was the whole answer while one
# desktop was the only execution home; it sees nothing of a Vercel Sandbox run
# and a sandbox sees nothing of it. The two are complementary and both stay:
# flock is instant and free for the same-machine case, this covers the case
# flock structurally cannot.
#
# It is a LEASE, not a lock, because there is no reliable "runner died"
# signal across homes -- a machine powered off or booted into its other OS, and a sandbox reaped at its 3h
# limit both leave the row behind, and a plain lock would then block the
# other home forever.
#
# Every operation below is exactly one statement. Acquire is a single
# conditional upsert whose atomicity is the statement's own: SQLite applies
# it as one unit, so of two homes issuing it concurrently one lands first and
# the other's ON CONFLICT branch sees the lease it lost to. A read-then-write
# ("is it free? then take it") would not be a lock at all -- both homes can
# read "free" before either writes -- and BEGIN IMMEDIATE is not available to
# fix that here: turso_serverless emits BEGIN <isolation_level> with
# isolation_level defaulting to DEFERRED, which takes no write lock until its
# first write. tests/test_pipeline_lock.py pins that driver behaviour so a
# future default of IMMEDIATE fails loudly rather than being silently relied
# on.

PIPELINE_LOCK_NAME = "pipeline"

# How long a lease is good for without renewal.
#
# Bounded below by the longest single stage: pipeline.py renews between
# stages, but nothing renews *during* one, and score_photos.py polls a vision
# batch for hours. A lease shorter than that expires mid-run and lets the
# other home in, which is the failure this exists to prevent, arriving late.
#
# Bounded above by how long a crashed runner may block the other home. The
# pipeline already tolerates six hours of staleness (pipeline.py's
# DEFAULT_MAX_AGE_HOURS), so an abandoned lease costs at most one skipped
# trigger before the next one gets in.
#
# Six hours is where those two bounds meet.
DEFAULT_LEASE_SECONDS = 6 * 3600


@dataclass(frozen=True)
class Lease:
    """The lease in force after an acquire attempt -- ours if we won, the
    other home's if we lost, which is why `mine` is a field and not the
    return value: the loser needs the holder's identity to say anything
    useful before exiting."""

    token: str
    held_by: str
    acquired_at: str
    expires_at: str
    mine: bool


# `datetime('now')` rather than a bound timestamp, everywhere. The two homes
# are different machines; if each stamped and compared the lease from its own
# clock, a desktop running a few minutes fast would read a live sandbox lease
# as expired and take it. Inside the statement, both homes are measured
# against the one clock they share. SQLite fixes 'now' for the duration of a
# single statement, so the three uses below cannot disagree with each other.
_LEASE_EXPIRED = "pipeline_lock.expires_at <= datetime('now')"

# The conditional upsert. Insert when no row exists; on conflict, overwrite
# each column only if the stored lease has expired, and otherwise leave it
# exactly as it was. RETURNING then reports the resulting row either way, so
# a loser learns who holds the lease from the same round-trip rather than a
# follow-up SELECT.
_ACQUIRE_SQL = f"""
INSERT INTO pipeline_lock (lock_name, lease_token, held_by, acquired_at, expires_at)
VALUES (?, ?, ?, datetime('now'), datetime('now', ?))
ON CONFLICT(lock_name) DO UPDATE SET
    lease_token = CASE WHEN {_LEASE_EXPIRED}
                       THEN excluded.lease_token ELSE pipeline_lock.lease_token END,
    held_by     = CASE WHEN {_LEASE_EXPIRED}
                       THEN excluded.held_by     ELSE pipeline_lock.held_by END,
    acquired_at = CASE WHEN {_LEASE_EXPIRED}
                       THEN excluded.acquired_at ELSE pipeline_lock.acquired_at END,
    expires_at  = CASE WHEN {_LEASE_EXPIRED}
                       THEN excluded.expires_at  ELSE pipeline_lock.expires_at END
RETURNING lease_token, held_by, acquired_at, expires_at
"""


def acquire_pipeline_lease(
    conn,
    held_by: str,
    token: str,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> Lease:
    """Try to take the cross-home run lease, in ONE statement.

    `held_by` is the execution home (a hostname or HOME_SEARCH_HOME) and is
    only ever read by a human. `token` identifies this RUN and is the thing
    ownership is decided on -- a second process on the same home is still a
    second run, and keying on the home name would let a restarted desktop run
    steal its own predecessor's lease.

    Returns the lease now in force. `lease.mine` is the answer; when it is
    False, the rest of the fields describe the run that beat us.
    """
    row = conn.execute(
        _ACQUIRE_SQL, (PIPELINE_LOCK_NAME, token, held_by, f"+{int(lease_seconds)} seconds")
    ).fetchone()
    # Read the winner back out of the row rather than trusting rowcount:
    # turso_serverless reports rowcount as -1 for any statement that returns
    # columns, and what it would report for an upsert that updated nothing is
    # not something this project has verified against the server. A returned
    # token is positive evidence and means the same thing on both drivers.
    if hasattr(conn, "commit"):
        conn.commit()
    return Lease(
        token=row["lease_token"],
        held_by=row["held_by"],
        acquired_at=row["acquired_at"],
        expires_at=row["expires_at"],
        mine=row["lease_token"] == token,
    )


def renew_pipeline_lease(
    conn, token: str, lease_seconds: int = DEFAULT_LEASE_SECONDS
) -> bool:
    """Push our own lease's expiry out, in ONE statement.

    Called between stages, so a full run may outlast a single lease without
    the lock going stale under it. Guarded on the token, so it can only ever
    extend the lease it was given -- never adopt one that expired and was
    taken by the other home in the meantime.

    False means we no longer hold it. That is worth shouting about but is not
    itself grounds to abort: by then the other home is already running, and
    stopping half way through leaves the data in a worse state than finishing.
    """
    cursor = conn.execute(
        "UPDATE pipeline_lock SET expires_at = datetime('now', ?) "
        "WHERE lock_name = ? AND lease_token = ? "
        "RETURNING lease_token",
        (f"+{int(lease_seconds)} seconds", PIPELINE_LOCK_NAME, token),
    )
    renewed = cursor.fetchone() is not None
    if hasattr(conn, "commit"):
        conn.commit()
    return renewed


def release_pipeline_lease(conn, token: str) -> bool:
    """Give the lease up, in ONE statement.

    Idempotent, because it runs from a finally block that a crash path can
    reach after the lease is already gone -- releasing nothing returns False
    rather than raising.

    The token guard is the load-bearing part. A run whose lease expired and
    was taken over by the other home must not delete the NEW holder's row on
    its way out; that would hand a third trigger the lock while two runs are
    still live.
    """
    cursor = conn.execute(
        "DELETE FROM pipeline_lock WHERE lock_name = ? AND lease_token = ? "
        "RETURNING lease_token",
        (PIPELINE_LOCK_NAME, token),
    )
    released = cursor.fetchone() is not None
    if hasattr(conn, "commit"):
        conn.commit()
    return released


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


def get_commutes_by_listing(conn: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    """Every listing's commute row in ONE statement, keyed by listing_id.

    The per-listing get_commute() is correct but costs a statement each, and
    a statement against Turso is a ~240ms HTTP round-trip -- 129 listings is
    half a minute of latency for data that is one SELECT. Listings with no
    commute row are simply absent, so callers use .get() and fall back
    exactly as they did when get_commute() returned None."""
    return {
        row["listing_id"]: row
        for row in conn.execute("SELECT * FROM commute")
    }


def get_listing_ids_missing_commute(
    conn: sqlite3.Connection, retry_failed: bool = True, force: bool = False
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
    if force:
        # Every listing, whether or not it already has a usable row.
        #
        # Needed because nothing else invalidates a commute row: there is no
        # TTL and no source check, so a complete row computed under an old
        # provider is never re-selected and would keep its stale duration
        # forever. Changing how commutes are measured is exactly the case
        # this exists for.
        return [
            row["listing_id"]
            for row in conn.execute(
                "SELECT listing_id FROM listings ORDER BY listing_id"
            ).fetchall()
        ]

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


# Rows per batched INSERT. scores has 12 columns, so 30 rows is 360 bound
# variables -- well inside SQLite's 999-variable default, with room to spare
# if the table grows. Mirrors src.turso_db.BATCH_CHUNK and exists for the
# same reason: one statement per row is a ~240ms round-trip per row once the
# connection is Turso rather than a local file.
SCORE_BATCH_CHUNK = 30

_SCORE_COLUMNS = (
    "listing_id", "commute_score", "sqft_score", "condition_score",
    "outdoor_score", "room_count_score", "parking_score", "hoa_score",
    "composite", "passes_filters", "has_incomplete_data", "computed_at",
)


def upsert_scores(
    conn: sqlite3.Connection, results: list[tuple[str, ScoreResult]]
) -> None:
    """Insert or replace many score rows in a handful of statements.

    Same semantics as calling upsert_score() per row, but score.py rewrites
    every scores row on every run -- one statement per listing is exactly the
    per-row pattern that made a full Turso sync take 22 minutes. Chunked
    multi-row INSERT OR REPLACE keeps the statement count flat as the corpus
    grows."""
    if not results:
        return
    computed_at = datetime.now(timezone.utc).isoformat()
    col_list = ", ".join(_SCORE_COLUMNS)
    one = "(" + ", ".join("?" for _ in _SCORE_COLUMNS) + ")"
    with conn:
        for start in range(0, len(results), SCORE_BATCH_CHUNK):
            chunk = results[start:start + SCORE_BATCH_CHUNK]
            values: list[object] = []
            for listing_id, result in chunk:
                values.extend((
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
                    computed_at,
                ))
            conn.execute(
                f"INSERT OR REPLACE INTO scores ({col_list}) VALUES "
                + ", ".join(one for _ in chunk),
                tuple(values),
            )


def get_scores(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM scores ORDER BY composite DESC").fetchall()


def get_amenities(conn: sqlite3.Connection, listing_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT amenity FROM amenities WHERE listing_id = ? ORDER BY amenity", (listing_id,)
    ).fetchall()
    return [row["amenity"] for row in rows]


def get_amenities_by_listing(conn: sqlite3.Connection) -> dict[str, list[str]]:
    """Every listing's amenities in ONE statement, keyed by listing_id.

    LEFT JOIN from listings rather than grouping amenities, so a listing with
    no amenity rows still gets a key with an empty list -- callers index this
    dict directly and a missing key would be a KeyError mid-scoring rather
    than the empty list the per-listing get_amenities() returned."""
    by_listing: dict[str, list[str]] = {}
    for row in conn.execute(
        """
        SELECT l.listing_id AS listing_id, a.amenity AS amenity
        FROM listings l
        LEFT JOIN amenities a ON a.listing_id = l.listing_id
        ORDER BY l.listing_id, a.amenity
        """
    ):
        amenities = by_listing.setdefault(row["listing_id"], [])
        if row["amenity"] is not None:
            amenities.append(row["amenity"])
    return by_listing


def delete_listing(conn: sqlite3.Connection, listing_id: str) -> None:
    """Permanently removes a listing and every child row referencing it
    (amenities, photo_urls, commute, scores, visual_scores). Used when a
    listing drops out of the live Compass collection — delisted listings are
    hard-deleted, not archived, since they're never expected to be referenced
    again. Safe to call for a listing_id that doesn't exist.

    Keep this list in step with _SCHEMA: visual_scores was added to the schema
    long after this function and went unnoticed here for months, orphaning one
    row per delisting.

    hosted_photos is included for the same reason bulk_delete_listings
    includes it, and the omission here was the same bug a second time. This
    is the fallback the batched delete drops to when it fails, so it runs
    precisely when something has already gone wrong -- and it was doing a
    quietly less complete delete than the path it stands in for. It declares
    no foreign key and lives only in the hosted schema, so orphaning it fails
    silently: the rows linger, their blobs are never reclaimed, and
    collect_pending_photos goes on believing those photos are hosted.

    Filtered to the tables that actually exist, so the local schema (which
    has no hosted_photos) still works."""
    existing = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    with conn:
        # Child-first, derived from the schema rather than hand-listed --
        # see tables_child_first().
        for table in tables_child_first(extra_tables=("hosted_photos",)):
            if table in existing:
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


def get_visual_scores_by_listing(conn: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    """Every listing's visual_scores row in ONE statement, keyed by
    listing_id. Listings never scored are absent, matching what the
    per-listing get_visual_score() signalled by returning None."""
    return {
        row["listing_id"]: row
        for row in conn.execute("SELECT * FROM visual_scores")
    }


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
