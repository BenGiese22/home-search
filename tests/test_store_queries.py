"""Set-based queries that replace the local JSON store.

`src/store.py` gates the scrape on files existing on disk. That works on a
laptop and fails completely in a sandbox with no persistent disk: every
listing looks unscraped, so the run re-downloads ~700 MB from Compass and
rebuilds the CSV and gallery from a near-empty store — a real gate-4 sandbox
run produced a 2-listing gallery from a 100-listing corpus.

These are the Turso-derived replacements. Every one of them is ONE
statement, asserted, because a per-listing form is a ~240ms HTTP round-trip
each: 100 listings would be 24 seconds to answer a question one query
answers.
"""
import sqlite3

import pytest

from src.db import (
    get_listing_ids_missing_fields,
    get_photo_urls_by_listing,
    hosted_photo_index,
    listing_ids_with_any_hosted_or_no_urls,
    listings_from_rows,
    needs_photo_work,
    query_listings,
    upsert_listing,
)
from src.models import Listing
from src.turso_db import ensure_schema


def _conn() -> sqlite3.Connection:
    # ensure_schema, not _SCHEMA: hosted_photos lives only in the hosted
    # schema and is what most of these queries read.
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    return conn


def _listing(n: int, photos: int = 3) -> Listing:
    return Listing(
        listing_id=f"L{n:04d}", address=f"{n} Test St", city="Arvada", state="CO",
        zip_code="80003", price="$600,000", beds=3, baths=2.0, sqft=2000,
        lot_sqft=7000, parking_spaces=2, year_built=2000, description="d",
        amenities=["Garage"], photo_urls=[f"https://img/{n}/{i}.jpg" for i in range(photos)],
        listing_url=f"https://example.com/{n}",
        property_type="Single Family", localized_status="Active",
    )


class _Counted:
    """Counts SQL statements, the unit that maps 1:1 to a Turso round-trip."""

    def __init__(self, conn):
        self.conn = conn
        self.statements = []

    def __enter__(self):
        self.conn.set_trace_callback(self.statements.append)
        return self.statements

    def __exit__(self, *exc):
        self.conn.set_trace_callback(None)
        return False


def _host(conn, listing_id, position, source_url, blob="https://blob/x.jpg"):
    conn.execute(
        "INSERT OR REPLACE INTO hosted_photos (listing_id, position, source_url, blob_url) "
        "VALUES (?, ?, ?, ?)", (listing_id, position, source_url, blob))
    conn.commit()


# --- hosted_photo_index --------------------------------------------------

def test_hosted_photo_index_maps_position_to_source_url():
    conn = _conn()
    upsert_listing(conn, _listing(1))
    _host(conn, "L0001", 1, "https://img/1/0.jpg")

    assert hosted_photo_index(conn) == {("L0001", 1): "https://img/1/0.jpg"}


def test_hosted_photo_index_is_one_statement_whatever_the_size():
    conn = _conn()
    for n in range(40):
        upsert_listing(conn, _listing(n))
        for pos in range(1, 6):
            _host(conn, f"L{n:04d}", pos, f"https://img/{n}/{pos-1}.jpg")

    with _Counted(conn) as statements:
        index = hosted_photo_index(conn)

    assert len(index) == 200
    assert len([s for s in statements if s.strip().upper().startswith("SELECT")]) == 1


def test_a_null_source_url_is_preserved_as_unknown():
    """NULL predates the column: identity unknown, which must not be
    mistaken for 'matches nothing' or for any real URL."""
    conn = _conn()
    upsert_listing(conn, _listing(1))
    conn.execute("INSERT INTO hosted_photos (listing_id, position, source_url, blob_url) "
                 "VALUES ('L0001', 1, NULL, 'https://blob/x.jpg')")
    conn.commit()

    assert hosted_photo_index(conn)[("L0001", 1)] is None


# --- needs_photo_work (pure) ---------------------------------------------

def test_a_listing_with_no_urls_needs_no_photo_work():
    assert needs_photo_work(_listing(1, photos=0), {}) is False


def test_a_listing_with_urls_but_nothing_hosted_needs_work():
    assert needs_photo_work(_listing(1, photos=3), {}) is True


def test_a_fully_hosted_listing_needs_no_work():
    listing = _listing(1, photos=3)
    index = {("L0001", i + 1): url for i, url in enumerate(listing.photo_urls)}

    assert needs_photo_work(listing, index) is False


def test_a_changed_url_at_one_position_needs_work():
    """The relist case. Position 2 now serves a different photo, so the
    hosted row is stale even though every position is present."""
    listing = _listing(1, photos=3)
    index = {("L0001", i + 1): url for i, url in enumerate(listing.photo_urls)}
    index[("L0001", 2)] = "https://img/OLD-LISTING/1.jpg"

    assert needs_photo_work(listing, index) is True


def test_a_null_hosted_identity_counts_as_needing_work():
    listing = _listing(1, photos=1)

    assert needs_photo_work(listing, {("L0001", 1): None}) is True


def test_a_partially_hosted_listing_needs_work():
    listing = _listing(1, photos=3)
    index = {("L0001", 1): listing.photo_urls[0]}

    assert needs_photo_work(listing, index) is True


# --- get_photo_urls_by_listing -------------------------------------------

def test_photo_urls_come_back_in_position_order():
    conn = _conn()
    upsert_listing(conn, _listing(1, photos=4))

    assert get_photo_urls_by_listing(conn)["L0001"] == _listing(1, photos=4).photo_urls


