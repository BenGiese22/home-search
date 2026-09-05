"""Remove the stale predecessor of a property that appears twice.

    python ops/dedupe_relists.py            # report only
    python ops/dedupe_relists.py --apply    # delete, reclaiming blobs

A one-off. `scrape.py` now supersedes a relist automatically the moment
Compass returns the new id (see src/diff.supersede_relisted), but that only
fires when exactly one of the pair is in a live fetch. The duplicates that
predate that code may have both ids absent from every fetch, in which case
nothing will ever clear them.

The winner is chosen the same way a human would, and the reasoning is
printed rather than assumed:

- a row with a `localized_status` beats one without -- no status means the
  scrape never saw it live
- failing that, more hosted photos wins, as the more completely ingested of
  the two

Both are tiebreaks, not evidence. Anything less clear-cut than these should
be looked at by a person, which is why this prints and requires --apply.

Deletion goes through src/diff.apply_delisting, never delete_listing:
hosted_photos.blob_url is the only record of an uploaded image, so the URLs
have to be read before the rows go. tests/test_delete_paths.py enforces
that, and this module is subject to it.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import load_env
from src.db import duplicate_address_groups
from src.diff import apply_delisting
from src.turso_db import stage_connection

PHOTOS_DIR = Path("data") / "photos"


def _row(conn, listing_id):
    return conn.execute(
        """SELECT localized_status, price, is_pinned,
             (SELECT COUNT(*) FROM hosted_photos h WHERE h.listing_id = ?) AS photos
           FROM listings WHERE listing_id = ?""",
        (listing_id, listing_id),
    ).fetchone()


def choose(conn, ids: list[str]) -> tuple[str, str, str]:
    """Returns (keep, drop, why). Raises when the pair is not clear-cut."""
    rows = {i: _row(conn, i) for i in ids}
    with_status = [i for i in ids if (rows[i][0] or "").strip()]
    if len(with_status) == 1:
        keep = with_status[0]
        return keep, next(i for i in ids if i != keep), "only one has a status"

    by_photos = sorted(ids, key=lambda i: rows[i][3], reverse=True)
    if rows[by_photos[0]][3] != rows[by_photos[1]][3]:
        return by_photos[0], by_photos[1], (
            f"more hosted photos ({rows[by_photos[0]][3]} vs {rows[by_photos[1]][3]})"
        )
    raise ValueError("no clear winner; needs a human")


def main() -> int:
    apply = "--apply" in sys.argv
    conn = stage_connection()
    groups = duplicate_address_groups(conn)
    if not groups:
        print("no duplicate addresses")
        return 0

    doomed = []
    for address, ids in groups:
        print(f"\n{address}")
        for i in ids:
            r = _row(conn, i)
            status = (r[0] or "(no status)")
            print(f"  {i}  {status:14} {r[1]:>9}  pinned={r[2]}  photos={r[3]}")
        if len(ids) != 2:
            print("  SKIP: more than two rows, needs a human")
            continue
        try:
            keep, drop, why = choose(conn, ids)
        except ValueError as exc:
            print(f"  SKIP: {exc}")
            continue
        print(f"  -> keep {keep}, drop {drop} ({why})")
        doomed.append(drop)

    if not doomed:
        return 0
    if not apply:
        print(f"\n{len(doomed)} row(s) would be removed. Re-run with --apply.")
        return 0

    print(f"\nremoving {len(doomed)} row(s), reclaiming their blobs")
    apply_delisting(
        conn, PHOTOS_DIR, doomed,
        blob_token=load_env().get("BLOB_READ_WRITE_TOKEN"),
    )
    remaining = duplicate_address_groups(conn)
    print(f"duplicate groups remaining: {len(remaining)}")
    return 0 if not remaining else 1


if __name__ == "__main__":
    sys.exit(main())
