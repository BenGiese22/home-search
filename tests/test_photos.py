from pathlib import Path

from src.photos import (
    count_downloaded_photos,
    delete_photos,
    download_photos,
    photo_filename,
)


def test_download_photos_writes_content_keyed_files(tmp_path: Path):
    calls = []

    def fake_fetch(url: str) -> bytes:
        calls.append(url)
        return f"bytes for {url}".encode()

    dest_dir = tmp_path / "listing-1"
    urls = ["https://example.com/a.jpg", "https://example.com/b.jpg"]

    saved = download_photos(urls, dest_dir, fake_fetch)

    assert saved == [
        dest_dir / photo_filename(1, urls[0]),
        dest_dir / photo_filename(2, urls[1]),
    ]
    assert saved[0].read_bytes() == b"bytes for https://example.com/a.jpg"
    assert saved[1].read_bytes() == b"bytes for https://example.com/b.jpg"
    assert calls == urls


def test_download_photos_skips_existing_files(tmp_path: Path):
    dest_dir = tmp_path / "listing-1"
    dest_dir.mkdir(parents=True)
    url = "https://example.com/a.jpg"
    (dest_dir / photo_filename(1, url)).write_bytes(b"already here")

    calls = []

    def fake_fetch(u: str) -> bytes:
        calls.append(u)
        return b"new bytes"

    download_photos([url], dest_dir, fake_fetch)

    assert calls == []
    assert (dest_dir / photo_filename(1, url)).read_bytes() == b"already here"


def test_download_photos_refetches_a_position_whose_url_changed(tmp_path: Path):
    """The relist case, at the disk layer. Position 1 already exists for the
    OLD url; the listing now serves a different photo there. Keying the file
    on the url is what makes the skip notice."""
    dest_dir = tmp_path / "listing-1"
    dest_dir.mkdir(parents=True)
    old_url = "https://example.com/old.jpg"
    new_url = "https://example.com/new.jpg"
    (dest_dir / photo_filename(1, old_url)).write_bytes(b"stale")

    saved = download_photos([new_url], dest_dir, lambda u: b"fresh")

    assert saved == [dest_dir / photo_filename(1, new_url)]
    assert (dest_dir / photo_filename(1, new_url)).read_bytes() == b"fresh"


def test_download_photos_skips_photo_on_fetch_failure(tmp_path: Path):
    dest_dir = tmp_path / "listing-1"
    urls = ["https://example.com/bad.jpg", "https://example.com/good.jpg"]

    def flaky_fetch(url: str) -> bytes:
        if "bad" in url:
            raise RuntimeError("network error")
        return b"good bytes"

    saved = download_photos(urls, dest_dir, flaky_fetch)

    assert saved == [dest_dir / photo_filename(2, urls[1])]
    assert not (dest_dir / photo_filename(1, urls[0])).exists()
    assert saved[0].read_bytes() == b"good bytes"


def test_download_photos_calls_sleep_fn_after_each_network_fetch(tmp_path: Path):
    dest_dir = tmp_path / "listing-1"
    sleep_calls = []

    def fake_fetch(url: str) -> bytes:
        return b"bytes"

    urls = ["https://example.com/a.jpg", "https://example.com/b.jpg"]
    download_photos(urls, dest_dir, fake_fetch, sleep_fn=lambda: sleep_calls.append(1))

    assert len(sleep_calls) == 2


def test_download_photos_does_not_sleep_for_skipped_or_failed_photos(tmp_path: Path):
    dest_dir = tmp_path / "listing-1"
    dest_dir.mkdir(parents=True)
    urls = ["https://example.com/a.jpg", "https://example.com/bad.jpg"]
    (dest_dir / photo_filename(1, urls[0])).write_bytes(b"already here")
    sleep_calls = []

    def flaky_fetch(url: str) -> bytes:
        raise RuntimeError("network error")

    download_photos(urls, dest_dir, flaky_fetch, sleep_fn=lambda: sleep_calls.append(1))

    assert sleep_calls == []


def test_delete_photos_removes_directory_and_contents(tmp_path: Path):
    listing_dir = tmp_path / "abc123"
    listing_dir.mkdir()
    (listing_dir / "01.jpg").write_bytes(b"x")
    (listing_dir / "02.jpg").write_bytes(b"x")

    delete_photos(tmp_path, "abc123")

    assert not listing_dir.exists()


def test_delete_photos_is_safe_when_directory_missing(tmp_path: Path):
    delete_photos(tmp_path, "nope")  # should not raise, no warning printed


def test_delete_photos_logs_warning_on_real_failure(tmp_path: Path, capsys, monkeypatch):
    listing_dir = tmp_path / "abc123"
    listing_dir.mkdir()
    (listing_dir / "01.jpg").write_bytes(b"x")

    def boom(path, *args, **kwargs):
        raise OSError("permission denied")

    monkeypatch.setattr("shutil.rmtree", boom)

    delete_photos(tmp_path, "abc123")  # should not raise

    assert "abc123" in capsys.readouterr().out


def test_count_downloaded_photos_counts_content_keyed_files(tmp_path: Path):
    listing_dir = tmp_path / "abc123"
    listing_dir.mkdir()
    (listing_dir / photo_filename(1, "https://example.com/a.jpg")).write_bytes(b"x")
    (listing_dir / photo_filename(2, "https://example.com/b.jpg")).write_bytes(b"x")

    assert count_downloaded_photos(tmp_path, "abc123") == 2


def test_count_downloaded_photos_ignores_old_format_files(tmp_path: Path):
    """An un-migrated NN.jpg must not count towards the vision-scoring floor:
    it may belong to a previous listing at this id."""
    listing_dir = tmp_path / "abc123"
    listing_dir.mkdir()
    (listing_dir / "01.jpg").write_bytes(b"x")
    (listing_dir / photo_filename(2, "https://example.com/b.jpg")).write_bytes(b"x")

    assert count_downloaded_photos(tmp_path, "abc123") == 1


def test_count_downloaded_photos_returns_zero_when_dir_missing(tmp_path: Path):
    assert count_downloaded_photos(tmp_path, "nope") == 0


def test_count_downloaded_photos_ignores_non_jpg_files(tmp_path: Path):
    listing_dir = tmp_path / "abc123"
    listing_dir.mkdir()
    (listing_dir / photo_filename(1, "https://example.com/a.jpg")).write_bytes(b"x")
    (listing_dir / "notes.txt").write_bytes(b"x")

    assert count_downloaded_photos(tmp_path, "abc123") == 1
