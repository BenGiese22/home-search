"""Set-at-a-time writes for the scrape stage.

`upsert_listing` writes one listing per call, and inside each call it uses
`executemany` for amenities and photo_urls. Against local SQLite that is one
fast statement. Against Turso it is emphatically not: turso_serverless's
executemany loops and issues one HTTP round-trip per parameter set, so a
listing with 24 amenities and 36 photos costs ~65 round-trips and the
129-listing corpus costs ~8,385 -- about 33 minutes.

These tests pin the batched shape: the statement count must stay flat as
listings, amenities and photos grow.
"""
import sqlite3

import pytest

from src.db import (
    _SCHEMA,
    bulk_delete_listings,
    bulk_upsert_listings,
    get_amenities_by_listing,
    get_connection,
    query_listings,
    tables_child_first,
    upsert_listing,
)
from src.models import Listing
from src.turso_db import MAX_SQL_VARIABLES, chunk_size


def _listing(n: int, amenities: int = 3, photos: int = 4) -> Listing:
    return Listing(
        listing_id=f"L{n:04d}",
        address=f"{n} Test St",
        city="Arvada",
        state="CO",
        zip_code="80003",
        price="$600,000",
        beds=3,
        baths=2.0,
        sqft=2000,
        lot_sqft=7000,
        parking_spaces=2,
        year_built=2000,
        description="desc",
        amenities=[f"Amenity {n}-{i}" for i in range(amenities)],
        photo_urls=[f"https://example.com/{n}/{i}.jpg" for i in range(photos)],
        listing_url=f"https://example.com/{n}",
    )


