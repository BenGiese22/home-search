"""The suite must never send a real email or push. See conftest.py."""

import pipeline
import src.mailer
import src.notify


def test_send_email_without_a_post_cannot_reach_the_network():
    # A real-looking key and recipient: without the guard this is a real send.
    assert src.mailer.send_email("re_key", "ben@example.com", "s", "b") is False


def test_notify_without_a_post_cannot_reach_the_network():
    assert src.notify.notify("real-topic", "t", "m") is False


def test_the_pipeline_sees_the_guarded_senders(monkeypatch):
    monkeypatch.setattr(
        pipeline, "load_env",
        lambda: {"RESEND_API_KEY": "re_key", "DIGEST_EMAIL_TO": "ben@example.com",
                 "NTFY_TOPIC": "real-topic"},
    )
    assert pipeline._default_notify("title", "message") is False
