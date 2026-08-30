import re
import subprocess
from pathlib import Path
from typing import Callable

_BLOB_URL_RE = re.compile(r"https://\S+\.blob\.vercel-storage\.com/\S+")


def already_uploaded(conn, listing_id: str, position: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM hosted_photos WHERE listing_id = ? AND position = ?",
        (listing_id, position),
    ).fetchone()
    return row is not None


def upload_photo(
    local_path: Path,
    listing_id: str,
    position: int,
    rw_token: str,
    run: Callable = subprocess.run,
) -> str:
    """Uploads one photo via the Vercel CLI and returns the resulting public
    URL. --allow-overwrite true makes a rerun after a photo changes safe;
    --access public matches the mockup's plain <img>/next/image usage --
    nothing in this project is sensitive at the photo level.

    Raises RuntimeError (not subprocess.CalledProcessError) on a non-zero
    exit, so callers have one exception type to catch regardless of how the
    CLI failed."""
    pathname = f"photos/{listing_id}/{position:02d}.jpg"
    try:
        result = run(
            [
                "vercel", "blob", "put", str(local_path),
                "--pathname", pathname,
                "--access", "public",
                "--allow-overwrite", "true",
                # The CLI defaults --multipart to true, which splits every
                # upload into start + one-per-part + complete. Vercel bills
                # EACH of those as an Advanced Operation, so ~250KB listing
                # photos cost 3+ operations apiece instead of 1. That is what
                # burned 11K operations against a 2K free-tier budget on the
                # first real sync. Multipart is meant for files over ~100MB;
                # these are a quarter megabyte.
                "--multipart", "false",
                "--rw-token", rw_token,
            ],
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            f"vercel blob put failed for {local_path} (exit {exc.returncode}): "
            f"{exc.stderr}"
        ) from exc

    # The CLI prints "> Success! <url>" on STDERR, not stdout -- stdout comes
    # back empty. Found on the first real sync, which recorded ~200 photos
    # with a blank blob_url before anyone noticed. Search both streams, and
    # refuse to return an empty string: a blank URL silently yields broken
    # images instead of a visible failure.
    for stream in (getattr(result, "stdout", ""), getattr(result, "stderr", "")):
        match = _BLOB_URL_RE.search(stream or "")
        if match:
            return match.group(0)
    raise RuntimeError(
        f"vercel blob put reported success for {local_path} but no blob URL "
        f"appeared in its output (stdout={result.stdout!r} "
        f"stderr={(getattr(result, 'stderr', '') or '')[-200:]!r})"
    )
