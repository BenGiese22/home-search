"""One-time: reclaim blobs that were stranded before delete_blobs existed.

Until #57 this project could upload a blob and forget one, but never delete
one -- and `hosted_photos.blob_url` is the only record of what exists in the
store. Two sets accumulated:

1. Six hosted_photos rows whose source_url stayed NULL after
   ops/backfill_hosted_source_urls.py ran. Their listing has no photo_urls
   entry at that position, so they belong to a photo set the listing no
   longer serves.
2. 1,813 URLs exported to data/archive/orphaned-hosted-photos-20260903.json
   before those orphaned rows were deleted, precisely because deleting the
   rows would otherwise have stranded the blobs permanently.

Ordering is copied from src.diff.apply_delisting and is deliberate: capture
the URLs, delete the ROWS, then delete the blobs. Rows first means a failed
blob delete can never leave a row pointing at a blob that is already gone --
and the URLs are already in hand, so a failure prints them rather than
losing them. Doing it the other way round is how set 2 came to exist.

Irreversible. --dry-run first.

    ./venv/bin/python ops/reclaim_stranded_blobs.py --dry-run
    ./venv/bin/python ops/reclaim_stranded_blobs.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.blob_upload import delete_blobs  # noqa: E402
from src.config import load_env  # noqa: E402
from src.turso_db import stage_connection  # noqa: E402

ARCHIVE_PATH = Path("data") / "archive" / "orphaned-hosted-photos-20260903.json"


def stale_rows(conn) -> list:
    """Hosted rows with no known identity, in ONE statement.

    source_url IS NULL means the backfill could not find a photo_urls entry
    at that position -- the listing no longer serves a photo there, so the
    row and its blob are stale.
    """
    return list(
        conn.execute(
            "SELECT listing_id, position, source_url, blob_url FROM hosted_photos "
            "WHERE source_url IS NULL ORDER BY listing_id, position"
        )
    )


def reclaim_stale_rows(conn, rw_token, delete_fn=delete_blobs, apply=False) -> int:
    """Delete the stale rows and their blobs. Returns how many were acted on
    (or would be, under --dry-run)."""
    rows = stale_rows(conn)
    if not rows:
        return 0

    urls = [row["blob_url"] for row in rows]
    for row in rows:
        print(f"  {row['listing_id']}/{int(row['position']):02d}  {row['blob_url']}")
    if not apply:
        return len(rows)

    # Rows first -- see the module docstring. The URLs are already captured.
    ids = [(r["listing_id"], int(r["position"])) for r in rows]
    with conn:
        for listing_id, position in ids:
            conn.execute(
                "DELETE FROM hosted_photos WHERE listing_id = ? AND position = ?",
                (listing_id, position),
            )
    _delete_or_report(urls, rw_token, delete_fn)
    return len(rows)


def load_archived_urls(path: Path = ARCHIVE_PATH) -> list[str]:
    """The blob URLs exported before the 2026-09-03 orphan cleanup. A missing
    file is not an error -- it just means there is nothing archived to
    reclaim."""
    if not path.exists():
        return []
    data = json.loads(path.read_text())
    return [row["blob_url"] for row in data.get("rows", []) if row.get("blob_url")]


def reclaim_archived(
    path: Path = ARCHIVE_PATH,
    rw_token: str = "",
    live_urls: set[str] | None = None,
    delete_fn=delete_blobs,
    apply=False,
) -> int:
    """Delete the archived blobs, EXCEPT any that are live again.

    The archive is a point-in-time list of blobs whose rows were deleted. It
    can go stale in one direction that matters: a delisted listing can come
    back, and if it re-uploads to the same pathname its "orphaned" URL is
    current once more.

    That is not hypothetical. Checked against production before this first
    ran: 44 of the 1,813 archived URLs were live again, all belonging to
    6085 West 82nd Drive -- delisted, archived, returned as a Pending
    favorite, re-uploaded to the same photos/<id>/NN.jpg pathnames. Deleting
    on the archive alone would have blanked 44 photos in the viewer with
    nothing failing to say so.

    So the live set is subtracted, always. Pass it explicitly rather than
    defaulting to empty by accident: an empty live set means "delete
    everything the archive lists", which is the dangerous reading.

    The archive file itself is never removed -- it is the record of what was
    reclaimed, and without it there is no evidence of what the blobs were.
    """
    urls = load_archived_urls(path)
    live = live_urls or set()
    resurrected = [u for u in urls if u in live]
    urls = [u for u in urls if u not in live]
    if resurrected:
        print(
            f"  skipping {len(resurrected)} archived URL(s) that are live again "
            "(their listing came back and re-uploaded to the same pathname)"
        )
    if not urls:
        return 0
    print(f"  {len(urls)} archived blob(s) from {path}")
    if not apply:
        return len(urls)
    _delete_or_report(urls, rw_token, delete_fn)
    return len(urls)


def _delete_or_report(urls: list[str], rw_token: str, delete_fn) -> None:
    """Delete, or print every URL on failure.

    At this point the rows are gone, so these URLs are the only remaining
    handle on the blobs. Swallowing the failure silently is exactly how 1,813
    blobs came to be stranded.
    """
    try:
        delete_fn(urls, rw_token)
        print(f"  deleted {len(urls)} blob(s)")
    except Exception as exc:
        print(f"  blob deletion FAILED ({exc}). These URLs are now the only")
        print("  handle on those blobs -- keep them:")
        for url in urls:
            print(f"    {url}")


def main(argv: list[str]) -> None:
    apply = "--dry-run" not in argv
    if not apply:
        print("--dry-run: nothing will be deleted\n")

    env = load_env()
    token = env.get("BLOB_READ_WRITE_TOKEN", "")
    if apply and not token:
        sys.exit("BLOB_READ_WRITE_TOKEN is not set; refusing to delete rows we cannot reclaim")

    conn = stage_connection(env)
    print("stale hosted_photos rows (no current source URL):")
    stale = reclaim_stale_rows(conn, token, apply=apply)
    if not stale:
        print("  none")
    conn.close()

    print("\narchived orphan blobs:")
    # Read the live set from the SAME connection state the stale-row pass
    # just left, so a URL that is currently referenced is never deleted.
    conn = stage_connection(env)
    live = {row[0] for row in conn.execute("SELECT blob_url FROM hosted_photos")}
    conn.close()
    archived = reclaim_archived(ARCHIVE_PATH, token, live_urls=live, apply=apply)
    if not archived:
        print("  none")

    verb = "reclaimed" if apply else "would reclaim"
    print(f"\n{verb} {stale} stale row(s) and {archived} archived blob(s)")


if __name__ == "__main__":
    main(sys.argv[1:])
