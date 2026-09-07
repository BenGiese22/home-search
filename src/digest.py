"""Turning a run's changes into an email worth opening.

Two decisions live here, and neither is about formatting.

**Whether to send at all.** Ben chose what interrupts: a new listing does, a
price change or a delisting does not. So an email goes out when there is at
least one new listing -- everything else rides along in it. Without a second
rule that would mean a price drop waits indefinitely for an unrelated house
to appear, so unsent changes also force a send once they are a week old. A
digest nobody asked for trains people to ignore the one they did.

**What a listing is worth saying about.** An address and a price are not
enough to decide whether to look: the whole point of the rubric is that it
has already ranked this house against the other hundred. So the digest
carries rank and composite, which is why it runs after `score` rather than
inside `scrape`.

Pure. The caller supplies the rows; nothing here opens a database or sends
anything.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from src.db import KIND_DELISTED, KIND_NEW, KIND_PRICE

# How long an unsent change waits for a new listing to carry it before it
# forces a send on its own. Long enough that a quiet week stays quiet; short
# enough that a price drop on a house being watched does not go unseen.
STALE_AFTER = timedelta(days=7)


@dataclass(frozen=True)
class Digest:
    subject: str
    body: str
    event_ids: list[str]


def _price_line(detail: str | None) -> str:
    return detail or "price changed"


def should_send(events, now: datetime) -> bool:
    """Whether this set of changes is worth an email.

    A new listing always is. Anything else waits to be carried by one, unless
    it has waited a week.
    """
    if not events:
        return False
    if any(e["kind"] == KIND_NEW for e in events):
        return True
    oldest = min(e["detected_at"] for e in events)
    try:
        detected = datetime.fromisoformat(oldest)
    except (TypeError, ValueError):
        # An unreadable timestamp must not suppress a send forever. Erring
        # toward one extra email is the cheap direction.
        return True
    if detected.tzinfo is None:
        detected = detected.replace(tzinfo=timezone.utc)
    return now - detected >= STALE_AFTER


def _subject(counts: dict[str, int]) -> str:
    parts = []
    if counts.get(KIND_NEW):
        n = counts[KIND_NEW]
        parts.append(f"{n} new listing{'s' if n != 1 else ''}")
    if counts.get(KIND_PRICE):
        n = counts[KIND_PRICE]
        parts.append(f"{n} price change{'s' if n != 1 else ''}")
    if counts.get(KIND_DELISTED):
        parts.append(f"{counts[KIND_DELISTED]} delisted")
    return ", ".join(parts) if parts else "no changes"


def compose(events, listings_by_id, ranks_by_id, total, site_url="") -> Digest:
    """Build the digest. `listings_by_id` and `ranks_by_id` may be missing an
    id -- a delisted listing has no row left, and a brand-new one may not be
    scored yet -- so every lookup degrades to what is known rather than
    dropping the line. A change we cannot describe fully is still a change
    worth reporting.
    """
    counts: dict[str, int] = {}
    for event in events:
        counts[event["kind"]] = counts.get(event["kind"], 0) + 1

    sections: dict[str, list[str]] = {KIND_NEW: [], KIND_PRICE: [], KIND_DELISTED: []}
    for event in events:
        listing_id = event["listing_ref"]
        row = listings_by_id.get(listing_id)
        rank = ranks_by_id.get(listing_id)
        address = row["address"] if row else (event["detail"] or listing_id)
        city = f", {row['city']}" if row else ""
        lines = []

        if event["kind"] == KIND_NEW:
            place = f"ranked #{rank[0]} of {total}" if rank else "not scored yet"
            lines.append(f"NEW  ·  {place}")
            lines.append(f"{address}{city}")
            if row:
                lines.append(
                    f"{row['price']} · {row['beds']}bd {row['baths']}ba · {row['sqft']:,} sqft"
                )
            if rank:
                lines.append(f"composite {rank[1]:.1f}")
        elif event["kind"] == KIND_PRICE:
            place = f"ranked #{rank[0]}" if rank else "unranked"
            lines.append(f"PRICE  ·  {place}")
            lines.append(f"{address}{city}")
            lines.append(_price_line(event["detail"]))
        else:
            lines.append(f"{address}{city}")
            lines.append("gone from the collection")

        if row and site_url:
            lines.append(f"{site_url.rstrip('/')}/listing/{listing_id}")
        sections[event["kind"]].append("\n".join(lines))

    body_parts = []
    for kind, heading in (
        (KIND_NEW, None),
        (KIND_PRICE, None),
        (KIND_DELISTED, "DELISTED"),
    ):
        if not sections[kind]:
            continue
        joined = "\n\n".join(sections[kind])
        body_parts.append(f"{heading}\n{joined}" if heading else joined)

    if site_url:
        body_parts.append(f"\nThe full ranking: {site_url.rstrip('/')}")

    return Digest(
        subject=_subject(counts),
        body="\n\n".join(body_parts).strip(),
        event_ids=[e["event_id"] for e in events],
    )
