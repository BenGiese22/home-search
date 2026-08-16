from pathlib import Path

from src.photos import delete_photos, download_photos


def test_download_photos_writes_numbered_files(tmp_path: Path):
    calls = []

    def fake_fetch(url: str) -> bytes:
        calls.append(url)
        return f"bytes for {url}".encode()

    dest_dir = tmp_path / "listing-1"
    urls = ["https://example.com/a.jpg", "https://example.com/b.jpg"]

    saved = download_photos(urls, dest_dir, fake_fetch)

    assert saved == [dest_dir / "01.jpg", dest_dir / "02.jpg"]
    assert (dest_dir / "01.jpg").read_bytes() == b"bytes for https://example.com/a.jpg"
    assert (dest_dir / "02.jpg").read_bytes() == b"bytes for https://example.com/b.jpg"
    assert calls == urls


def test_download_photos_skips_existing_files(tmp_path: Path):
    dest_dir = tmp_path / "listing-1"
    dest_dir.mkdir(parents=True)
    (dest_dir / "01.jpg").write_bytes(b"already here")

    calls = []

    def fake_fetch(url: str) -> bytes:
        calls.append(url)
        return b"new bytes"

    download_photos(["https://example.com/a.jpg"], dest_dir, fake_fetch)

    assert calls == []
    assert (dest_dir / "01.jpg").read_bytes() == b"already here"


def test_download_photos_skips_photo_on_fetch_failure(tmp_path: Path):
    dest_dir = tmp_path / "listing-1"
    urls = ["https://example.com/bad.jpg", "https://example.com/good.jpg"]

    def flaky_fetch(url: str) -> bytes:
        if "bad" in url:
            raise RuntimeError("network error")
        return b"good bytes"

    saved = download_photos(urls, dest_dir, flaky_fetch)

    assert saved == [dest_dir / "02.jpg"]
    assert not (dest_dir / "01.jpg").exists()
    assert (dest_dir / "02.jpg").read_bytes() == b"good bytes"


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
