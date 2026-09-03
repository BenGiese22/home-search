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

import pytest

from src.photos import (
    PHOTO_GLOB,
    parse_photo_filename,
    photo_filename,
    photo_hash,
)


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
