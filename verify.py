"""Assert what a finished pipeline run must be true of, and fail if it is not.

    python verify.py            # exit 1 on any violation
    python verify.py --warn     # report and exit 0

Runs as the last pipeline stage, and exists because of a specific failure
mode this project keeps producing: **confident, well-formed, wrong output**.

Three defects in a row looked identical from outside -- exit 0, HTTP 200,
plausible row counts, no exception anywhere -- while the run had quietly done
the wrong thing. Dashboards cannot see that. Status codes cannot see that. The
only thing that can is an explicit statement of what should be true, checked
against what is.

So this does not measure anything. It asserts, and it turns a semantic error
into a non-zero exit code -- which is the one signal every layer above
already knows how to react to: pipeline.py stops the run, run.py records it
in the `done` marker, the reaper reads that and pushes a notification.

Nothing here talks to Compass or spends money. It is one connection and a
handful of queries against data that already exists.
"""

import sys
from dataclasses import dataclass
from typing import Callable

from src.commute import COMMUTE_SOURCE
from src.config import load_env  # noqa: F401  (kept for .env side effects)
from src.db import duplicate_address_groups, duplicate_property_groups
from src.turso_db import stage_connection


@dataclass(frozen=True)
class Violation:
    check: str
    detail: str
    rows: list


def _rows(conn, sql: str) -> list:
    return conn.execute(sql).fetchall()


def check_active_listings_have_photos(conn) -> Violation | None:
    """A listing with photo URLs but nothing hosted renders imageless.

    This is issue #70: an orphaned pin sat in exactly this state while every
    run reported success, because no stage is responsible for noticing that
    work it identified as pending never happened.
    """
    rows = _rows(conn, """
        SELECT l.listing_id, l.address
        FROM listings l
        WHERE (SELECT COUNT(*) FROM photo_urls p WHERE p.listing_id = l.listing_id) > 0
          AND (SELECT COUNT(*) FROM hosted_photos h WHERE h.listing_id = l.listing_id) = 0
        ORDER BY l.address
    """)
    if not rows:
        return None
    return Violation(
        "listings_without_hosted_photos",
        f"{len(rows)} listing(s) have photo URLs but no hosted photos, so the "
        f"viewer shows them with no images",
        [f"{r[1]} ({r[0]})" for r in rows],
    )


def check_every_listing_is_scored(conn) -> Violation | None:
    """The viewer ranks on `scores`. A listing without a row is invisible to
    the ordering rather than merely last."""
    rows = _rows(conn, """
        SELECT l.listing_id, l.address FROM listings l
        WHERE NOT EXISTS (SELECT 1 FROM scores s WHERE s.listing_id = l.listing_id)
        ORDER BY l.address
    """)
    if not rows:
        return None
    return Violation(
        "listings_without_scores",
        f"{len(rows)} listing(s) have no score row",
        [f"{r[1]} ({r[0]})" for r in rows],
    )


def check_no_orphaned_children(conn) -> Violation | None:
    """Child rows whose listing is gone. Orphaned `hosted_photos` are the
    expensive kind -- each one is a blob nothing will ever delete."""
    findings = []
    for table in ("hosted_photos", "photo_urls", "scores", "amenities", "commute", "visual_scores"):
        n = _rows(conn, f"""
            SELECT COUNT(*) FROM {table} t
            WHERE NOT EXISTS (SELECT 1 FROM listings l WHERE l.listing_id = t.listing_id)
        """)[0][0]
        if n:
            findings.append(f"{table}={n}")
    if not findings:
        return None
    return Violation(
        "orphaned_child_rows",
        "child rows survive a listing that no longer exists",
        findings,
    )


def check_corpus_is_not_empty(conn) -> Violation | None:
    """The catastrophic case, and the cheapest to check.

    A scrape that returns nothing -- an expired session, a changed selector, a
    WAF block -- can delist the entire corpus. Every stage downstream would
    then succeed perfectly against zero rows.
    """
    n = _rows(conn, "SELECT COUNT(*) FROM listings")[0][0]
    if n > 0:
        return None
    return Violation("empty_corpus", "the listings table is empty", [])


def check_addresses_are_unique(conn) -> Violation | None:
    """One property, one row.

    Two rows for one address means Compass reissued the listing under a new
    id -- a relist -- and nothing recognised it as the same house. The cost is
    not cosmetic: the duplicate is scored separately and appears twice in the
    ranking, and it has been paid for twice at the vision API, because
    visual_scores is keyed on listing_id and knows nothing about addresses.

    Found by accident during unrelated work, which is the reason this check
    exists: nothing was watching for it.
    """
    groups = duplicate_address_groups(conn)
    if not groups:
        return None
    return Violation(
        "duplicate_addresses",
        f"{len(groups)} address(es) held by more than one listing",
        [f"{address}: {', '.join(ids)}" for address, ids in groups],
    )


