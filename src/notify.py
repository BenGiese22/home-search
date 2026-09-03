"""Telling someone the run failed.

The pipeline runs unattended: on a systemd timer today, on a cron in a
sandbox under Phase 3. A run that dies at 3am with nobody watching is
indistinguishable from a run that never fired -- that gap is what this
closes, and it is the one piece of Phase 3 worth having even if the rest is
never built.

ntfy.sh needs no account and no credential: the topic name IS the address,
which is why the topic should be an unguessable string rather than
"home-search". Anyone who knows it can publish to it. Nothing secret is ever
put in a notification body for the same reason.

Every failure path here is swallowed. Notifying is commentary on a run that
has already succeeded or already failed; a notifier that raises converts a
reported failure into a different and less informative one, and a notifier
that hangs holds the pipeline's flock open behind it.
"""
from typing import Callable, Sequence

import requests

NTFY_BASE_URL = "https://ntfy.sh"

# Short on purpose. This runs at the end of a stage, so a slow notification
# delays the whole pipeline, and the message is worth strictly less than the
# run it describes.
NTFY_TIMEOUT_SECONDS = 5


def notify(
    topic: str,
    title: str,
    message: str,
    *,
    priority: str = "default",
    tags: Sequence[str] = (),
    post: Callable = requests.post,
) -> bool:
    """Publish one notification. Returns True only if it was delivered.

    An empty or whitespace-only topic is a silent no-op returning False:
    notification is opt-in, and with NTFY_TOPIC unset the pipeline must
    behave exactly as it does today rather than printing a warning on every
    run.

    `post` is injected so tests never reach the network, the same seam
    src/blob_upload.py and src/revalidate.py already use.
    """
    if not topic or not topic.strip():
        return False

    headers = {"Title": title, "Priority": priority}
    if tags:
        # An empty Tags header renders as a stray blank tag in the client.
        headers["Tags"] = ",".join(tags)

    try:
        response = post(
            f"{NTFY_BASE_URL}/{topic.strip()}",
            data=message.encode("utf-8"),
            headers=headers,
            timeout=NTFY_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        # Deliberately broader than requests' own hierarchy: this is often
        # the last thing a script does before exiting, so there is nothing
        # above it to catch whatever it lets through.
        print(f"warning: notification failed ({type(exc).__name__}: {exc})")
        return False

    if not 200 <= response.status_code < 300:
        print(
            f"warning: notification rejected (HTTP {response.status_code}): "
            f"{response.text[:120]}"
        )
        return False
    return True
