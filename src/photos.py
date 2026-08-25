import shutil
from pathlib import Path
from typing import Callable


def download_photos(
    photo_urls: list[str],
    dest_dir: Path,
    fetch_bytes: Callable[[str], bytes],
    sleep_fn: Callable[[], None] | None = None,
) -> list[Path]:
    """Download each photo to dest_dir/NN.jpg, skipping files that already
    exist. A failure fetching one photo is logged and skipped rather than
    aborting the rest of the listing's photos.

    sleep_fn, when given, is called after every photo that actually hit the
    network (not after a skip) -- callers pass a randomized-delay function
    to avoid firing hundreds of photo requests back-to-back with no pacing,
    which looks nothing like a browser loading a gallery. Left as an
    injected no-op by default so tests run instantly."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    saved: list[Path] = []
    for i, url in enumerate(photo_urls, start=1):
        dest = dest_dir / f"{i:02d}.jpg"
        if dest.exists():
            saved.append(dest)
            continue
        try:
            dest.write_bytes(fetch_bytes(url))
        except Exception as exc:
            print(f"skip photo (failed to download {url}): {exc}")
            continue
        saved.append(dest)
        if sleep_fn is not None:
            sleep_fn()
    return saved


def delete_photos(photos_dir: Path, listing_id: str) -> None:
    """Removes a listing's downloaded photos entirely. Safe to call even if
    the listing never had photos downloaded. A real removal failure (e.g.
    a locked file or a permissions issue) is logged rather than silently
    swallowed, so a failed cleanup doesn't look identical to a no-op one —
    but it still doesn't raise, matching this project's per-item
    log-and-continue convention."""
    listing_dir = photos_dir / listing_id
    if not listing_dir.exists():
        return
    try:
        shutil.rmtree(listing_dir)
    except OSError as exc:
        print(f"warning: failed to fully remove photos for {listing_id}: {exc}")


def count_downloaded_photos(photos_dir: Path, listing_id: str) -> int:
    """Counts already-downloaded photos for a listing, used to decide whether
    there's enough to run vision scoring on."""
    listing_dir = photos_dir / listing_id
    if not listing_dir.exists():
        return 0
    return len(list(listing_dir.glob("*.jpg")))
