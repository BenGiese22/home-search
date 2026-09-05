"""One-time: give every existing hosted_photos row its source_url.

hosted_photos gained source_url so the upload skip could be keyed on what a
photo IS rather than where it sits (see src/photo_upload.py). Existing rows
have it NULL, and NULL means "identity unknown", which
collect_pending_photos treats as needing re-upload -- correct, but it would
re-upload the whole corpus (~3,255 photos) and delete every current blob.
This fills them in instead, in ONE statement.

The join is `photo_urls.position = hosted_photos.position - 1`. That offset
is not cosmetic: hosted_photos.position comes from the NN in a photo's
filename, which download_photos numbers from 1, while photo_urls.position is
written by upsert_listing's `enumerate(listing.photo_urls)` and starts at 0.
Getting it wrong would give every photo its neighbour's identity, which fails
silently -- every row would look migrated and every photo would re-upload.

It assumes the rows currently on file match the URLs currently on file. After
the 2026-09-03 cleanup, and with the prune-on-delist path in place, the
residual risk is "a photo changed without a delist between its upload and
today" -- bounded and historical. `ops/rehost_photos.py --all` (an overnight
desktop run: ~3,255 downloads, ~700 MB, ~$0.02 of Blob operations) is the
paranoid alternative and is deliberately not this script.

Rows still NULL afterwards are hosted photos whose listing has no URL at that
slot. They are stale -- they belong to a set the listing no longer serves --
and must be deleted WITH THEIR BLOBS, never row-only. This script reports
them; it does not delete them.

Run once, from the desktop, before the next scrape:

    ./venv/bin/python ops/backfill_hosted_source_urls.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.turso_db import stage_connection  # noqa: E402


def backfill_source_urls(conn) -> tuple[int, int]:
    """Fills every NULL source_url from photo_urls. Returns
    (rows updated, rows still NULL).

    One UPDATE for the whole table: a per-row form would be ~3,255 statements
    at a ~240ms round-trip each, roughly 13 minutes to do what one statement
    does.
    """
    cursor = conn.execute(
        """
        UPDATE hosted_photos
        SET source_url = (
            SELECT p.url FROM photo_urls p
            WHERE p.listing_id = hosted_photos.listing_id
              AND p.position = hosted_photos.position - 1
        )
        WHERE source_url IS NULL
        """
    )
    # rowcount counts every row the WHERE matched, including ones the
    # subquery could not resolve and left NULL -- so subtract those.
    matched = cursor.rowcount if cursor.rowcount is not None else 0
    if hasattr(conn, "commit"):
        conn.commit()
    still_null = conn.execute(
        "SELECT COUNT(*) FROM hosted_photos WHERE source_url IS NULL"
    ).fetchone()[0]
    return matched - still_null, still_null


def main() -> None:
    conn = stage_connection()
    updated, still_null = backfill_source_urls(conn)
    print(f"filled source_url on {updated} hosted_photos row(s)")
    if still_null:
        print(
            f"{still_null} row(s) still have no source_url -- their listing has "
            "no photo_urls entry at that position, so they are stale. Delete "
            "them WITH THEIR BLOBS (src.blob_upload.delete_blobs); never the "
            "rows alone, or the blobs are stranded for good."
        )
        for row in conn.execute(
            "SELECT listing_id, position, blob_url FROM hosted_photos "
            "WHERE source_url IS NULL ORDER BY listing_id, position"
        ):
            print(f"  {row[0]}/{int(row[1]):02d}  {row[2]}")
    conn.close()


if __name__ == "__main__":
    main()
