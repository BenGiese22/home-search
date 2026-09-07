"""Telling Compass what Ben already told us.

The rejection lives in our database first -- that half shipped in #90 and is
the one that was destructive to get wrong. This is the convenience half:
keeping Compass consistent so the two do not drift.

Captured from devtools on 2026-09-07:

    PUT /api/v3/collections/commands/listings/not_interested
    PUT /api/v3/collections/commands/listings/unmark_not_interested
    {"collectionIdToListingIds":{"<collection>":["<listing_id>"]}}
    -> 200 {}

The empty response is why almost every test here is about verification rather
than about the request.
"""

import pytest

from src.compass_reject import (
    MAX_PER_RUN,
    NOT_INTERESTED_URL,
    UNMARK_URL,
    CompassWriteRefused,
    confirm_sync,
    mark_not_interested,
    plan_sync,
    unmark_not_interested,
)

COLLECTION = "6a27426b698343000129b139"


def recorder(status=200, raises=None):
    def put(url, json):
        put.calls.append({"url": url, "json": json})
        if raises:
            raise raises
        return status, {}

    put.calls = []
    return put


# --- the request ----------------------------------------------------------


def test_the_request_matches_what_compass_sends():
    put = recorder()
    mark_not_interested(COLLECTION, ["2170507642002070233"], put)

    assert put.calls[0]["url"] == NOT_INTERESTED_URL
    assert put.calls[0]["json"] == {
        "collectionIdToListingIds": {COLLECTION: ["2170507642002070233"]}
    }


def test_the_undo_differs_only_by_one_word():
    put = recorder()
    unmark_not_interested(COLLECTION, ["2170507642002070233"], put)

    assert put.calls[0]["url"] == UNMARK_URL
    assert put.calls[0]["json"]["collectionIdToListingIds"][COLLECTION] == [
        "2170507642002070233"
    ]


def test_nothing_to_send_makes_no_request():
    put = recorder()
    assert mark_not_interested(COLLECTION, [], put) == []
    assert put.calls == []


# --- the cap --------------------------------------------------------------


def test_more_than_the_cap_is_refused_outright():
    """The body takes an ARRAY, so a malformed loop could mark the entire
    collection in one request. Refusing is the only defence that works before
    the damage rather than after it."""
    put = recorder()
    with pytest.raises(CompassWriteRefused, match="cap"):
        mark_not_interested(COLLECTION, [str(i) for i in range(MAX_PER_RUN + 1)], put)
    assert put.calls == []


def test_exactly_the_cap_is_allowed():
    put = recorder()
    mark_not_interested(COLLECTION, [str(i) for i in range(MAX_PER_RUN)], put)
    assert len(put.calls) == 1


def test_a_missing_collection_id_is_refused():
    """Without it the payload names no collection, and what Compass would do
    with that is not something to discover in production."""
    put = recorder()
    with pytest.raises(CompassWriteRefused):
        mark_not_interested("", ["L1"], put)
    assert put.calls == []


# --- what a 200 does and does not mean ------------------------------------


def test_a_non_2xx_is_reported_not_swallowed():
    put = recorder(status=403)
    with pytest.raises(CompassWriteRefused, match="403"):
        mark_not_interested(COLLECTION, ["L1"], put)


def test_a_transport_failure_is_reported():
    put = recorder(raises=RuntimeError("connection reset"))
    with pytest.raises(CompassWriteRefused, match="connection reset"):
        mark_not_interested(COLLECTION, ["L1"], put)


def test_a_success_returns_the_ids_it_claims_to_have_sent():
    """Deliberately NOT "it worked". The response is {} -- no echo, no state --
    so a 200 means the request was accepted, not that the listing moved. The
    caller verifies against the next collection fetch."""
    put = recorder()
    assert mark_not_interested(COLLECTION, ["L1", "L2"], put) == ["L1", "L2"]


# --- deciding what to send, and what to believe afterwards --------------
#
# The write itself is one request. Everything below is about the two things
# that request cannot tell us: which rejections still need it, and whether
# the one we sent actually took.


def pending(*rows):
    """Rows as rejections_pending_compass_sync returns them, oldest first."""
    return [
        {"property_id": pid, "listing_ref": ref, "address": f"{pid} St"}
        for pid, ref in rows
    ]


def test_a_rejection_with_no_listing_id_is_skipped_not_guessed():
    """Compass keys not_interested on the LISTING id. A rejection made
    against a house we never held a listing for -- or one recorded before
    this column existed -- has nothing to send, and inventing an id would
    mark a stranger's house."""
    to_send, skipped = plan_sync(pending(("131FZM", None)))

    assert to_send == {}
    assert "131FZM" in skipped


def test_a_backlog_is_rate_limited_rather_than_refused():
    """Five rejections in one evening is a normal Friday, not a bug. The cap
    in _send guards against a loop gone wrong; if it also stopped a genuine
    backlog, that backlog would never sync at all -- and the un-sent ones
    stay pending, so the next run picks them up."""
    rows = pending(*[(f"P{i}", f"L{i}") for i in range(5)])

    to_send, skipped = plan_sync(rows)

    assert len(to_send) == MAX_PER_RUN
    # Oldest first: rejections are returned in rejected_at order, and the
    # ones that have waited longest go first.
    assert set(to_send) == {"P0", "P1", "P2"}
    assert set(skipped) == {"P3", "P4"}


def test_nothing_pending_plans_nothing():
    assert plan_sync([]) == ({}, {})


def test_a_listing_absent_from_the_next_fetch_is_confirmed():
    """The whole verification. Marking a listing notInterested moves it out
    of filters 0 and 1 -- the only two we fetch -- so a rejected listing that
    is no longer in the fetch is one Compass has acted on."""
    confirmed, unconfirmed = confirm_sync({"131FZM": "L1"}, fetched_ids={"L2", "L3"})

    assert confirmed == ["131FZM"]
    assert unconfirmed == []


def test_a_listing_still_in_the_next_fetch_is_not_confirmed():
    """A 200 with an empty body means "accepted", not "moved". This is the
    case that separates the two, and it must not be recorded as synced --
    leaving it pending costs one retry next run."""
    confirmed, unconfirmed = confirm_sync({"131FZM": "L1"}, fetched_ids={"L1", "L2"})

    assert confirmed == []
    assert unconfirmed == ["131FZM"]


def test_confirmation_reads_the_fetch_not_the_response():
    """Nothing sent, nothing to confirm -- even against an empty fetch, which
    would otherwise look like every rejection succeeding at once."""
    assert confirm_sync({}, fetched_ids=set()) == ([], [])
