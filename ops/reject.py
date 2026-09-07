"""Say no to a house, permanently, without it coming back under a new id.

    venv/bin/python ops/reject.py <listing_id|address> [--reason "..."]
    venv/bin/python ops/reject.py --list
    venv/bin/python ops/reject.py --undo <property_id>

Rejection is recorded against the **property**, not the listing. Compass keys
its own notInterested on the listing id, so a relist mints a new one and the
rejection is forgotten -- the house comes back, gets re-scraped,
re-photographed, and re-paid for at the vision API. The property id survives
that, which is why #86 made it the identity.

The listing's own rows are then removed by the ordinary delisting cascade on
the next run, blobs reclaimed, and the digest reports it as a rejection
rather than as a sale.

Requires the property id to have been resolved. It is cached by the scrape
(one HEAD per listing, once ever), so in practice every listing has one --
but a listing added minutes ago may not, and this refuses rather than
guessing.
"""

import sys
from pathlib import Path

# Run as `python ops/<name>.py`, which puts ops/ on sys.path rather than the
# repo root. Same line as ops/canary.py, for the same reason.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.db import (
    reject_property,
    rejected_property_ids,
    unreject_property,
)
from src.turso_db import stage_connection


def _find(conn, needle: str):
    """Resolve a listing id or an address fragment to (listing_id, address,
    property_id). Refuses an ambiguous match rather than picking one."""
    rows = conn.execute(
        """
        SELECT l.listing_id, l.address, l.city, p.property_id
        FROM listings l LEFT JOIN property_ids p ON p.listing_id = l.listing_id
        WHERE l.listing_id = ? OR LOWER(l.address) LIKE LOWER(?)
        """,
        (needle, f"%{needle}%"),
    ).fetchall()
    if not rows:
        print(f"no listing matches {needle!r}", file=sys.stderr)
        return None
    if len(rows) > 1:
        print(f"{needle!r} matches {len(rows)} listings:", file=sys.stderr)
        for r in rows:
            print(f"  {r['listing_id']}  {r['address']}, {r['city']}", file=sys.stderr)
        print("be more specific", file=sys.stderr)
        return None
    return rows[0]


def main() -> int:
    args = sys.argv[1:]
    conn = stage_connection()

    if "--list" in args:
        pids = rejected_property_ids(conn)
        if not pids:
            print("nothing rejected")
            return 0
        print(f"{len(pids)} rejected propert(ies):")
        for row in conn.execute(
            "SELECT property_id, reason, rejected_at FROM rejections ORDER BY rejected_at"
        ):
            print(f"  {row['property_id']:10} {row['rejected_at'][:10]}  {row['reason'] or ''}")
        return 0

    if "--undo" in args:
        pid = args[args.index("--undo") + 1]
        unreject_property(conn, pid)
        print(f"un-rejected {pid}. It will be re-ingested on the next run.")
        return 0

    if not args or args[0].startswith("--"):
        print(__doc__, file=sys.stderr)
        return 2

    reason = None
    if "--reason" in args:
        reason = args[args.index("--reason") + 1]

    row = _find(conn, args[0])
    if row is None:
        return 1
    if not row["property_id"]:
        print(
            f"{row['address']} has no resolved property id yet, so a rejection "
            "could not survive a relist. Run the scrape once and try again.",
            file=sys.stderr,
        )
        return 1

    reject_property(conn, row["property_id"], reason=reason)
    print(f"rejected {row['address']}, {row['city']}")
    print(f"  property {row['property_id']} (listing {row['listing_id']})")
    print("  It will be removed on the next run and will not come back, even")
    print("  if Compass relists it under a new listing id.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
