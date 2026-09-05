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

from src.config import load_env  # noqa: F401  (kept for .env side effects)
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


CHECKS: tuple[Callable, ...] = (
    check_corpus_is_not_empty,
    check_active_listings_have_photos,
    check_every_listing_is_scored,
    check_no_orphaned_children,
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
