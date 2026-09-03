"""Uploading listing photos to Vercel Blob, as a stage-level concern.

This lived inside publish.py. Once Turso is the source of truth and
publish.py is gone, upload belongs where the photos are produced -- the
scrape stage, immediately after they are downloaded.

Three hard-won invariants live here, all paid for in production:

- **The skip set is ONE query.** A per-photo already_uploaded() check spent
  ~12 minutes on ~3,000 photos before the first upload could start, because
  every statement against hosted Turso is a ~240ms HTTP round-trip.
- **A failed upload records no row.** The hosted_photos row is what marks a
  photo as done, so writing one for a failed upload would permanently skip a
  photo that never made it.
- **The skip key includes the source URL.** `(listing_id, position)` is
  where a photo sits, not what it is. 6085 West 82nd Drive was delisted and
  relisted under the same listing_id with 44 different photos in the same 44
  positions; every positional key matched, so the upload would have skipped
  and the viewer would have gone on serving the previous listing's images
  with nothing failing anywhere.
"""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, NamedTuple

from src.blob_upload import upload_photo
from src.photos import photo_filename
from src.turso_db import chunk_size

# Uploads are independent -- each writes its own pathname -- so they
# parallelize cleanly. Each is one HTTP PUT, so a worker costs a socket.
# Kept modest: this is a personal sync, not a load test.
UPLOAD_WORKERS = 8

_HOSTED_COLUMNS = ("listing_id", "position", "blob_url", "source_url")


class PendingPhoto(NamedTuple):
    """One photo that is on disk and not yet hosted under its current URL.

    superseded_blob_url is the blob this upload replaces, or None for a photo
    that was never hosted at this position. hosted_photos.blob_url is the
    project's only record of what exists in the store, so if the pending item
    did not carry it forward the old blob would be stranded the instant the
    row is overwritten -- 1,813 rows' worth of blobs (~371 MB) accumulated
    exactly that way before anything here could delete one.
    """
    listing_id: str
    position: int
    path: Path
    source_url: str
    superseded_blob_url: str | None


def get_photo_urls_by_listing(conn) -> dict[str, list[str]]:
    """Every listing's photo URLs in ONE statement, in position order,
    keyed by listing_id. A listing with no photo_urls rows still gets a key
    with an empty list, so callers can index the dict directly.

    B1 moves this to src/db.py under the same name, alongside the other
    set-based queries; it lives here until then so B0 does not collide with
    that task inside src/db.py. Same LEFT JOIN idiom as
    get_amenities_by_listing.
    """
    by_listing: dict[str, list[str]] = {}
    for row in conn.execute(
        """
        SELECT l.listing_id AS listing_id, p.url AS url
        FROM listings l
        LEFT JOIN photo_urls p ON p.listing_id = l.listing_id
        ORDER BY l.listing_id, p.position
        """
    ):
        urls = by_listing.setdefault(row["listing_id"], [])
        if row["url"] is not None:
            urls.append(row["url"])
    return by_listing