def test_a_listing_with_no_photos_gets_an_empty_list_not_a_missing_key():
    """Callers index this dict directly; a missing key would be a KeyError
    mid-scrape rather than 'this listing has no photos'."""
    conn = _conn()
    upsert_listing(conn, _listing(1, photos=0))

    assert get_photo_urls_by_listing(conn) == {"L0001": []}


def test_photo_urls_by_listing_is_one_statement():
    conn = _conn()
    for n in range(30):
        upsert_listing(conn, _listing(n, photos=5))

    with _Counted(conn) as statements:
        urls = get_photo_urls_by_listing(conn)

    assert sum(len(v) for v in urls.values()) == 150
    assert len([s for s in statements if s.strip().upper().startswith("SELECT")]) == 1


# --- get_listing_ids_missing_fields --------------------------------------

def test_missing_fields_finds_listings_with_a_null_column():
    conn = _conn()
    upsert_listing(conn, _listing(1))
    upsert_listing(conn, _listing(2))
    conn.execute("UPDATE listings SET hoa_annual = NULL WHERE listing_id = 'L0001'")
    conn.execute("UPDATE listings SET hoa_annual = 100 WHERE listing_id = 'L0002'")
    conn.commit()

    assert get_listing_ids_missing_fields(conn, ("hoa_annual",)) == ["L0001"]


def test_missing_fields_is_an_or_across_fields():
    """`--backfill-missing` wants any listing missing ANY of the fields."""
    conn = _conn()
    upsert_listing(conn, _listing(1))
    upsert_listing(conn, _listing(2))
    conn.execute("UPDATE listings SET hoa_annual = 1, tax_annual = NULL WHERE listing_id='L0001'")
    conn.execute("UPDATE listings SET hoa_annual = NULL, tax_annual = 1 WHERE listing_id='L0002'")
    conn.commit()

    assert set(get_listing_ids_missing_fields(conn, ("hoa_annual", "tax_annual"))) == {"L0001", "L0002"}


def test_missing_fields_is_one_statement():
    conn = _conn()
    for n in range(25):
        upsert_listing(conn, _listing(n))

    with _Counted(conn) as statements:
        get_listing_ids_missing_fields(conn, ("hoa_annual", "tax_annual"))

    assert len([s for s in statements if s.strip().upper().startswith("SELECT")]) == 1


def test_an_unknown_field_name_is_rejected():
    """Field names are interpolated into SQL, so they are validated against
    the schema's own column list rather than trusted."""
    conn = _conn()
    for bad in ("hoa_annual; DROP TABLE listings", "nonsense", "1=1"):
        with pytest.raises(ValueError):
            get_listing_ids_missing_fields(conn, (bad,))


def test_the_injection_attempt_left_the_table_intact():
    conn = _conn()
    upsert_listing(conn, _listing(1))
    with pytest.raises(ValueError):
        get_listing_ids_missing_fields(conn, ("hoa_annual; DROP TABLE listings",))

    assert len(query_listings(conn)) == 1


# --- listings_from_rows --------------------------------------------------

def test_listings_from_rows_rebuilds_a_listing_including_photo_urls():
    """score.py's _row_to_listing left photo_urls empty because nothing
    needed them. scrape.py's gates do."""
    conn = _conn()
    original = _listing(1, photos=3)
    upsert_listing(conn, original)

    rows = query_listings(conn)
    listings = listings_from_rows(rows, {"L0001": ["Garage"]}, get_photo_urls_by_listing(conn))

    assert len(listings) == 1
    got = listings[0]
    assert got.photo_urls == original.photo_urls
    assert got.property_type == "Single Family"
    assert got.localized_status == "Active"
    assert got.amenities == ["Garage"]
    assert got.address == original.address


def test_listings_from_rows_tolerates_a_listing_absent_from_either_map():
    conn = _conn()
    upsert_listing(conn, _listing(1))

    listings = listings_from_rows(query_listings(conn), {}, {})

    assert listings[0].amenities == []
    assert listings[0].photo_urls == []


def test_listings_from_rows_makes_no_queries():
    """It is pure: the caller has already done the reads."""
    conn = _conn()
    upsert_listing(conn, _listing(1))
    rows = query_listings(conn)

    with _Counted(conn) as statements:
        listings_from_rows(rows, {}, {})

    assert statements == []


# --- the weak pre-fetch gate ---------------------------------------------

def test_prefetch_gate_covers_hosted_listings_and_url_less_ones():
    conn = _conn()
    upsert_listing(conn, _listing(1, photos=2))   # urls, nothing hosted -> absent
    upsert_listing(conn, _listing(2, photos=0))   # no urls -> present
    upsert_listing(conn, _listing(3, photos=2))
    _host(conn, "L0003", 1, "https://img/3/0.jpg")  # hosted -> present

    gate = listing_ids_with_any_hosted_or_no_urls(conn)

    assert "L0002" in gate and "L0003" in gate
    assert "L0001" not in gate


def test_prefetch_gate_is_one_statement():
    conn = _conn()
    for n in range(30):
        upsert_listing(conn, _listing(n, photos=2))

    with _Counted(conn) as statements:
        listing_ids_with_any_hosted_or_no_urls(conn)

    assert len([s for s in statements if s.strip().upper().startswith("SELECT")]) == 1
