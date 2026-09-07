"""A vision failure must be retried, and must not become a loop.

Two listings sat permanently unscored because the selector asked for rows
that do not exist, and a `photo_score_unavailable = 1` row does. Both were
written by the below-the-floor skip during a run where their photos had not
been downloaded yet; both have had photos ever since. The same shape as the
commute selector before #74, where a failed geocode counted as "covered".
"""

import sqlite3

import pytest

import score_photos
from src.db import get_listing_ids_missing_visual_score, upsert_visual_score
from src.turso_db import ensure_schema
from src.vision import MIN_PHOTOS_FOR_VISION_SCORING, VisualScoreResult


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    ensure_schema(c)
    return c


def add(conn, lid, hosted=0):
    conn.execute(
        """INSERT INTO listings (
             listing_id, address, city, state, zip_code, price, beds, baths,
             sqft, lot_sqft, parking_spaces, year_built, description,
             listing_url, property_type, localized_status
           ) VALUES (?, 'A St', 'Arvada', 'CO', '80003', '$1', 3, 2.0, 1800,
                     7000, 2, 1990, 'd', 'https://x/l', 'Single Family', 'Active')""",
        (lid,),
    )
    for i in range(hosted):
        conn.execute(
            "INSERT INTO hosted_photos (listing_id, position, blob_url, source_url)"
            " VALUES (?, ?, ?, ?)",
            (lid, i + 1, f"https://blob/{lid}/{i}.jpg", f"https://cdn/{lid}/{i}.jpg"),
        )
    conn.commit()


def failed(conn, lid):
    upsert_visual_score(conn, lid, None)


def scored(conn, lid):
    upsert_visual_score(
        conn, lid, VisualScoreResult(condition_photo_score=70.0, outdoor_photo_score=60.0)
    )


# --- the selector ---------------------------------------------------------


def test_a_listing_with_no_row_is_selected(conn):
    add(conn, "fresh")
    assert get_listing_ids_missing_visual_score(conn) == ["fresh"]


def test_a_recorded_failure_with_photos_is_retried(conn):
    """The stall. Selecting only rows that do not exist made a failure row
    invisible forever."""
    add(conn, "stuck", hosted=37)
    failed(conn, "stuck")
    assert get_listing_ids_missing_visual_score(conn) == ["stuck"]


def test_a_recorded_failure_with_too_few_photos_is_left_alone(conn):
    """A house that genuinely has three photos is not a failure to retry --
    it is an answer. Retrying it every run would spend money to learn the
    same thing."""
    add(conn, "thin", hosted=MIN_PHOTOS_FOR_VISION_SCORING - 1)
    failed(conn, "thin")
    assert get_listing_ids_missing_visual_score(conn) == []


def test_a_successfully_scored_listing_is_never_reselected(conn):
    add(conn, "done", hosted=37)
    scored(conn, "done")
    assert get_listing_ids_missing_visual_score(conn) == []


def test_the_retry_counts_hosted_photos_not_files_on_disk(conn):
    """data/photos/ does not survive the sandbox, and scrape will not
    re-download a listing whose URLs are all already hosted. A disk-keyed
    retry would find nothing and re-record the failure forever."""
    add(conn, "stuck", hosted=MIN_PHOTOS_FOR_VISION_SCORING)
    failed(conn, "stuck")
    # No files are written anywhere in this test.
    assert get_listing_ids_missing_visual_score(conn) == ["stuck"]


# --- pulling the photos back ---------------------------------------------


def test_photos_are_restored_from_blob(conn, tmp_path, monkeypatch):
    add(conn, "stuck", hosted=6)
    monkeypatch.setattr(score_photos, "PHOTOS_DIR", tmp_path)

    count = score_photos.hydrate_from_blob(conn, "stuck", fetch=lambda url: b"jpegbytes")

    assert count == 6
    names = sorted(p.name for p in (tmp_path / "stuck").iterdir())
    assert names[0].startswith("01-") and names[0].endswith(".jpg")


def test_restored_files_sort_into_listing_order(conn, tmp_path, monkeypatch):
    """score_photos and the gallery both depend on a plain sorted() staying in
    listing order, which is why the position leads the filename."""
    add(conn, "stuck", hosted=12)
    monkeypatch.setattr(score_photos, "PHOTOS_DIR", tmp_path)

    score_photos.hydrate_from_blob(conn, "stuck", fetch=lambda url: b"x")

    names = sorted(p.name for p in (tmp_path / "stuck").iterdir())
    assert [n[:2] for n in names] == [f"{i:02d}" for i in range(1, 13)]


def test_an_existing_file_is_not_refetched(conn, tmp_path, monkeypatch):
    add(conn, "stuck", hosted=5)
    monkeypatch.setattr(score_photos, "PHOTOS_DIR", tmp_path)
    score_photos.hydrate_from_blob(conn, "stuck", fetch=lambda url: b"x")

    calls = []
    score_photos.hydrate_from_blob(conn, "stuck", fetch=lambda url: calls.append(url) or b"x")

    assert calls == []


def test_a_fetch_failure_is_reported_not_raised(conn, tmp_path, monkeypatch, capsys):
    """One unreachable photo must not fail a run over one house."""
    add(conn, "stuck", hosted=3)
    monkeypatch.setattr(score_photos, "PHOTOS_DIR", tmp_path)

    def boom(url):
        raise RuntimeError("404")

    assert score_photos.hydrate_from_blob(conn, "stuck", fetch=boom) == 0
    assert "could not restore" in capsys.readouterr().out


def test_a_listing_with_nothing_hosted_restores_nothing(conn, tmp_path, monkeypatch):
    add(conn, "empty")
    monkeypatch.setattr(score_photos, "PHOTOS_DIR", tmp_path)
    assert score_photos.hydrate_from_blob(conn, "empty", fetch=lambda url: b"x") == 0
