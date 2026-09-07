"""Tell someone what changed. The last thing a run does that a person sees.

    venv/bin/python notify_changes.py [--dry-run]

For the life of this project the pipeline has detected new listings, price
changes and delistings, and reported none of them. `compute_changes()`
produced a full report and `scrape.py` used only the delisting half; the
push channel that did exist was publishing to an ntfy topic nobody had ever
subscribed to. A new listing arrived overnight on 2026-09-06 and was found
by asking, not by being told.

Runs after `score` on purpose. An address and a price are not enough to
decide whether to look at a house -- the rubric has already ranked it against
the other hundred, and that ranking is what makes the email worth opening.

**Never fails the run.** A digest is commentary on a run that has already
finished; a notifier that raises converts a successful run into a failed one,
and the notification is worth strictly less than the run it describes.
"""

import sys
from datetime import datetime, timezone

from src.config import load_env
from src.db import (
    mark_change_events_notified,
    query_listings,
    unnotified_change_events,
)
from src.digest import compose, should_send
from src.mailer import send_email
from src.turso_db import stage_connection

EXIT_OK = 0


def _ranks(conn) -> tuple[dict[str, tuple[int, float]], int]:
    """listing_id -> (rank, composite), plus the corpus size.

    Ranked here rather than read from a column because nothing stores a rank:
    `scores.composite` is the ordering, and the position in it is what a
    person actually wants to know.
    """
    rows = conn.execute(
        "SELECT listing_id, composite FROM scores ORDER BY composite DESC"
    ).fetchall()
    return (
        {row["listing_id"]: (i + 1, row["composite"]) for i, row in enumerate(rows)},
        len(rows),
    )


def run(conn, *, send, now, dry_run: bool = False) -> int:
    events = unnotified_change_events(conn)
    if not events:
        print("no unreported changes")
        return EXIT_OK

    if not should_send(events, now):
        kinds = ", ".join(sorted({e["kind"] for e in events}))
        print(f"holding {len(events)} change(s) ({kinds}) for the next new listing")
        return EXIT_OK

    ranks, total = _ranks(conn)
    listings_by_id = {row["listing_id"]: row for row in query_listings(conn)}
    digest = compose(
        events,
        listings_by_id,
        ranks,
        total=total,
        site_url=_site_url(),
    )

    if dry_run:
        print(f"--dry-run\n\nSubject: {digest.subject}\n\n{digest.body}")
        return EXIT_OK

    if send(digest.subject, digest.body):
        # Stamped only on success, so a failed send is retried next run
        # rather than silently swallowing the one email that mattered.
        mark_change_events_notified(conn, digest.event_ids)
        print(f"sent: {digest.subject}")
    else:
        print(f"could not send ({digest.subject}); will retry next run")
    return EXIT_OK


def _site_url() -> str:
    """The address a person can actually open.

    Deliberately not SHORT_LIST_URL by default. That variable exists for the
    revalidate POST, which works against any deployment alias -- so in the
    sandbox it can be a per-deployment vercel.app URL that rotates and, in a
    protected project, refuses a browser. A link in an email has to survive
    longer than the deployment that produced it.
    """
    env = load_env()
    return env.get("SITE_URL") or env.get("SHORT_LIST_URL", "")


def _default_send(subject: str, body: str) -> bool:
    env = load_env()
    return send_email(
        env.get("RESEND_API_KEY", ""),
        env.get("DIGEST_EMAIL_TO", ""),
        subject,
        body,
        sender=env.get("RESEND_FROM", "home-search <onboarding@resend.dev>"),
    )


def main() -> int:
    conn = stage_connection()
    try:
        return run(
            conn,
            send=_default_send,
            now=datetime.now(timezone.utc),
            dry_run="--dry-run" in sys.argv,
        )
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
