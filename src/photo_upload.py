"""Uploading listing photos to Vercel Blob, as a stage-level concern.

This lived inside publish.py. Once Turso is the source of truth and
publish.py is gone, upload belongs where the photos are produced -- the
scrape stage, immediately after they are downloaded.

Two hard-won invariants live here, both paid for in production:

- **The skip set is ONE query.** A per-photo already_uploaded() check spent
  ~12 minutes on ~3,000 photos before the first upload could start, because
  every statement against hosted Turso is a ~240ms HTTP round-trip.
- **A failed upload records no row.** The hosted_photos row is what marks a
  photo as done, so writing one for a failed upload would permanently skip a
  photo that never made it.
"""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

from src.blob_upload import upload_photo
from src.photos import PHOTO_GLOB, parse_photo_filename
from src.turso_db import chunk_size

# Uploads are independent -- each writes its own pathname -- so they
# parallelize cleanly. Each is one HTTP PUT, so a worker costs a socket.
# Kept modest: this is a personal sync, not a load test.
UPLOAD_WORKERS = 8

_HOSTED_COLUMNS = ("listing_id", "position", "blob_url")


def collect_pending_photos(
    conn,
    photos_dir: Path,
    listing_ids: list[str],
    max_per_listing: int = 0,
) -> list[tuple[str, int, Path]]:
    """The photos on disk that are not yet hosted.

    Reads the whole hosted_photos key set in ONE query rather than asking per
    photo. max_per_listing of 0 means no cap.
    """
    already: set[tuple[str, int]] = set()
    for row in conn.execute("SELECT listing_id, position FROM hosted_photos"):
        already.add((row[0], int(row[1])))

    pending: list[tuple[str, int, Path]] = []
    for listing_id in listing_ids:
        photo_dir = photos_dir / listing_id
        if not photo_dir.exists():
            continue
        # PHOTO_GLOB rather than *.jpg so an un-migrated NN.jpg is never
        # uploaded: it may be a previous listing's photo at this id.
        photo_paths = sorted(photo_dir.glob(PHOTO_GLOB))
        if max_per_listing > 0:
            photo_paths = photo_paths[:max_per_listing]
        for photo_path in photo_paths:
            parsed = parse_photo_filename(photo_path.name)
            if parsed is None:
                print(
                    f"  {listing_id}/{photo_path.name}: skipped "
                    "(name is not NN-<hash8>.jpg)"
                )
                continue
            position, _ = parsed
            if (listing_id, position) in already:
                continue
            pending.append((listing_id, position, photo_path))
    return pending


def _record(conn, recorded: list[tuple[str, int, str]]) -> None:
    """One multi-row INSERT per chunk, rather than an INSERT plus a commit
    per photo. The per-photo form was three round-trips each -- roughly
    2,600 for an 867-photo run, dwarfing the uploads themselves."""
    if not recorded:
        return
    col_list = ", ".join(_HOSTED_COLUMNS)
    one = "(" + ", ".join("?" for _ in _HOSTED_COLUMNS) + ")"
    per_statement = chunk_size(len(_HOSTED_COLUMNS))
    with conn:
        for start in range(0, len(recorded), per_statement):
            chunk = recorded[start:start + per_statement]
            flat: list[object] = []
            for row in chunk:
                flat.extend(row)
            conn.execute(
                f"INSERT OR REPLACE INTO hosted_photos ({col_list}) VALUES "
                + ", ".join(one for _ in chunk),
                tuple(flat),
            )


def upload_photos(
    conn,
    photos_dir: Path,
    listing_ids: list[str],
    rw_token: str,
    max_per_listing: int = 0,
    upload_fn: Callable = upload_photo,
    workers: int = UPLOAD_WORKERS,
) -> tuple[int, int]:
    """Uploads every not-yet-hosted photo and records its URL.

    Three phases, deliberately: the pending list is built on the calling
    thread (it reads the shared connection), the uploads run in parallel
    (they touch only HTTP and the filesystem), and the hosted_photos writes
    happen back on the calling thread in batches. The shared connection is
    therefore never touched from a worker.

    Returns (uploaded, failed). A photo whose upload fails records no row, so
    a rerun retries exactly that photo and nothing else.
    """
    pending = collect_pending_photos(conn, photos_dir, listing_ids, max_per_listing)
    if not pending:
        return 0, 0

    print(f"uploading {len(pending)} new photo(s) with {workers} workers")

    def _do_upload(item):
        listing_id, position, photo_path = item
        try:
            return item, upload_fn(photo_path, listing_id, position, rw_token), None
        except Exception as exc:
            return item, None, exc

    recorded: list[tuple[str, int, str]] = []
    failed = 0
    flush_every = chunk_size(len(_HOSTED_COLUMNS))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        # Consumed in submission order rather than completion order so
        # printed failures stay grouped by listing.
        for item, url, exc in pool.map(_do_upload, pending):
            listing_id, position, photo_path = item
            if exc is not None:
                failed += 1
                print(f"  {listing_id}/{photo_path.name}: upload failed ({exc})")
                continue
            recorded.append((listing_id, position, url))
            # Flush periodically so a crash mid-run does not throw away
            # uploads already paid for -- they would be re-uploaded next run,
            # which is correct but wastes operations.
            if len(recorded) >= flush_every:
                _record(conn, recorded)
                recorded = []

    _record(conn, recorded)

    uploaded = len(pending) - failed
    return uploaded, failed
