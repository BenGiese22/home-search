"""A photo's identity is its source URL, not its position.

The positional key was unsound and it cost a real incident: 6085 West 82nd
Drive was delisted and relisted under the same listing_id with 44 different
photos in the same 44 positions. Every `(listing_id, position)` key matched,
so the download skipped, the upload skipped, and the viewer would have gone
on serving the previous listing's images with nothing failing anywhere.

These tests pin the replacement: the filename on disk and the pathname in
Blob both carry `sha1(source_url)[:8]`, so a changed URL is a different file
and a different blob rather than a silent overwrite behind Blob's CDN cache.
"""
import hashlib
import importlib.util
import sqlite3
from pathlib import Path

import pytest

from src.photos import (
    PHOTO_GLOB,
    parse_photo_filename,
    photo_filename,
    photo_hash,
)
from src.turso_db import ensure_schema


# --- the hash -------------------------------------------------------------

def test_photo_hash_is_the_first_eight_hex_of_sha1():
    url = "https://example.com/a.jpg"
    expected = hashlib.sha1(url.encode("utf-8")).hexdigest()[:8]

    assert photo_hash(url) == expected
    assert len(photo_hash(url)) == 8


def test_the_same_url_always_hashes_the_same():
    assert photo_hash("https://example.com/a.jpg") == photo_hash(
        "https://example.com/a.jpg"
    )


def test_a_different_url_hashes_differently():
    """The whole point: the relist case must not collide."""
    assert photo_hash("https://example.com/old.jpg") != photo_hash(
        "https://example.com/new.jpg"
    )


def test_a_non_ascii_url_hashes_without_raising():
    assert len(photo_hash("https://example.com/café.jpg")) == 8


# --- the filename ---------------------------------------------------------

def test_photo_filename_is_position_then_hash():
    url = "https://example.com/a.jpg"

    assert photo_filename(1, url) == f"01-{photo_hash(url)}.jpg"


def test_photo_filename_zero_pads_the_position():
    """Sorting the directory has to stay positional -- score_photos.py and
    the gallery both rely on sorted() giving photo order."""
    url = "https://example.com/a.jpg"

    assert photo_filename(7, url).startswith("07-")
    assert photo_filename(12, url).startswith("12-")


def test_two_photos_of_the_same_listing_at_the_same_position_differ_by_url():
    assert photo_filename(3, "https://example.com/old.jpg") != photo_filename(
        3, "https://example.com/new.jpg"
    )


# --- parsing back ---------------------------------------------------------

def test_parse_photo_filename_round_trips():
    url = "https://example.com/a.jpg"
    name = photo_filename(4, url)

    assert parse_photo_filename(name) == (4, photo_hash(url))


@pytest.mark.parametrize(
    "name",
    [
        "01.jpg",          # the old positional format
        "cover.jpg",
        "1-abcdef12.jpg",  # position not zero-padded
        "01-abc.jpg",      # hash too short
        "01-abcdefg1.jpg",  # hash too long
        "01-ABCDEF12.jpg",  # sha1 hex is lowercase
        "01-abcdef1z.jpg",  # not hex
        "01-abcdef12.png",
        "",
    ],
)
def test_parse_photo_filename_rejects_anything_else(name):
    assert parse_photo_filename(name) is None


# --- the glob -------------------------------------------------------------

def test_the_glob_matches_the_new_format(tmp_path):
    (tmp_path / photo_filename(1, "https://example.com/a.jpg")).write_bytes(b"x")

    assert len(list(tmp_path.glob(PHOTO_GLOB))) == 1


def test_the_glob_matches_a_three_digit_position(tmp_path):
    """MAX_PHOTOS_PER_LISTING defaults to no cap, so position 100 exists and
    must not fall out of the pattern."""
    (tmp_path / photo_filename(100, "https://example.com/a.jpg")).write_bytes(b"x")

    assert len(list(tmp_path.glob(PHOTO_GLOB))) == 1
    assert parse_photo_filename(
        photo_filename(100, "https://example.com/a.jpg")
    ) == (100, photo_hash("https://example.com/a.jpg"))


def test_the_glob_ignores_a_non_numeric_prefix(tmp_path):
    (tmp_path / "cover-abcdef12.jpg").write_bytes(b"x")

    assert list(tmp_path.glob(PHOTO_GLOB)) == []


def test_the_glob_ignores_old_format_files(tmp_path):
    """A leftover NN.jpg from before the migration must be invisible --
    counted, scored or uploaded, it would be the stale photo all over again."""
    (tmp_path / "01.jpg").write_bytes(b"x")
    (tmp_path / "02.jpg").write_bytes(b"x")

    assert list(tmp_path.glob(PHOTO_GLOB)) == []


# --- the one-time migrations ----------------------------------------------
#
# Two scripts move the existing corpus onto the new identity. They run once,
# from Ben's desktop, before the next scrape. They are exercised here against
# in-memory sqlite and tmp_path because the alternative is only finding a
# mistake after it has renamed 3,253 files or rewritten 3,255 rows.

def _load(name: str):
    """ops/ is not a package, so the scripts load by path."""
    path = Path("ops") / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _hosted_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    return conn


def _listing(conn, listing_id: str, urls: list[str]) -> None:
    conn.execute(
        "INSERT INTO listings (listing_id, address, city, state, zip_code, price, "
        "beds, baths, sqft, lot_sqft, parking_spaces, year_built, description, "
        "listing_url) VALUES (?, 'a', 'Arvada', 'CO', '80003', '$1', 3, 2.0, 1, 1, "
        "2, 2000, 'd', 'u')",
        (listing_id,),
    )
    # photo_urls.position is ZERO-based (upsert_listing enumerates from 0).
    conn.executemany(
        "INSERT INTO photo_urls (listing_id, position, url) VALUES (?, ?, ?)",
        [(listing_id, i, url) for i, url in enumerate(urls)],
    )
    conn.commit()


