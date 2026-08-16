from pathlib import Path

from src.db import get_connection, query_listings, upsert_listing
from src.diff import apply_delisting, compute_changes, format_report, should_apply_delisting
from src.models import Listing

SAMPLE = Listing(
    listing_id="abc123",
    address="1 Test St",
    city="Testville",
    state="CO",
    zip_code="80020",
    price="$650,000",
    beds=4,
    baths=2.5,
    sqft=2200,
    lot_sqft=5000,
    parking_spaces=2,
    year_built=2005,
    description="desc",
    amenities=["Garage", "Central AC"],
    photo_urls=["https://example.com/1.jpg"],
    listing_url="https://example.com/listing/abc123",
)


def test_listing_not_in_snapshot_is_new():
    report = compute_changes([SAMPLE], before={})

    assert report.new_listings == [SAMPLE]
    assert report.price_changes == []


def test_listing_with_same_price_is_unchanged():
    before = {"abc123": ("$650,000", 650000.0)}

    report = compute_changes([SAMPLE], before)

    assert report.new_listings == []
    assert report.price_changes == []


def test_listing_with_different_price_is_a_price_change():
    before = {"abc123": ("$600,000", 600000.0)}

    report = compute_changes([SAMPLE], before)

    assert report.new_listings == []
    assert len(report.price_changes) == 1
    change = report.price_changes[0]
    assert change.listing == SAMPLE
    assert change.old_price == "$600,000"
    assert change.new_price == "$650,000"


def test_format_report_lists_new_listing_address_and_url():
    report = compute_changes([SAMPLE], before={})

    text = format_report(report)

    assert "1 new listing" in text
    assert "1 Test St" in text
    assert "https://example.com/listing/abc123" in text


def test_format_report_lists_price_change_old_and_new():
    before = {"abc123": ("$600,000", 600000.0)}
    report = compute_changes([SAMPLE], before)

    text = format_report(report)

    assert "1 price change" in text
    assert "$600,000" in text
    assert "$650,000" in text


def test_duplicate_listing_id_in_fetched_is_reported_once():
    report = compute_changes([SAMPLE, SAMPLE], before={})

    assert report.new_listings == [SAMPLE]


def test_format_report_no_changes_reads_clean():
    report = compute_changes([SAMPLE], before={"abc123": ("$650,000", 650000.0)})

    text = format_report(report)

    assert "0 new listing" in text
    assert "0 price change" in text


def test_listing_in_snapshot_but_not_fetched_is_delisted():
    before = {"abc123": ("$650,000", 650000.0), "gone456": ("$500,000", 500000.0)}

    report = compute_changes([SAMPLE], before)

    assert report.delisted_ids == ["gone456"]


def test_no_delisted_ids_when_all_snapshot_listings_still_fetched():
    before = {"abc123": ("$650,000", 650000.0)}

    report = compute_changes([SAMPLE], before)

    assert report.delisted_ids == []


def test_delisted_ids_sorted_for_deterministic_output():
    before = {
        "abc123": ("$650,000", 650000.0),
        "zzz999": ("$1", 1.0),
        "aaa111": ("$1", 1.0),
    }

    report = compute_changes([SAMPLE], before)

    assert report.delisted_ids == ["aaa111", "zzz999"]


def test_format_report_lists_delisted_count_and_ids():
    before = {"abc123": ("$650,000", 650000.0), "gone456": ("$500,000", 500000.0)}
    report = compute_changes([SAMPLE], before)

    text = format_report(report)

    assert "1 delisted" in text
    assert "gone456" in text


def test_format_report_no_delisted_reads_clean():
    report = compute_changes([SAMPLE], before={"abc123": ("$650,000", 650000.0)})

    text = format_report(report)

    assert "0 delisted" in text


def test_pinned_id_absent_from_fetched_is_not_delisted():
    before = {"abc123": ("$650,000", 650000.0), "pinned789": ("$1", 1.0)}

    report = compute_changes([SAMPLE], before, pinned_ids=frozenset({"pinned789"}))

    assert report.delisted_ids == []


