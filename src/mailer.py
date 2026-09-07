"""Sending one email, and failing quietly when it cannot.

The pipeline runs unattended. A run that finds a new listing at 3am has to
reach a person, and until now it did not: the ntfy path this replaces was
publishing to a topic nobody had ever subscribed to, so every failure alert
and every canary result for the life of the project went into a void.

Email rather than push because of what the message has to carry. A new
listing is an address, a price, a rank, and a link -- content a push
notification truncates and an inbox does not. Volume is a handful a week, so
nothing here needs batching or a queue.

Resend is the transport: a REST call with a bearer token, no SDK, and a free
tier far above what this sends. `post` is injected so tests never reach the
network -- the same seam src/notify.py, src/blob_upload.py and
src/revalidate.py already use.

**Every failure path is swallowed.** Notifying is commentary on a run that
has already succeeded or already failed. A notifier that raises converts a
reported outcome into a different and less informative one, and a notifier
that hangs holds the pipeline's lease open behind it.
"""

import json
from typing import Callable

import requests

RESEND_URL = "https://api.resend.com/emails"

# Short on purpose: this runs at the end of a stage, and the message is worth
# strictly less than the run it describes.
TIMEOUT_SECONDS = 10


def send_email(
    api_key: str,
    to: str,
    subject: str,
    body: str,
    *,
    sender: str = "home-search <onboarding@resend.dev>",
    post: Callable = requests.post,
) -> bool:
    """Send one plain-text email. Returns True only if Resend accepted it.

    A missing key or recipient is a silent no-op returning False. Notification
    is opt-in, and with the variables unset the pipeline must behave exactly
    as it did before this existed rather than printing a warning every run.

    The default sender is Resend's shared onboarding domain, which delivers
    only to the address that owns the Resend account. That is the whole
    audience here, so it works with no DNS setup -- set RESEND_FROM to a
    verified domain if that ever stops being true.
    """
    if not api_key or not api_key.strip() or not to or not to.strip():
        return False

    try:
        response = post(
            RESEND_URL,
            headers={
                "Authorization": f"Bearer {api_key.strip()}",
                "Content-Type": "application/json",
            },
            data=json.dumps(
                {"from": sender, "to": [to.strip()], "subject": subject, "text": body}
            ),
            timeout=TIMEOUT_SECONDS,
        )
    except Exception as exc:
        # Deliberately broader than requests' own hierarchy: this is often the
        # last thing a stage does, so there is nothing above it to catch
        # whatever it lets through.
        print(f"warning: email failed ({type(exc).__name__}: {exc})")
        return False

    if not 200 <= response.status_code < 300:
        # The body can name a bad key or an unverified domain, which is the
        # whole diagnosis. The key itself is never in it.
        print(f"warning: email rejected (HTTP {response.status_code}): {response.text[:200]}")
        return False
    return True
