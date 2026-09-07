"""The digest stage: when it sends, and what happens when it cannot."""

import sqlite3
from datetime import datetime, timezone

import pytest

import notify_changes
from src.db import KIND_NEW, KIND_PRICE, record_change_events, unnotified_change_events
from src.turso_db import ensure_schema

NOW = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    ensure_schema(c)
    return c


def add(conn, lid, address="8221 West 93rd Way", composite=None):
    conn.execute(
        """INSERT INTO listings (
             listing_id, address, city, state, zip_code, price, beds, baths,
             sqft, lot_sqft, parking_spaces, year_built, description,
             listing_url, is_pinned, property_type, localized_status
           ) VALUES (?, ?, 'Westminster', 'CO', '80021', '$625,000', 3, 2.5,
                     2140, 7000, 2, 1990, 'd', 'https://x/l', 0,
                     'Single Family', 'Active')""",
        (lid, address),
    )
    if composite is not None:
        conn.execute(
            """INSERT INTO scores (
                 listing_id, commute_score, sqft_score, condition_score,
                 outdoor_score, room_count_score, parking_score, hoa_score,
                 composite, passes_filters, has_incomplete_data, computed_at
               ) VALUES (?, 50,50,50,50,50,50,50, ?, 1, 0, '2026-09-06T00:00:00+00:00')""",
            (lid, composite),
        )
    conn.commit()


def sender(ok=True):
    def send(subject, body):
        send.calls.append((subject, body))
        return ok

    send.calls = []
    return send


def test_a_new_listing_produces_an_email(conn):
    add(conn, "L1", composite=71.2)
    record_change_events(conn, [(KIND_NEW, "L1", "8221 West 93rd Way")])
    send = sender()

    assert notify_changes.run(conn, send=send, now=NOW) == 0

    subject, body = send.calls[0]
    assert subject == "1 new listing"
    assert "8221 West 93rd Way" in body


def test_a_sent_digest_is_not_sent_again(conn):
    add(conn, "L1", composite=71.2)
    record_change_events(conn, [(KIND_NEW, "L1", None)])
    send = sender()

    notify_changes.run(conn, send=send, now=NOW)
    notify_changes.run(conn, send=send, now=NOW)

    assert len(send.calls) == 1


def test_a_failed_send_leaves_the_changes_to_retry(conn):
    """The stamp is the only thing preventing a re-send, so it must not be
    applied to an email that never went out. Losing the one notification that
    mattered to a transient 500 is the failure this guards."""
    add(conn, "L1", composite=71.2)
    record_change_events(conn, [(KIND_NEW, "L1", None)])

    notify_changes.run(conn, send=sender(ok=False), now=NOW)

    assert len(unnotified_change_events(conn)) == 1


def test_a_price_change_alone_is_held_rather_than_sent(conn):
    add(conn, "L2", composite=58.2)
    record_change_events(conn, [(KIND_PRICE, "L2", "$599,000 -> $579,000")])
    send = sender()

    notify_changes.run(conn, send=send, now=NOW)

    assert send.calls == []
    assert len(unnotified_change_events(conn)) == 1


def test_nothing_to_report_sends_nothing(conn):
    send = sender()
    assert notify_changes.run(conn, send=send, now=NOW) == 0
    assert send.calls == []


def test_the_rank_is_a_position_in_the_corpus_not_a_stored_column(conn):
    """Nothing stores a rank -- composite is the ordering, and the position
    in it is what a person actually wants to know."""
    add(conn, "best", address="Best St", composite=90.0)
    add(conn, "mid", address="Mid St", composite=70.0)
    add(conn, "L1", composite=50.0)
    record_change_events(conn, [(KIND_NEW, "L1", None)])
    send = sender()

    notify_changes.run(conn, send=send, now=NOW)

    assert "ranked #3 of 3" in send.calls[0][1]


def test_a_dry_run_sends_nothing_and_stamps_nothing(conn, capsys):
    add(conn, "L1", composite=71.2)
    record_change_events(conn, [(KIND_NEW, "L1", None)])
    send = sender()

    notify_changes.run(conn, send=send, now=NOW, dry_run=True)

    assert send.calls == []
    assert len(unnotified_change_events(conn)) == 1
    assert "Subject: 1 new listing" in capsys.readouterr().out


def test_the_stage_never_fails_the_run(conn):
    """A digest is commentary on a run that already finished. A notifier that
    fails the run converts a good run into a bad one."""
    add(conn, "L1", composite=71.2)
    record_change_events(conn, [(KIND_NEW, "L1", None)])

    assert notify_changes.run(conn, send=sender(ok=False), now=NOW) == 0


def test_the_link_prefers_a_stable_site_url_over_the_revalidate_target(monkeypatch):
    """SHORT_LIST_URL exists for the revalidate POST and works against any
    deployment alias -- in the sandbox it can be a per-deployment vercel.app
    URL that rotates. A link in an email has to outlive the deployment."""
    monkeypatch.setattr(
        notify_changes, "load_env",
        lambda: {"SITE_URL": "https://short-list.bgiese.tech",
                 "SHORT_LIST_URL": "https://short-list-abc123.vercel.app"},
    )
    assert notify_changes._site_url() == "https://short-list.bgiese.tech"


def test_the_revalidate_url_is_used_when_no_stable_one_is_set(monkeypatch):
    monkeypatch.setattr(
        notify_changes, "load_env",
        lambda: {"SHORT_LIST_URL": "https://short-list-abc123.vercel.app"},
    )
    assert notify_changes._site_url() == "https://short-list-abc123.vercel.app"