def test_pinned_id_does_not_suppress_other_delisted_ids():
    before = {
        "abc123": ("$650,000", 650000.0),
        "pinned789": ("$1", 1.0),
        "gone456": ("$500,000", 500000.0),
    }

    report = compute_changes([SAMPLE], before, pinned_ids=frozenset({"pinned789"}))

    assert report.delisted_ids == ["gone456"]


def test_apply_delisting_removes_db_row_photos_and_json(tmp_path: Path):
    conn = get_connection(tmp_path / "listings.db")
    upsert_listing(conn, SAMPLE)
    photos_dir = tmp_path / "photos"
    listing_photo_dir = photos_dir / "abc123"
    listing_photo_dir.mkdir(parents=True)
    (listing_photo_dir / "01.jpg").write_bytes(b"x")
    store_dir = tmp_path / "listings"
    store_dir.mkdir()
    (store_dir / "abc123.json").write_text("{}")

    apply_delisting(conn, photos_dir, store_dir, ["abc123"])

    assert query_listings(conn) == []
    assert not listing_photo_dir.exists()
    assert not (store_dir / "abc123.json").exists()


def test_apply_delisting_handles_empty_list(tmp_path: Path):
    conn = get_connection(tmp_path / "listings.db")

    apply_delisting(conn, tmp_path / "photos", tmp_path / "listings", [])  # should not raise


def test_apply_delisting_continues_past_a_failure(tmp_path: Path, monkeypatch, capsys):
    from src import diff as diff_module

    conn = get_connection(tmp_path / "listings.db")
    upsert_listing(conn, SAMPLE)
    other = SAMPLE.__class__(**{**SAMPLE.__dict__, "listing_id": "other456"})
    upsert_listing(conn, other)

    real_delete_listing = diff_module.delete_listing
    calls = []

    def flaky_delete_listing(conn, listing_id):
        calls.append(listing_id)
        if listing_id == "abc123":
            raise Exception("database is locked")
        real_delete_listing(conn, listing_id)

    monkeypatch.setattr(diff_module, "delete_listing", flaky_delete_listing)

    apply_delisting(conn, tmp_path / "photos", tmp_path / "listings", ["abc123", "other456"])

    assert calls == ["abc123", "other456"]  # both attempted, one failing doesn't stop the other
    assert [row["listing_id"] for row in query_listings(conn)] == ["abc123"]  # other456 removed
    assert "abc123" in capsys.readouterr().out


def test_should_apply_delisting_false_when_fetch_failed():
    before = {"abc123": ("$1", 1.0), "gone456": ("$1", 1.0)}
    report = compute_changes([], before)  # would look like both are delisted

    assert should_apply_delisting(False, report, before, frozenset()) is False


def test_should_apply_delisting_true_for_a_normal_small_delist():
    # 1 of 5 known listings dropped out -- an ordinary, plausible delisting
    before = {f"id{i}": ("$1", 1.0) for i in range(5)}
    fetched_ids = {f"id{i}" for i in range(4)}
    fetched = [Listing(**{**SAMPLE.__dict__, "listing_id": lid}) for lid in fetched_ids]
    report = compute_changes(fetched, before)

    assert should_apply_delisting(True, report, before, frozenset()) is True


def test_should_apply_delisting_false_when_almost_everything_looks_delisted():
    # A "successful" fetch that returned nothing -- the exact shape of the
    # bug where an empty-but-200-OK collection response would otherwise
    # wipe every known listing.
    before = {f"id{i}": ("$1", 1.0) for i in range(5)}
    report = compute_changes([], before)

    assert should_apply_delisting(True, report, before, frozenset()) is False


def test_should_apply_delisting_true_when_no_listings_known_yet():
    assert should_apply_delisting(True, compute_changes([], {}), {}, frozenset()) is True


def test_should_apply_delisting_excludes_pinned_ids_from_the_denominator():
    # 1 of 1 *eligible* (non-pinned) listing delisted -- at the threshold,
    # but the pinned listing shouldn't count toward "everything known"
    # and shouldn't itself ever appear in delisted_ids.
    before = {"pinned1": ("$1", 1.0), "gone456": ("$1", 1.0)}
    report = compute_changes([], before, pinned_ids=frozenset({"pinned1"}))

    assert report.delisted_ids == ["gone456"]
    assert should_apply_delisting(True, report, before, frozenset({"pinned1"})) is True
