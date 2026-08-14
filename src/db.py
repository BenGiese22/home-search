import re
import sqlite3
from pathlib import Path

from src.models import Listing

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
"""

_PRICE_RE = re.compile(r"[\d.]+")


def _parse_price(price: str) -> float | None:
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
                _parse_price(listing.price),
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
