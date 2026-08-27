import sqlite3

from src.db import _SCHEMA
from src.turso_sync import ensure_schema, upsert_row, replace_listing_rows


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
