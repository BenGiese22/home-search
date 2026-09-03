import hashlib
import re
import shutil
from pathlib import Path
from typing import Callable

# A photo is identified by its source URL, not by its position in the
# listing. `(listing_id, position)` looked like a key and was not: a listing
# can be delisted and relist under the same listing_id with entirely
# different photos in the same positions, and a live listing can have its
# photos re-shot or reordered. 6085 West 82nd Drive did exactly that and came
# back with 44 stale photos whose positions all matched -- the download and
# the upload would both have skipped, and the viewer would have gone on
# serving the previous listing's images with nothing failing.
#
# Eight hex characters of sha1 is ~4.3 billion values; the collision that
# matters is between the handful of URLs one listing serves at one position,
# not across the corpus. sha1 is used as a short stable digest, not for
# anything security-bearing.
_HASH_CHARS = 8


def photo_hash(url: str) -> str:
    """The identity of a photo's source URL, as 8 lowercase hex chars."""
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:_HASH_CHARS]


def photo_filename(position: int, url: str) -> str:
    """`NN-<hash8>.jpg`. The position still leads so a plain sorted() over
    the directory stays in listing order -- score_photos.py and the gallery
    both depend on that."""
    return f"{position:02d}-{photo_hash(url)}.jpg"


# Not `*.jpg`: a leftover file in the old positional format must be invisible
# to every count, score and upload, because it may belong to a previous
# listing at the same id. ops/migrate_photo_files.py renames the ones that
# are still current.
#
# Two leading digits then `*` rather than a flat `??`, because the position
# is zero-padded to two digits but not truncated to them -- MAX_PHOTOS_PER_
# LISTING defaults to no cap, so a 100-photo listing writes `100-<hash>.jpg`
# and a two-`?` pattern would quietly stop counting at 99. The `*` cannot
# swallow a non-numeric prefix: the two `[0-9]` come first, and
# parse_photo_filename re-validates anyway.
PHOTO_GLOB = "[0-9][0-9]*-" + "?" * _HASH_CHARS + ".jpg"

_PHOTO_FILENAME_RE = re.compile(r"^(\d{2,})-([0-9a-f]{%d})\.jpg$" % _HASH_CHARS)


def parse_photo_filename(name: str) -> tuple[int, str] | None:
    """`(position, hash8)` for a content-keyed filename, None for anything
    else -- including the old `NN.jpg`, which callers must treat as absent
    rather than as position NN."""
    match = _PHOTO_FILENAME_RE.match(name)
    if match is None:
        return None
    return int(match.group(1)), match.group(2)


def download_photos(
    photo_urls: list[str],
    dest_dir: Path,
    fetch_bytes: Callable[[str], bytes],
    sleep_fn: Callable[[], None] | None = None,
) -> list[Path]:
    """Download each photo to dest_dir/NN-<hash8>.jpg, skipping files that
    already exist. A failure fetching one photo is logged and skipped rather
    than aborting the rest of the listing's photos.

    The filename carries sha1(url)[:8] so the skip is keyed on the photo's
    identity rather than its slot -- see photo_hash(). A position whose URL
    changed is a different filename, so it downloads instead of silently
    keeping the old image.

    sleep_fn, when given, is called after every photo that actually hit the
    network (not after a skip) -- callers pass a randomized-delay function
    to avoid firing hundreds of photo requests back-to-back with no pacing,
    which looks nothing like a browser loading a gallery. Left as an
    injected no-op by default so tests run instantly."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    saved: list[Path] = []
    for i, url in enumerate(photo_urls, start=1):
        dest = dest_dir / photo_filename(i, url)
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
    there's enough to run vision scoring on. Only content-keyed files count:
    an un-migrated NN.jpg cannot be attributed to the listing's current
    photos, so counting it would let a stale set clear the scoring floor."""
    listing_dir = photos_dir / listing_id
    if not listing_dir.exists():
        return 0
    return len(list(listing_dir.glob(PHOTO_GLOB)))