def _fk_conn() -> sqlite3.Connection:
    """Foreign keys ON, which is how Turso behaves and how bare sqlite3 does
    not -- a child written before its parent must fail here."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


class _Counter:
    def __init__(self, conn):
        self.conn = conn
        self.statements = []

    def __enter__(self):
        self.conn.set_trace_callback(self.statements.append)
        return self.statements

    def __exit__(self, *exc):
        self.conn.set_trace_callback(None)
        return False


# --- correctness ----------------------------------------------------------

def test_bulk_upsert_writes_every_listing_and_its_children(tmp_path):
    conn = get_connection(tmp_path / "db.sqlite")
    listings = [_listing(n) for n in range(5)]

    bulk_upsert_listings(conn, listings)

    assert len(query_listings(conn)) == 5
    amenities = get_amenities_by_listing(conn)
    assert amenities["L0002"] == ["Amenity 2-0", "Amenity 2-1", "Amenity 2-2"]
    photos = conn.execute(
        "SELECT COUNT(*) FROM photo_urls WHERE listing_id = 'L0002'"
    ).fetchone()[0]
    assert photos == 4


def test_bulk_upsert_matches_upsert_listing_field_for_field(tmp_path):
    """The batched path must be a drop-in for the per-listing one."""
    listing = _listing(1)

    single = get_connection(tmp_path / "single.sqlite")
    upsert_listing(single, listing)
    expected = dict(query_listings(single)[0])

    bulk = get_connection(tmp_path / "bulk.sqlite")
    bulk_upsert_listings(bulk, [listing])
    actual = dict(query_listings(bulk)[0])

    assert actual == expected


def test_bulk_upsert_is_idempotent_and_does_not_accumulate_children(tmp_path):
    conn = get_connection(tmp_path / "db.sqlite")
    listings = [_listing(n) for n in range(3)]

    bulk_upsert_listings(conn, listings)
    bulk_upsert_listings(conn, listings)

    assert len(query_listings(conn)) == 3
    assert conn.execute("SELECT COUNT(*) FROM amenities").fetchone()[0] == 9
    assert conn.execute("SELECT COUNT(*) FROM photo_urls").fetchone()[0] == 12


def test_re_upserting_a_listing_replaces_rather_than_appends_children(tmp_path):
    conn = get_connection(tmp_path / "db.sqlite")
    bulk_upsert_listings(conn, [_listing(1, amenities=5)])

    shrunk = _listing(1, amenities=1)
    bulk_upsert_listings(conn, [shrunk])

    assert get_amenities_by_listing(conn)["L0001"] == ["Amenity 1-0"]


def test_pin_status_is_preserved_per_listing(tmp_path):
    """upsert_listing fully replaces the row, so pin status must be passed
    per listing on every write or a pinned listing is silently un-pinned."""
    conn = get_connection(tmp_path / "db.sqlite")
    listings = [_listing(1), _listing(2)]

    bulk_upsert_listings(conn, listings, pinned_ids={"L0002"})

    pinned = {
        row["listing_id"]: row["is_pinned"]
        for row in conn.execute("SELECT listing_id, is_pinned FROM listings")
    }
    assert pinned == {"L0001": 0, "L0002": 1}


def test_an_empty_batch_is_a_clean_no_op(tmp_path):
    conn = get_connection(tmp_path / "db.sqlite")

    with _Counter(conn) as statements:
        bulk_upsert_listings(conn, [])

    assert statements == []


# --- foreign keys ---------------------------------------------------------

def test_listings_are_written_before_their_children(tmp_path):
    """Turso enforces the foreign keys local SQLite ignores. Writing an
    amenity before its listing row aborts the whole write."""
    conn = _fk_conn()

    bulk_upsert_listings(conn, [_listing(n) for n in range(4)])

    assert conn.execute("SELECT COUNT(*) FROM amenities").fetchone()[0] == 12


# --- the round-trip guarantee --------------------------------------------

def test_no_executemany_anywhere_in_the_bulk_write_path(tmp_path):
    """The specific landmine. turso_serverless.Cursor.executemany loops over
    its parameter sets and issues one round-trip each -- it is a per-row
    write wearing a batch-shaped API."""
    conn = get_connection(tmp_path / "db.sqlite")
    calls = []

    class Guard:
        def execute(self, sql, *a, **k):
            return conn.execute(sql, *a, **k)

        def executemany(self, *a, **k):
            calls.append(a)
            raise AssertionError("executemany is one round-trip per row on Turso")

        def commit(self, *a, **k):
            return conn.commit()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            conn.commit()
            return False

    bulk_upsert_listings(Guard(), [_listing(n) for n in range(5)])

    assert calls == []


def test_statement_count_does_not_grow_per_listing(tmp_path):
    """The regression guard. Ten listings and a hundred listings must cost
    a similar, small number of statements -- not one per listing, and
    emphatically not one per amenity or photo."""
    counts = {}
    for size in (10, 100):
        conn = get_connection(tmp_path / f"db-{size}.sqlite")
        listings = [_listing(n) for n in range(size)]
        with _Counter(conn) as statements:
            bulk_upsert_listings(conn, listings)
        counts[size] = len([s for s in statements if s.strip().upper().startswith(
            ("INSERT", "DELETE")
        )])

    # 10x the listings must not cost anywhere near 10x the statements.
    assert counts[100] < counts[10] * 3, counts
    # And the absolute number stays small enough to be seconds, not minutes.
    assert counts[100] < 40, counts


def test_a_full_corpus_sized_batch_stays_far_under_the_old_cost(tmp_path):
    """129 listings with realistic child counts. The per-listing path cost
    ~8,385 round-trips (~33 min at 240ms). This must be orders less."""
    conn = get_connection(tmp_path / "db.sqlite")
    listings = [_listing(n, amenities=24, photos=36) for n in range(129)]

    with _Counter(conn) as statements:
        bulk_upsert_listings(conn, listings)

    writes = [s for s in statements if s.strip().upper().startswith(("INSERT", "DELETE"))]
    assert len(writes) < 100, (
        f"{len(writes)} statements = {len(writes) * 0.24:.0f}s against Turso"
    )


# --- chunk sizing ---------------------------------------------------------

def test_chunk_size_never_exceeds_sqlites_variable_limit():
    """CHUNK x columns must stay under 999 or every write fails at once.
    This bit before: listings reached 23 columns and a fixed chunk of 50
    would have been 1,150 variables."""
    for columns in range(1, 60):
        assert chunk_size(columns) * columns <= MAX_SQL_VARIABLES, columns
        assert chunk_size(columns) >= 1


def test_narrow_tables_get_more_rows_per_statement_than_wide_ones():
    """amenities has 2 columns and photo_urls 3; listings has 23. Sizing the
    chunk off the widest table would leave the narrow ones issuing many
    times more statements than they need."""
    assert chunk_size(2) > chunk_size(23)


# --- delisting ------------------------------------------------------------

def test_bulk_delete_removes_listings_and_every_child_row(tmp_path):
    conn = get_connection(tmp_path / "db.sqlite")
    bulk_upsert_listings(conn, [_listing(n) for n in range(4)])

    bulk_delete_listings(conn, ["L0001", "L0003"])

    remaining = {row["listing_id"] for row in query_listings(conn)}
    assert remaining == {"L0000", "L0002"}
    assert conn.execute(
        "SELECT COUNT(*) FROM amenities WHERE listing_id IN ('L0001','L0003')"
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM photo_urls WHERE listing_id IN ('L0001','L0003')"
    ).fetchone()[0] == 0


def test_bulk_delete_prunes_children_before_the_parent_row():
    """Deleting the listings row first aborts the whole delete under the
    foreign keys Turso enforces. This reproduces that by turning them on."""
    conn = _fk_conn()
    bulk_upsert_listings(conn, [_listing(n) for n in range(3)])

    bulk_delete_listings(conn, ["L0001"])  # must not raise

    assert conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0] == 2


def test_bulk_delete_uses_the_schema_derived_child_first_order(tmp_path):
    conn = get_connection(tmp_path / "db.sqlite")
    bulk_upsert_listings(conn, [_listing(1)])

    with _Counter(conn) as statements:
        bulk_delete_listings(conn, ["L0001"])

    deletes = [s for s in statements if s.strip().upper().startswith("DELETE")]
    order = [t for t in tables_child_first() if any(f" {t} " in s for s in deletes)]
    assert order.index("listings") == len(order) - 1, order


def test_bulk_delete_costs_a_bounded_number_of_statements(tmp_path):
    """One statement per table, not one per table per listing."""
    conn = get_connection(tmp_path / "db.sqlite")
    bulk_upsert_listings(conn, [_listing(n) for n in range(50)])

    with _Counter(conn) as statements:
        bulk_delete_listings(conn, [f"L{n:04d}" for n in range(50)])

    deletes = [s for s in statements if s.strip().upper().startswith("DELETE")]
    assert len(deletes) <= len(tables_child_first()), len(deletes)


def test_bulk_delete_on_an_empty_list_is_a_no_op(tmp_path):
    conn = get_connection(tmp_path / "db.sqlite")

    with _Counter(conn) as statements:
        bulk_delete_listings(conn, [])

    assert statements == []


def test_bulk_delete_chunks_a_listing_id_list_past_the_variable_limit(tmp_path):
    """A delisting larger than the variable limit must not blow up."""
    conn = get_connection(tmp_path / "db.sqlite")
    ids = [f"L{n:04d}" for n in range(MAX_SQL_VARIABLES + 50)]

    bulk_delete_listings(conn, ids)  # must not raise


# --- hosted_photos must be pruned too (issue #22) ------------------------

def _turso_shaped_conn() -> sqlite3.Connection:
    """A connection with the full Turso schema, including hosted_photos --
    which exists only there and has no foreign key back to listings."""
    from src.turso_db import ensure_schema

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    return conn


def test_delisting_prunes_hosted_photos(tmp_path):
    """hosted_photos declares no foreign key, so nothing makes this fail
    loudly -- the rows just linger. publish.py pruned them via its own
    PRUNABLE_TABLES; when it was deleted that had to move here or every
    delisting would orphan rows and strand their blobs forever.
    """
    conn = _turso_shaped_conn()
    bulk_upsert_listings(conn, [_listing(1), _listing(2)])
    conn.execute("INSERT INTO hosted_photos VALUES ('L0001', 1, 'https://blob/a.jpg')")
    conn.execute("INSERT INTO hosted_photos VALUES ('L0002', 1, 'https://blob/b.jpg')")
    conn.commit()

    bulk_delete_listings(conn, ["L0001"])

    remaining = [r["listing_id"] for r in conn.execute("SELECT * FROM hosted_photos")]
    assert remaining == ["L0002"], "delisted listing left orphaned hosted_photos rows"


def test_delisting_still_works_without_a_hosted_photos_table(tmp_path):
    """The local schema has no hosted_photos. Deleting from a table that does
    not exist must not blow up the delisting path."""
    conn = get_connection(tmp_path / "db.sqlite")
    bulk_upsert_listings(conn, [_listing(1)])

    bulk_delete_listings(conn, ["L0001"])  # must not raise

    assert query_listings(conn) == []


# --- duplicates across collection tabs -------------------------------------

def test_duplicate_listing_in_one_batch_doubles_child_rows():
    """Why dedup must happen before the upsert, not just before the diff.

    `listings` is INSERT OR REPLACE on a primary key, so a duplicate is
    harmless there. `amenities` and `photo_urls` have no unique constraint
    (see _SCHEMA), so the same listing twice in one batch silently doubles
    both. A listing sitting in favorites and matches at once -- 12307 Utica
    Street, today -- is exactly that input.
    """
    conn = _fk_conn()
    listing = _listing(1, amenities=3, photos=4)

    bulk_upsert_listings(conn, [listing, listing])

    assert conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM amenities").fetchone()[0] == 6
    assert conn.execute("SELECT COUNT(*) FROM photo_urls").fetchone()[0] == 8


def test_dedupe_before_upsert_keeps_child_rows_correct():
    from src.backfill import dedupe_by_listing_id

    conn = _fk_conn()
    listing = _listing(1, amenities=3, photos=4)

    bulk_upsert_listings(conn, dedupe_by_listing_id([listing, listing]))

    assert conn.execute("SELECT COUNT(*) FROM amenities").fetchone()[0] == 3
    assert conn.execute("SELECT COUNT(*) FROM photo_urls").fetchone()[0] == 4