def check_properties_are_unique(conn) -> Violation | None:
    """One property, one row -- checked against Compass's own property id.

    The stricter twin of check_addresses_are_unique, and it catches what that
    one cannot: a relist where Compass re-entered the address text, so the
    two rows never grouped by address at all and the duplicate is invisible
    to every address-based check.

    It also cannot produce that check's one false positive. A duplex shares
    an address without being one property, and the address check has to fail
    on it; two rows sharing a PROPERTY id are the same house by Compass's own
    reckoning, with no ambiguity left.

    Silent until property ids have been resolved, which is correct: nothing
    is asserted about listings we have not looked up.
    """
    groups = duplicate_property_groups(conn)
    if not groups:
        return None
    return Violation(
        "duplicate_properties",
        f"{len(groups)} propert(ies) held by more than one listing",
        [f"{property_id}: {', '.join(ids)}" for property_id, ids in groups],
    )


def check_every_listing_has_a_commute_row(conn) -> Violation | None:
    """The commutes stage writes a row for every listing it attempts,
    including the ones that fail. A listing with no row at all therefore
    means the stage never reached it -- it exited early, or the selector did
    not return it -- and that listing is now being ranked on the neutral
    fallback for the heaviest-weighted factor in the rubric.

    Distinct from a row with NULL minutes, which is an honest recorded
    failure and is allowed. This is about a listing nothing even tried.
    """
    rows = _rows(
        conn,
        """
        SELECT l.listing_id, l.address FROM listings l
        LEFT JOIN commute c ON c.listing_id = l.listing_id
        WHERE c.listing_id IS NULL
        ORDER BY l.listing_id
        """,
    )
    if not rows:
        return None
    return Violation(
        "listings_without_a_commute_row",
        f"{len(rows)} listing(s) the commutes stage never wrote a result for",
        [f"{row['listing_id']} {row['address']}" for row in rows],
    )


def check_commutes_share_one_source(conn) -> Violation | None:
    """Every routed commute must have been measured the same way.

    This is the invariant the whole provenance column exists for. A corpus
    holding both free-flow and rush-hour durations ranks the un-recomputed
    rows above the recomputed ones -- an 18-minute free-flow drive scores
    100 where the same drive at 8:15 scores 88 -- and every symptom of it is
    invisible: the rows are complete, the numbers are plausible, and the run
    exits 0.

    It is also the check that would catch a revoked Mapbox key. We use
    Mapbox knowingly against its terms (docs/routing-provider-terms.md), so
    the realistic failure is not a lawsuit, it is a key switched off at 3am;
    after which new listings never get a current-source row and the ranking
    goes quietly wrong for exactly the newest listings.

    Rows with NULL minutes do not count: they routed nothing, so they claim
    nothing about how they were measured.
    """
    rows = _rows(
        conn,
        """
        SELECT commute_source, COUNT(*) AS n FROM commute
        WHERE medtronic_minutes IS NOT NULL
        GROUP BY commute_source
        """,
    )
    if not rows:
        return None
    sources = {row["commute_source"]: row["n"] for row in rows}
    if list(sources) == [COMMUTE_SOURCE]:
        return None
    detail = (
        "the corpus holds more than one kind of commute measurement"
        if len(sources) > 1
        else f"every commute was measured as {list(sources)[0]!r}, not {COMMUTE_SOURCE!r}"
    )
    return Violation(
        "mixed_commute_sources",
        detail,
        [f"{source!r}: {count} row(s)" for source, count in sorted(
            sources.items(), key=lambda item: (item[0] is not None, item[0])
        )],
    )


CHECKS: tuple[Callable, ...] = (
    check_corpus_is_not_empty,
    check_active_listings_have_photos,
    check_every_listing_is_scored,
    check_no_orphaned_children,
    check_addresses_are_unique,
    check_properties_are_unique,
    check_every_listing_has_a_commute_row,
    check_commutes_share_one_source,
)


def run_checks(conn, checks: tuple[Callable, ...] = CHECKS) -> list[Violation]:
    found = []
    for check in checks:
        violation = check(conn)
        name = getattr(check, "__name__", str(check))
        if violation is None:
            print(f"  ok    {name}")
        else:
            print(f"  FAIL  {name}: {violation.detail}")
            for row in violation.rows[:10]:
                print(f"          {row}")
            if len(violation.rows) > 10:
                print(f"          ... and {len(violation.rows) - 10} more")
            found.append(violation)
    return found


def main(argv: list[str] | None = None, conn=None) -> int:
    args = sys.argv[1:] if argv is None else argv
    warn_only = "--warn" in args

    print("verify: asserting pipeline invariants")
    violations = run_checks(conn if conn is not None else stage_connection())

    if not violations:
        print("verify: all invariants hold")
        return 0

    print(f"verify: {len(violations)} invariant(s) violated")
    if warn_only:
        print("verify: --warn given, not failing the run")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