def collect_pending_photos(
    conn,
    photos_dir: Path,
    photo_urls_by_listing: dict[str, list[str]],
    max_per_listing: int = 0,
) -> list[PendingPhoto]:
    """The photos on disk that are not hosted under their current source URL.

    Driven by the listing's CURRENT photo URLs rather than by whatever files
    happen to sit in its directory: the URL is what decides both the expected
    filename and whether the hosted row still matches, and a file with no
    corresponding URL is by definition stale.

    Reads the whole hosted_photos key set in ONE query rather than asking per
    photo -- exactly one statement no matter how many listings are passed.
    max_per_listing of 0 means no cap.

    A hosted row whose source_url is NULL predates the column and its
    identity is unknown; unknown is treated as "does not match", so it
    re-uploads rather than being assumed current.
    ops/backfill_hosted_source_urls.py exists to make that a one-time cost.
    """
    hosted: dict[tuple[str, int], tuple[str | None, str]] = {}
    for row in conn.execute(
        "SELECT listing_id, position, source_url, blob_url FROM hosted_photos"
    ):
        hosted[(row[0], int(row[1]))] = (row[2], row[3])

    pending: list[PendingPhoto] = []
    for listing_id, urls in photo_urls_by_listing.items():
        photo_dir = photos_dir / listing_id
        if not photo_dir.exists():
            continue
        if max_per_listing > 0:
            urls = urls[:max_per_listing]
        for position, source_url in enumerate(urls, start=1):
            path = photo_dir / photo_filename(position, source_url)
            # No file means the download failed or has not run yet. Nothing
            # to upload; the next scrape fetches it.
            if not path.exists():
                continue
            prior = hosted.get((listing_id, position))
            if prior is not None and prior[0] == source_url:
                continue
            pending.append(
                PendingPhoto(
                    listing_id=listing_id,
                    position=position,
                    path=path,
                    source_url=source_url,
                    superseded_blob_url=None if prior is None else prior[1],
                )
            )
    return pending


def _record(conn, recorded: list[tuple[str, int, str, str]]) -> None:
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


def _retire_blobs(urls: list[str]) -> None:
    """Blobs whose hosted_photos row has just been overwritten by a
    replacement upload.

    Called only AFTER the replacement row is written, so a failure here can
    only ever strand a blob -- it can never leave a row pointing at a blob
    that no longer exists. Commit 3 of this task wires src.blob_upload.
    delete_blobs in behind this seam; until then the URLs are printed so
    they are recoverable rather than lost silently.
    """
    for url in urls:
        print(f"  superseded blob (not yet deleted): {url}")


def upload_photos(
    conn,
    photos_dir: Path,
    photo_urls_by_listing: dict[str, list[str]],
    rw_token: str,
    max_per_listing: int = 0,
    upload_fn: Callable = upload_photo,
    workers: int = UPLOAD_WORKERS,
) -> tuple[int, int]:
    """Uploads every photo not hosted under its current URL and records it.

    Three phases, deliberately: the pending list is built on the calling
    thread (it reads the shared connection), the uploads run in parallel
    (they touch only HTTP and the filesystem), and the hosted_photos writes
    happen back on the calling thread in batches. The shared connection is
    therefore never touched from a worker.

    Returns (uploaded, failed). A photo whose upload fails records no row, so
    a rerun retries exactly that photo and nothing else.
    """
    pending = collect_pending_photos(
        conn, photos_dir, photo_urls_by_listing, max_per_listing
    )
    if not pending:
        return 0, 0

    print(f"uploading {len(pending)} new photo(s) with {workers} workers")

    def _do_upload(item: PendingPhoto):
        try:
            return item, upload_fn(
                item.path, item.listing_id, item.position, rw_token, item.source_url
            ), None
        except Exception as exc:
            return item, None, exc

    recorded: list[tuple[str, int, str, str]] = []
    # Kept in step with `recorded` and flushed with it: a blob is retired only
    # once the row that replaces it has actually been written.
    superseded: list[str] = []
    failed = 0
    flush_every = chunk_size(len(_HOSTED_COLUMNS))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        # Consumed in submission order rather than completion order so
        # printed failures stay grouped by listing.
        for item, url, exc in pool.map(_do_upload, pending):
            if exc is not None:
                failed += 1
                print(f"  {item.listing_id}/{item.path.name}: upload failed ({exc})")
                continue
            recorded.append((item.listing_id, item.position, url, item.source_url))
            if item.superseded_blob_url:
                superseded.append(item.superseded_blob_url)
            # Flush periodically so a crash mid-run does not throw away
            # uploads already paid for -- they would be re-uploaded next run,
            # which is correct but wastes operations.
            if len(recorded) >= flush_every:
                _record(conn, recorded)
                _retire_blobs(superseded)
                recorded = []
                superseded = []

    _record(conn, recorded)
    _retire_blobs(superseded)

    uploaded = len(pending) - failed
    return uploaded, failed