def test_the_backfill_fills_source_url_from_photo_urls():
    """The off-by-one is the whole risk here: hosted_photos.position is
    one-based (it comes from the NN in the filename) while photo_urls.position
    is zero-based. Getting it wrong would assign every photo its neighbour's
    identity and silently re-upload the entire corpus."""
    backfill = _load("backfill_hosted_source_urls")
    conn = _hosted_conn()
    _listing(conn, "aaa", ["https://cdn/first.jpg", "https://cdn/second.jpg"])
    conn.executemany(
        "INSERT INTO hosted_photos (listing_id, position, blob_url) VALUES (?, ?, ?)",
        [("aaa", 1, "https://blob/1.jpg"), ("aaa", 2, "https://blob/2.jpg")],
    )
    conn.commit()

    updated, still_null = backfill.backfill_source_urls(conn)

    assert (updated, still_null) == (2, 0)
    rows = conn.execute(
        "SELECT position, source_url FROM hosted_photos ORDER BY position"
    ).fetchall()
    assert [r["source_url"] for r in rows] == [
        "https://cdn/first.jpg", "https://cdn/second.jpg"
    ]


def test_the_backfill_is_one_statement_and_leaves_filled_rows_alone():
    backfill = _load("backfill_hosted_source_urls")
    conn = _hosted_conn()
    _listing(conn, "aaa", ["https://cdn/first.jpg"])
    conn.execute(
        "INSERT INTO hosted_photos (listing_id, position, blob_url, source_url) "
        "VALUES ('aaa', 1, 'https://blob/1.jpg', 'https://cdn/already-known.jpg')"
    )
    conn.commit()

    statements = []
    conn.set_trace_callback(statements.append)
    updated, _ = backfill.backfill_source_urls(conn)
    conn.set_trace_callback(None)

    assert updated == 0
    assert len([s for s in statements if s.strip().upper().startswith("UPDATE")]) == 1
    row = conn.execute("SELECT source_url FROM hosted_photos").fetchone()
    assert row["source_url"] == "https://cdn/already-known.jpg"


def test_the_backfill_reports_rows_it_could_not_identify():
    """A hosted row whose listing has no URL at that slot is stale -- it must
    be counted and reported, not left looking migrated."""
    backfill = _load("backfill_hosted_source_urls")
    conn = _hosted_conn()
    _listing(conn, "aaa", ["https://cdn/first.jpg"])
    conn.executemany(
        "INSERT INTO hosted_photos (listing_id, position, blob_url) VALUES (?, ?, ?)",
        [("aaa", 1, "https://blob/1.jpg"), ("aaa", 9, "https://blob/9.jpg")],
    )
    conn.commit()

    updated, still_null = backfill.backfill_source_urls(conn)

    assert (updated, still_null) == (1, 1)


def test_the_file_migration_renames_by_the_same_join(tmp_path):
    migrate = _load("migrate_photo_files")
    d = tmp_path / "aaa"
    d.mkdir()
    (d / "01.jpg").write_bytes(b"one")
    (d / "02.jpg").write_bytes(b"two")
    urls = ["https://cdn/first.jpg", "https://cdn/second.jpg"]

    renamed, orphaned = migrate.migrate_listing(d, urls, apply=True)

    assert (renamed, orphaned) == (2, 0)
    assert (d / photo_filename(1, urls[0])).read_bytes() == b"one"
    assert (d / photo_filename(2, urls[1])).read_bytes() == b"two"
    assert not (d / "01.jpg").exists()


def test_a_file_with_no_current_url_is_deleted(tmp_path):
    """It belongs to a set of photos the listing no longer serves. Leaving it
    would keep it eligible for scoring under the wrong listing."""
    migrate = _load("migrate_photo_files")
    d = tmp_path / "aaa"
    d.mkdir()
    (d / "01.jpg").write_bytes(b"one")
    (d / "07.jpg").write_bytes(b"stale")

    renamed, orphaned = migrate.migrate_listing(d, ["https://cdn/first.jpg"], apply=True)

    assert (renamed, orphaned) == (1, 1)
    assert not (d / "07.jpg").exists()


def test_already_migrated_files_are_left_alone(tmp_path):
    """The script has to be safe to run twice -- a rerun after a partial one
    is the likeliest way it gets used."""
    migrate = _load("migrate_photo_files")
    d = tmp_path / "aaa"
    d.mkdir()
    url = "https://cdn/first.jpg"
    (d / photo_filename(1, url)).write_bytes(b"one")

    assert migrate.migrate_listing(d, [url], apply=True) == (0, 0)
    assert (d / photo_filename(1, url)).read_bytes() == b"one"


def test_a_dry_run_changes_nothing_on_disk(tmp_path):
    migrate = _load("migrate_photo_files")
    d = tmp_path / "aaa"
    d.mkdir()
    (d / "01.jpg").write_bytes(b"one")
    (d / "07.jpg").write_bytes(b"stale")

    renamed, orphaned = migrate.migrate_listing(d, ["https://cdn/first.jpg"], apply=False)

    assert (renamed, orphaned) == (1, 1)
    assert (d / "01.jpg").exists()
    assert (d / "07.jpg").exists()
