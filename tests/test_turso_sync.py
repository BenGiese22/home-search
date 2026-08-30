import sqlite3

from src.db import _SCHEMA
from src.turso_sync import BATCH_CHUNK, ensure_schema, upsert_row, upsert_rows, replace_listing_rows


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    return conn


def test_ensure_schema_creates_every_mirrored_table_and_hosted_photos():
    conn = _connect()

    ensure_schema(conn)

    tables = {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {
        "listings", "amenities", "photo_urls", "commute", "scores",
        "visual_scores", "hosted_photos",
    } <= tables


def test_ensure_schema_is_idempotent():
    conn = _connect()

    ensure_schema(conn)
    ensure_schema(conn)  # must not raise


def test_upsert_row_inserts_using_the_source_rows_own_columns():
    source = _connect()
    source.executescript(_SCHEMA)
    source.execute(
        "INSERT INTO commute (listing_id, lat, lon, denver_miles, denver_minutes, "
        "medtronic_miles, medtronic_minutes, geocode_failed, computed_at) "
        "VALUES ('abc', 39.8, -105.1, 14.2, 24.0, 9.8, 18.0, 0, '2026-08-27T00:00:00Z')"
    )
    row = source.execute("SELECT * FROM commute WHERE listing_id = 'abc'").fetchone()

    dest = _connect()
    ensure_schema(dest)

    upsert_row(dest, "commute", row)

    result = dest.execute("SELECT * FROM commute WHERE listing_id = 'abc'").fetchone()
    assert result["denver_minutes"] == 24.0
    assert result["medtronic_minutes"] == 18.0


def test_upsert_row_replaces_existing_row_with_same_key():
    dest = _connect()
    ensure_schema(dest)
    source = _connect()
    source.executescript(_SCHEMA)

    source.execute(
        "INSERT INTO commute (listing_id, lat, lon, denver_miles, denver_minutes, "
        "medtronic_miles, medtronic_minutes, geocode_failed, computed_at) "
        "VALUES ('abc', 1, 1, 1, 1, 1, 1, 0, 't1')"
    )
    row_v1 = source.execute("SELECT * FROM commute WHERE listing_id = 'abc'").fetchone()
    upsert_row(dest, "commute", row_v1)

    source.execute("UPDATE commute SET denver_minutes = 99 WHERE listing_id = 'abc'")
    row_v2 = source.execute("SELECT * FROM commute WHERE listing_id = 'abc'").fetchone()
    upsert_row(dest, "commute", row_v2)

    rows = dest.execute("SELECT * FROM commute WHERE listing_id = 'abc'").fetchall()
    assert len(rows) == 1
    assert rows[0]["denver_minutes"] == 99


def test_ensure_schema_migrates_a_mirror_created_before_a_column_existed():
    # Simulates a Turso mirror created before `property_type` was added to
    # _SCHEMA: CREATE TABLE IF NOT EXISTS alone would no-op on this table
    # forever, so the column must be added by an explicit ALTER TABLE.
    conn = _connect()
    conn.execute(
        """
        CREATE TABLE listings (
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
            is_pinned INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    cols_before = {row[1] for row in conn.execute("PRAGMA table_info(listings)")}
    assert "property_type" not in cols_before
    assert "localized_status" not in cols_before

    ensure_schema(conn)

    cols_after = {row[1] for row in conn.execute("PRAGMA table_info(listings)")}
    assert "property_type" in cols_after
    assert "localized_status" in cols_after

    source = _connect()
    source.executescript(_SCHEMA)
    source.execute(
        "INSERT INTO listings (listing_id, address, city, state, zip_code, price, "
        "price_numeric, beds, baths, sqft, lot_sqft, parking_spaces, year_built, "
        "description, listing_url, is_pinned, property_type, localized_status) "
        "VALUES ('abc', '123 Main St', 'Denver', 'CO', '80202', '$500,000', 500000, "
        "3, 2.0, 1500, 5000, 2, 2000, 'A house', 'https://example.com', 0, "
        "'SingleFamily', 'Active')"
    )
    row = source.execute("SELECT * FROM listings WHERE listing_id = 'abc'").fetchone()

    upsert_row(conn, "listings", row)

    result = conn.execute("SELECT * FROM listings WHERE listing_id = 'abc'").fetchone()
    assert result["property_type"] == "SingleFamily"
    assert result["localized_status"] == "Active"


def test_replace_listing_rows_drops_stale_rows_not_in_the_new_set():
    source = _connect()
    source.executescript(_SCHEMA)
    source.execute("INSERT INTO amenities (listing_id, amenity) VALUES ('abc', 'Pool')")
    source.execute("INSERT INTO amenities (listing_id, amenity) VALUES ('abc', 'Deck')")
    old_rows = source.execute(
        "SELECT * FROM amenities WHERE listing_id = 'abc'"
    ).fetchall()

    dest = _connect()
    ensure_schema(dest)
    replace_listing_rows(dest, "amenities", "abc", old_rows)

    source.execute("DELETE FROM amenities WHERE listing_id = 'abc'")
    source.execute("INSERT INTO amenities (listing_id, amenity) VALUES ('abc', 'New roof')")
    new_rows = source.execute(
        "SELECT * FROM amenities WHERE listing_id = 'abc'"
    ).fetchall()

    replace_listing_rows(dest, "amenities", "abc", new_rows)

    result = [
        r["amenity"]
        for r in dest.execute("SELECT * FROM amenities WHERE listing_id = 'abc'")
    ]
    assert result == ["New roof"]


def test_upsert_rows_writes_every_row():
    source = _connect()
    source.executescript(_SCHEMA)
    for i in range(120):
        source.execute(
            "INSERT INTO amenities (listing_id, amenity) VALUES (?, ?)", (f"L{i}", f"Feature {i}")
        )
    rows = source.execute("SELECT * FROM amenities").fetchall()

    dest = _connect()
    ensure_schema(dest)
    upsert_rows(dest, "amenities", rows)

    assert dest.execute("SELECT COUNT(*) FROM amenities").fetchone()[0] == 120


def test_upsert_rows_costs_one_statement_per_chunk_not_one_per_row():
    """The reason this function exists. Each statement against hosted Turso
    is a ~240ms HTTP round-trip; one-per-row made a full sync take ~22 min."""
    source = _connect()
    source.executescript(_SCHEMA)
    for i in range(120):
        source.execute("INSERT INTO amenities (listing_id, amenity) VALUES (?, ?)", (f"L{i}", "x"))
    rows = source.execute("SELECT * FROM amenities").fetchall()

    dest = _connect()
    ensure_schema(dest)
    statements = []
    inner = dest.execute

    class Counting:
        def execute(self, sql, *a, **k):
            statements.append(sql)
            return inner(sql, *a, **k)

        def commit(self, *a, **k):
            return dest.commit(*a, **k)

    upsert_rows(Counting(), "amenities", rows)

    inserts = [s for s in statements if s.lstrip().upper().startswith("INSERT")]
    assert len(inserts) == 3, f"120 rows at chunk 50 should be 3 statements, got {len(inserts)}"


def test_upsert_rows_replaces_on_conflict_like_upsert_row_did():
    source = _connect()
    source.executescript(_SCHEMA)
    source.execute(
        "INSERT INTO scores VALUES ('a', 1, 1, 1, 1, 1, 1, 10, 1, 0, 't1')"
    )
    dest = _connect()
    ensure_schema(dest)
    upsert_rows(dest, "scores", source.execute("SELECT * FROM scores").fetchall())

    source.execute("UPDATE scores SET composite = 99 WHERE listing_id = 'a'")
    upsert_rows(dest, "scores", source.execute("SELECT * FROM scores").fetchall())

    rows = dest.execute("SELECT composite FROM scores WHERE listing_id = 'a'").fetchall()
    assert len(rows) == 1
    assert rows[0][0] == 99


def test_upsert_rows_on_an_empty_list_is_a_no_op():
    dest = _connect()
    ensure_schema(dest)
    upsert_rows(dest, "amenities", [])
    assert dest.execute("SELECT COUNT(*) FROM amenities").fetchone()[0] == 0


def test_batch_chunk_stays_inside_sqlites_variable_limit():
    """CHUNK * widest column count must stay under SQLite's 999 default."""
    widest = max(len(_parse_cols(t)) for t in ("listings", "scores", "visual_scores"))
    assert BATCH_CHUNK * widest < 999, f"{BATCH_CHUNK} x {widest} exceeds the variable limit"


def _parse_cols(table: str) -> list[str]:
    conn = _connect()
    conn.executescript(_SCHEMA)
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
