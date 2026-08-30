from pathlib import Path

from src.gallery import render_gallery, write_gallery
from src.models import Listing

LISTING = Listing(
    listing_id="abc123",
    address="1 Test St",
    city="Testville",
    state="CO",
    zip_code="80020",
    price="$500,000",
    beds=3,
    baths=2.5,
    sqft=1800,
    lot_sqft=6000,
    parking_spaces=2,
    year_built=1995,
    description="A lovely home",
    amenities=["Renovated Kitchen", "Private Yard"],
    photo_urls=["https://example.com/1.jpg", "https://example.com/2.jpg"],
    listing_url="https://example.com/listing/abc123",
)


def test_render_gallery_includes_listing_details_and_photos(tmp_path: Path):
    photos_root = tmp_path / "photos"
    photo_dir = photos_root / "abc123"
    photo_dir.mkdir(parents=True)
    (photo_dir / "01.jpg").write_bytes(b"fake")
    (photo_dir / "02.jpg").write_bytes(b"fake")
    gallery_dir = tmp_path  # gallery.html would live at tmp_path/gallery.html

    html = render_gallery([LISTING], photos_root, gallery_dir)

    assert "1 Test St" in html
    assert "$500,000" in html
    assert "Renovated Kitchen" in html
    assert "photos/abc123/01.jpg" in html
    assert "photos/abc123/02.jpg" in html
    assert "https://example.com/listing/abc123" in html


def test_render_gallery_handles_listing_with_no_downloaded_photos(tmp_path: Path):
    photos_root = tmp_path / "photos"
    html = render_gallery([LISTING], photos_root, tmp_path)
    assert "1 Test St" in html


def test_write_gallery_writes_file(tmp_path: Path):
    photos_root = tmp_path / "photos"
    gallery_path = tmp_path / "gallery.html"

    write_gallery([LISTING], photos_root, gallery_path)

    assert gallery_path.exists()
    assert "1 Test St" in gallery_path.read_text()


def _render_one(listing, tmp_path):
    return render_gallery([listing], tmp_path / "photos", tmp_path)


def test_render_gallery_shows_no_hoa_when_confirmed_absent(tmp_path: Path):
    listing = LISTING.__class__(**{**LISTING.__dict__, "hoa_annual": 0.0})
    assert "No HOA" in _render_one(listing, tmp_path)


def test_render_gallery_shows_annual_hoa_when_known(tmp_path: Path):
    listing = LISTING.__class__(**{**LISTING.__dict__, "hoa_annual": 1200.0})
    assert "HOA $1,200/yr" in _render_one(listing, tmp_path)


def test_render_gallery_omits_hoa_line_when_unknown(tmp_path: Path):
    listing = LISTING.__class__(**{**LISTING.__dict__, "hoa_annual": None})
    html_out = _render_one(listing, tmp_path)
    assert "HOA" not in html_out
