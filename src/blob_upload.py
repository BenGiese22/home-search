from pathlib import Path
from typing import Callable
from urllib.parse import urlencode

import requests

# The Vercel Blob upload endpoint, its API version, and the header names below
# are @vercel/blob's internal contract, not the public REST API -- they are not
# in the published docs. Read out of @vercel/blob 2.8.0 as bundled with the
# installed Vercel CLI (dist/chunk-YYMLUMXS.js): `defaultVercelBlobApiUrl`,
# `BLOB_API_VERSION`, `createPutHeaders`, `createPutMethod`, and
# `parseStoreIdFromReadWriteToken`. If uploads ever start failing with a
# version error, re-read those symbols from the current SDK rather than
# guessing at the bump.
BLOB_API_URL = "https://vercel.com/api/blob"
BLOB_API_VERSION = "12"

# Connect timeout, then read timeout. Without one a stalled upload hangs the
# whole publish indefinitely, holding the pipeline flock behind it.
UPLOAD_TIMEOUT_SECONDS = (10, 120)


def already_uploaded(conn, listing_id: str, position: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM hosted_photos WHERE listing_id = ? AND position = ?",
        (listing_id, position),
    ).fetchone()
    return row is not None


def _store_id_from_token(rw_token: str) -> str:
    """A read-write token is `vercel_blob_rw_<storeId>_<secret>`; the store id
    is the fourth underscore-separated field. Matches the SDK's
    parseStoreIdFromReadWriteToken exactly, including returning "" rather than
    raising on a malformed token -- the API's own error is more useful than a
    guess made here."""
    parts = rw_token.split("_")
    return parts[3] if len(parts) > 3 else ""


def upload_photo(
    local_path: Path,
    listing_id: str,
    position: int,
    rw_token: str,
    put: Callable = requests.put,
) -> str:
    """Uploads one photo to Vercel Blob with a single-part REST PUT and
    returns its public URL.

    Single-part is the billing-critical part. The `vercel` CLI this replaced
    defaulted to `--multipart true`, which splits an upload into start +
    one-per-part + complete, and Vercel bills EACH as an Advanced Operation.
    A 3,000-photo backfill cost 11,000 operations against a 2,000/month
    budget and suspended the store for 30 days. One PUT is one operation.

    `put` is injected so tests never touch the network, the same seam the
    subprocess version used for `run`.

    Raises RuntimeError for every failure -- bad status, transport error, or a
    success response carrying no URL -- so callers have one exception type to
    catch. publish.py depends on that: a failed upload must record no
    hosted_photos row so a rerun retries it.
    """
    pathname = f"photos/{listing_id}/{position:02d}.jpg"
    # Read before requesting: a missing file must not burn an operation.
    body = local_path.read_bytes()

    url = f"{BLOB_API_URL}/?{urlencode({'pathname': pathname})}"
    headers = {
        "authorization": f"Bearer {rw_token}",
        "x-api-version": BLOB_API_VERSION,
        # Store id is not encoded in every token kind, so the SDK always
        # passes it separately.
        "x-vercel-blob-store-id": _store_id_from_token(rw_token),
        # public matches the viewer's plain <img>/next/image usage -- nothing
        # in this project is sensitive at the photo level.
        "x-vercel-blob-access": "public",
        # Makes a rerun safe after a listing's photo changes.
        "x-allow-overwrite": "1",
        "x-content-type": "image/jpeg",
    }

    try:
        response = put(
            url, data=body, headers=headers, timeout=UPLOAD_TIMEOUT_SECONDS
        )
    except requests.RequestException as exc:
        raise RuntimeError(
            f"blob upload failed for {local_path} ({type(exc).__name__}: {exc})"
        ) from exc

    if not 200 <= response.status_code < 300:
        raise RuntimeError(
            f"blob upload failed for {local_path} "
            f"(HTTP {response.status_code}): {response.text[:200]}"
        )

    # Refuse to return an empty string. That was the real failure mode with
    # the CLI: uploads succeeded, ~200 rows were written with a blank
    # blob_url, and every image silently broke instead of failing visibly.
    try:
        blob_url = response.json().get("url")
    except Exception:
        blob_url = None
    if blob_url:
        return blob_url
    raise RuntimeError(
        f"blob upload reported success for {local_path} but no blob URL "
        f"appeared in its response (body={response.text[:200]!r})"
    )
