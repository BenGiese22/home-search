"""Small state files that must survive between ephemeral runs.

Phase 3 runs the pipeline in a sandbox with no persistent disk, so two files
have to live somewhere durable:

- `compass_state.json` -- the Compass session. Losing it forces a cold
  login, which works but is exactly what the warm-session-first design
  exists to keep rare.
- `.photo_scoring_batch_state.json` -- the vision batch checkpoint. Losing
  this one breaks the never-pay-twice guarantee and costs real money.

Stored with **private** access. A Compass session behind a public URL is a
credential anyone holding the link can use; `photos/` is public because a
listing photo is not sensitive, and these two must never share that
namespace.
"""
from typing import Callable
from urllib.parse import urlencode

import requests

from src.blob_upload import (
    BLOB_API_URL,
    BLOB_API_VERSION,
    _store_id_from_token,
)

# Kept apart from `photos/`, which is uploaded with public access.
STATE_PREFIX = "state/"

STATE_TIMEOUT_SECONDS = (10, 30)


def state_blob_url(name: str, rw_token: str) -> str:
    """The private read URL for a state file. Private blobs are served from
    `<storeId>.private.blob.vercel-storage.com` and require the token on the
    GET -- verified against @vercel/blob's constructBlobUrl.

    The store id is lowercased. A read-write token carries it in mixed case
    (`vercel_blob_rw_Ie9RPNHVTObyiwOt_...`) while the host is all lowercase
    (`ie9rpnhvtobyiwot.public.blob.vercel-storage.com`), confirmed against
    both the public photo store and the private state store. DNS would
    forgive the mismatch, but a URL compared as a string would not.
    """
    store_id = _store_id_from_token(rw_token).lower()
    return (
        f"https://{store_id}.private.blob.vercel-storage.com/"
        f"{STATE_PREFIX}{name}"
    )


def put_state(
    name: str, data: bytes, rw_token: str, put: Callable = requests.put
) -> str:
    """Uploads (or replaces) one state file. Same single-part REST PUT as
    photo upload, with private access."""
    pathname = f"{STATE_PREFIX}{name}"
    url = f"{BLOB_API_URL}/?{urlencode({'pathname': pathname})}"
    headers = {
        "authorization": f"Bearer {rw_token}",
        "x-api-version": BLOB_API_VERSION,
        "x-vercel-blob-store-id": _store_id_from_token(rw_token),
        "x-vercel-blob-access": "private",
        "x-allow-overwrite": "1",
        "x-content-type": "application/json",
    }
    try:
        response = put(url, data=data, headers=headers, timeout=STATE_TIMEOUT_SECONDS)
    except requests.RequestException as exc:
        raise RuntimeError(
            f"blob state upload failed for {name} ({type(exc).__name__}: {exc})"
        ) from exc

    if not 200 <= response.status_code < 300:
        raise RuntimeError(
            f"blob state upload failed for {name} "
            f"(HTTP {response.status_code}): {response.text[:200]}"
        )
    try:
        return response.json().get("url") or ""
    except Exception:
        return ""


def get_state(
    name: str, rw_token: str, get: Callable = requests.get
) -> bytes | None:
    """Downloads one state file, or None when it does not exist yet.

    404 is a normal first-run condition -- nothing has been uploaded, so the
    caller falls back (a cold login, or an empty checkpoint). Every other
    error raises: silently treating a 403 as "no session" would trigger a
    needless cold login on every run and hide a broken token indefinitely.
    """
    try:
        response = get(
            state_blob_url(name, rw_token),
            headers={"authorization": f"Bearer {rw_token}"},
            timeout=STATE_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        raise RuntimeError(
            f"blob state download failed for {name} ({type(exc).__name__}: {exc})"
        ) from exc

    if response.status_code == 404:
        return None
    if not 200 <= response.status_code < 300:
        raise RuntimeError(
            f"blob state download failed for {name} "
            f"(HTTP {response.status_code}): {response.text[:200]}"
        )
    return response.content
