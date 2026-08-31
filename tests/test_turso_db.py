import math
import sqlite3
from pathlib import Path

import pytest
import turso_serverless
from turso_serverless.dbapi import Row as TursoRow

import src.turso_db as turso_db
from src.db import _SCHEMA
from src.turso_db import BATCH_CHUNK, BatchRowErrors, ensure_schema, upsert_row, upsert_rows, replace_listing_rows


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
    assert "hoa_annual" not in cols_before
    assert "tax_annual" not in cols_before

    ensure_schema(conn)

    cols_after = {row[1] for row in conn.execute("PRAGMA table_info(listings)")}
    assert "property_type" in cols_after
    assert "localized_status" in cols_after
    assert "hoa_annual" in cols_after
    for col in ("tax_annual", "sqft_above_grade", "sqft_below_grade", "outdoor_spaces"):
        assert col in cols_after

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
    expected = math.ceil(120 / BATCH_CHUNK)
    assert len(inserts) == expected, (
        f"120 rows at chunk {BATCH_CHUNK} should be {expected} "
        f"statements, got {len(inserts)}"
    )


def test_upsert_rows_replaces_on_conflict_like_upsert_row_did():
    source = _connect()
    source.executescript(_SCHEMA)
    source.execute(
        """
        INSERT INTO scores (
            listing_id, commute_score, sqft_score, condition_score, outdoor_score,
            room_count_score, parking_score, hoa_score, composite, passes_filters,
            has_incomplete_data, computed_at
        ) VALUES ('a', 1, 1, 1, 1, 1, 1, 1, 10, 1, 0, 't1')
        """
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


def test_one_bad_row_does_not_take_its_whole_batch_down():
    """Observed for real against Turso: visual_scores holds orphan rows whose
    listing no longer exists, and Turso enforces the foreign key. Without a
    per-row retry, a batch of 50 lost 49 good rows to 1 bad one."""
    dest = _connect()
    ensure_schema(dest)
    dest.execute("PRAGMA foreign_keys = ON")
    dest.execute(
        "INSERT INTO listings (listing_id, address, city, state, zip_code, price, "
        "beds, baths, sqft, lot_sqft, parking_spaces, year_built, description, "
        "listing_url) VALUES ('real','a','c','CO','80002','$1',1,1,1,1,1,2000,'d','u')"
    )
    dest.commit()

    source = _connect()
    source.executescript(_SCHEMA)
    for lid in ("real", "orphan"):
        source.execute(
            "INSERT INTO visual_scores (listing_id, has_layout_plan, "
            "watermarked_staging_detected, suspected_unwatermarked_staging, "
            "photo_score_unavailable, computed_at) VALUES (?, 0, 0, 0, 0, 't')",
            (lid,),
        )
    rows = source.execute("SELECT * FROM visual_scores ORDER BY listing_id").fetchall()

    with pytest.raises(BatchRowErrors) as caught:
        upsert_rows(dest, "visual_scores", rows)

    assert [r["listing_id"] for r in caught.value.rows] == ["orphan"]
    survived = [r[0] for r in dest.execute("SELECT listing_id FROM visual_scores")]
    assert survived == ["real"], "the good row must survive its batchmate failing"


# =========================================================================
# Connection factory and row-compat layer (issue #19)
# =========================================================================

class _StubCursor:
    """Stands in for a turso_serverless Cursor. Only `description` is read
    when a Row is built, which is what makes these tests network-free."""

    def __init__(self, columns):
        self.description = tuple(
            (name, None, None, None, None, None, None) for name in columns
        )


def _turso_row(columns, values):
    """A real turso_serverless.Row, built without a connection."""
    return TursoRow(_StubCursor(columns), values)


def test_a_turso_row_supports_access_by_name_like_sqlite3_row():
    row = _turso_row(("listing_id", "price"), ("abc", "$650,000"))

    assert row["listing_id"] == "abc"
    assert row["price"] == "$650,000"


def test_a_turso_row_supports_access_by_index_like_sqlite3_row():
    row = _turso_row(("listing_id", "price"), ("abc", "$650,000"))

    assert row[0] == "abc"
    assert row[1] == "$650,000"


def test_a_turso_row_matches_sqlite3_row_on_every_access_pattern_the_code_uses():
    """src/db.py and the stages read rows by name, by index, via .keys(),
    via dict(), and by iterating. All five must behave identically or the
    cutover breaks somewhere far from here."""
    sqlite_conn = _connect()
    sqlite_conn.row_factory = sqlite3.Row
    sqlite_conn.execute("CREATE TABLE t (listing_id TEXT, price TEXT)")
    sqlite_conn.execute("INSERT INTO t VALUES ('abc', '$650,000')")
    sqlite_row = sqlite_conn.execute("SELECT listing_id, price FROM t").fetchone()

    turso_row = _turso_row(("listing_id", "price"), ("abc", "$650,000"))

    assert turso_row["listing_id"] == sqlite_row["listing_id"]
    assert turso_row[0] == sqlite_row[0]
    assert list(turso_row.keys()) == list(sqlite_row.keys())
    assert dict(turso_row) == dict(sqlite_row)
    assert list(turso_row) == list(sqlite_row)
    assert len(turso_row) == len(sqlite_row)


def test_the_one_row_shape_difference_is_the_missing_key_exception():
    """Documented rather than papered over: sqlite3.Row raises IndexError for
    an unknown column, turso_serverless.Row raises KeyError. Nothing in this
    codebase indexes a column it did not SELECT, so this is pinned as a known
    difference rather than normalised -- if that assumption ever stops
    holding, this test is where to look."""
    sqlite_conn = _connect()
    sqlite_conn.row_factory = sqlite3.Row
    sqlite_conn.execute("CREATE TABLE t (a TEXT)")
    sqlite_conn.execute("INSERT INTO t VALUES ('x')")
    sqlite_row = sqlite_conn.execute("SELECT a FROM t").fetchone()

    with pytest.raises(IndexError):
        sqlite_row["nope"]
    with pytest.raises(KeyError):
        _turso_row(("a",), ("x",))["nope"]


def test_connect_passes_the_url_and_auth_token_through():
    captured = {}

    def fake_connect(url, auth_token=None):
        captured["url"] = url
        captured["auth_token"] = auth_token
        return _FakeTursoConnection()

    turso_db.connect(
        {"TURSO_DATABASE_URL": "libsql://db.example", "TURSO_AUTH_TOKEN": "tok"},
        connect_fn=fake_connect,
    )

    assert captured == {"url": "libsql://db.example", "auth_token": "tok"}


def test_connect_sets_a_row_factory_so_rows_are_not_bare_tuples():
    """The quirk that would otherwise break every stage at once. A
    turso_serverless connection defaults row_factory to None, so
    conn.execute(...).fetchone() hands back a plain tuple -- and every
    row["listing_id"] in src/db.py raises TypeError. The factory is the one
    place that has to remember."""
    conn = turso_db.connect(
        {"TURSO_DATABASE_URL": "libsql://db", "TURSO_AUTH_TOKEN": "t"},
        connect_fn=lambda url, auth_token=None: _FakeTursoConnection(),
    )

    assert conn.row_factory is TursoRow


def test_connect_reports_missing_configuration_clearly():
    for env in ({}, {"TURSO_DATABASE_URL": "libsql://db"}, {"TURSO_AUTH_TOKEN": "t"}):
        with pytest.raises(RuntimeError, match="TURSO_"):
            turso_db.connect(env, connect_fn=lambda *a, **k: _FakeTursoConnection())


def test_connect_falls_back_to_the_merged_env(monkeypatch):
    monkeypatch.setenv("TURSO_DATABASE_URL", "libsql://from-process")
    monkeypatch.setenv("TURSO_AUTH_TOKEN", "tok-from-process")
    captured = {}

    def fake_connect(url, auth_token=None):
        captured["url"] = url
        return _FakeTursoConnection()

    turso_db.connect(connect_fn=fake_connect)

    assert captured["url"] == "libsql://from-process"


class _FakeTursoConnection:
    """Mimics the attribute surface the factory touches."""

    def __init__(self):
        self.row_factory = None


def test_the_default_connect_fn_is_the_real_driver():
    """The injected seam exists so the suite never opens a live session, but
    production must get the real thing by default -- an injected-only seam
    that nobody wires up in production is worse than none."""
    import inspect

    default = inspect.signature(turso_db.connect).parameters["connect_fn"].default
    assert default is turso_serverless.connect


def test_missing_config_fails_before_any_connection_is_attempted():
    """Fail on configuration, not on a half-open session."""
    attempts = []

    def should_not_run(*args, **kwargs):
        attempts.append(args)
        return _FakeTursoConnection()

    with pytest.raises(RuntimeError):
        turso_db.connect({}, connect_fn=should_not_run)

    assert attempts == []
