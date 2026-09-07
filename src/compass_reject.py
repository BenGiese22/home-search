"""Telling Compass what Ben already told us.

The rejection lives in our database first (#90) -- that is the half that was
destructive to get wrong, and it is the half that survives a relist, because
Compass keys its own record on the LISTING id and we key ours on the property.
This module is the convenience half: keeping Compass consistent so the two do
not drift.

Captured from devtools on 2026-09-07 and recorded in
docs/journal/decisions.md:

    PUT /api/v3/collections/commands/listings/not_interested
    PUT /api/v3/collections/commands/listings/unmark_not_interested

    {"collectionIdToListingIds": {"<collectionId>": ["<listingId>", ...]}}

    200 {}

Three properties of that shape drive everything here.

**The body takes an array.** A malformed loop could mark the whole collection
in one request, so there is a hard cap and it refuses rather than truncating.
Truncating would do part of the damage and report success.

**The response is `{}`.** No echo, no state. A 200 means "accepted", not
"the listing moved" -- which is exactly the failure this project keeps
producing. So nothing here claims success; it returns the ids it sent, and
the caller verifies them against the next collection fetch.

**The collection is shared.** `updatedBy` stamps every change as Ben, and the
agent and Megan both see it. That is why the cap is small and why a refusal
is preferred to a best effort.
"""

from typing import Callable

_BASE = "https://www.compass.com/api/v3/collections/commands/listings"
NOT_INTERESTED_URL = f"{_BASE}/not_interested"
UNMARK_URL = f"{_BASE}/unmark_not_interested"

# Small on purpose. Rejections arrive one at a time from a person clicking a
# button, so a run with more than a handful outstanding means something is
# wrong with us rather than with Ben's opinions -- and this writes to a
# collection two other people curate.
MAX_PER_RUN = 3

# (status, body). `put` is injected so tests never reach the network, and so
# the caller decides whether the request goes through Playwright's
# authenticated context or plain requests.
PutJson = Callable[[str, dict], tuple[int, object]]


class CompassWriteRefused(RuntimeError):
    """The write did not happen. Never raised after a successful one."""


def _send(url: str, collection_id: str, listing_ids, put: PutJson) -> list[str]:
    ids = list(listing_ids)
    if not ids:
        return []
    if not collection_id:
        raise CompassWriteRefused(
            "refusing to write with no collection id: the payload would name "
            "no collection, and what Compass does with that is not something "
            "to discover in production"
        )
    if len(ids) > MAX_PER_RUN:
        raise CompassWriteRefused(
            f"refusing to write {len(ids)} listings in one run, cap is "
            f"{MAX_PER_RUN}. The body takes an array, so a loop that has gone "
            "wrong marks the whole collection in a single request."
        )

    payload = {"collectionIdToListingIds": {collection_id: ids}}
    try:
        status, _body = put(url, payload)
    except Exception as exc:  # noqa: BLE001 -- re-raised as a refusal
        raise CompassWriteRefused(f"{type(exc).__name__}: {exc}") from exc

    if not 200 <= status < 300:
        raise CompassWriteRefused(f"HTTP {status} from {url}")

    # Deliberately not "it worked". The response is {} -- the caller checks
    # the next collection fetch for absence.
    return ids


def mark_not_interested(collection_id: str, listing_ids, put: PutJson) -> list[str]:
    """Move listings into Compass's notInterested bucket. Returns the ids sent."""
    return _send(NOT_INTERESTED_URL, collection_id, listing_ids, put)


def unmark_not_interested(collection_id: str, listing_ids, put: PutJson) -> list[str]:
    """The rollback, differing from the above by one word in the URL.

    Kept beside it rather than tucked into an ops script: a write whose undo
    lives somewhere else is a write nobody reaches for in a hurry.
    """
    return _send(UNMARK_URL, collection_id, listing_ids, put)


def plan_sync(pending) -> tuple[dict[str, str], dict[str, str]]:
    """Split the rejections awaiting Compass into what to send now and what
    to leave for the next run.

    `pending` is what rejections_pending_compass_sync returns: mappings with
    `property_id` and `listing_ref`, oldest first.

    Returns `({property_id: listing_id}, {property_id: why_not})`.

    Two reasons to defer. A rejection with no listing id has nothing to send
    -- Compass keys notInterested on the listing, and a rejection recorded
    before that was captured has only the property. And a backlog past
    MAX_PER_RUN goes out over several runs rather than in one request: the
    cap exists to bound the damage of a loop gone wrong, and if it also
    refused a genuine backlog, that backlog would never sync at all. The
    deferred ones stay pending, so this is a rate limit, not a drop.
    """
    to_send: dict[str, str] = {}
    skipped: dict[str, str] = {}
    for row in pending:
        property_id = row["property_id"]
        listing_ref = row["listing_ref"]
        if not listing_ref:
            skipped[property_id] = "no Compass listing id recorded"
        elif len(to_send) >= MAX_PER_RUN:
            skipped[property_id] = f"over the {MAX_PER_RUN}-per-run cap"
        else:
            to_send[property_id] = listing_ref
    return to_send, skipped


def confirm_sync(sent, not_interested_ids, fetched_ids):
    """What Compass actually did with the ids we sent.

    Returns `(confirmed, still_present, absent)`, all lists of property ids.

    This used to confirm on ABSENCE from the collection fetch -- a listing
    that had left filters 0 and 1 was taken to have moved. That was the
    weakest available evidence dressed up as proof, and it produced exactly
    the failure it was written to prevent: 5012 West 77th Drive was stamped
    synced on 2026-09-07 while the id we sent was in no filter at all.

    The pile is readable (listingsFilter 2, probed the same day the wrong
    number in config.py was found), so the check is now positive: the id we
    sent is IN the discarded pile.

    - `confirmed`   -- in the pile. Compass moved it. Stop.
    - `still_present` -- back in filters 0/1. The 200 meant "accepted", not
      "moved". Stays pending; retrying costs one request next run.
    - `absent`      -- in none of them. Compass does not hold this listing id,
      so re-sending it will never do anything. Reported separately rather
      than folded into `confirmed`, because "we could not find it" and
      "Compass moved it" are different facts and only one of them is good
      news. 5012 was in the pile under an older, Expired listing id for the
      same address -- the right outcome by accident, which is precisely why
      it must not be recorded as a confirmation.
    """
    confirmed, still_present, absent = [], [], []
    for property_id, listing_id in sent.items():
        if listing_id in not_interested_ids:
            confirmed.append(property_id)
        elif listing_id in fetched_ids:
            still_present.append(property_id)
        else:
            absent.append(property_id)
    return confirmed, still_present, absent
# Headers Compass's own collection app sends with this command, minus the
# ones Playwright fills in. `x-hydra-app-name` names the front-end making the
# call; origin and referer are what the browser would attach and are not set
# automatically by page.request. Cookies come from the authenticated session
# -- there is no CSRF token in the captured request, which is the thing this
# was replayed to confirm.
def build_put_json(page, collection_url: str) -> PutJson:
    """A PutJson backed by an authenticated Playwright page.

    Goes through page.request rather than a standalone client for the same
    reason photo downloads do: it reuses the real session's cookies and TLS
    fingerprint, so a write looks like the collection app being used.
    """

    def put_json(url: str, payload: dict) -> tuple[int, object]:
        response = page.request.put(
            url,
            data=payload,
            headers={
                "content-type": "application/json",
                "origin": "https://www.compass.com",
                "referer": collection_url,
                "x-hydra-app-name": "collection",
            },
        )
        return response.status, None

    return put_json
