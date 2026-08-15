import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from src.commute import CommuteResult
from src.models import Listing
from src.scoring import ScoreResult

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
    listing_url TEXT NOT NULL
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
    parking_score REAL NOT NULL,
    composite REAL NOT NULL,
    passes_filters INTEGER NOT NULL,
    computed_at TEXT NOT NULL
);
"""

_PRICE_RE = re.compile(r"[\d.]+")


def parse_price(price: str) -> float | None:
    """Best-effort numeric price for range queries, e.g. "$650,000" -> 650000.0.
    Returns None for anything that doesn't contain a number (e.g. "Contact agent")
    rather than raising, since price is display text, not a guaranteed number."""
    match = _PRICE_RE.search(price.replace(",", ""))
    return float(match.group()) if match else None


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    conn.commit()


def get_connection(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def upsert_listing(conn: sqlite3.Connection, listing: Listing) -> None:
    """Insert or fully replace a listing's row and its amenities/photo_urls.
    Safe to call repeatedly for the same listing_id — re-scraping a listing
    should reflect its current state, not accumulate stale child rows."""
    with conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO listings (
                listing_id, address, city, state, zip_code,
                price, price_numeric, beds, baths, sqft, lot_sqft,
                parking_spaces, year_built, description, listing_url
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
