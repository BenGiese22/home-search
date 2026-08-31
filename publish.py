import os
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests
import turso_serverless
from dotenv import dotenv_values

from src.db import get_connection, query_listings
from src.turso_sync import BatchRowErrors, ensure_schema, replace_listing_rows, upsert_rows
from src.blob_upload import upload_photo

DATA_DIR = Path("data")
DB_PATH = DATA_DIR / "listings.db"
PHOTOS_DIR = DATA_DIR / "photos"

# Every mirrored table that has a real per-row primary key -- synced with
# upsert_row. amenities/photo_urls are handled separately (Step 6 below)
# since they have no primary key of their own.
KEYED_TABLES = ["listings", "commute", "scores", "visual_scores"]

# Mirrors src.turso_sync.BATCH_CHUNK -- how many rows go in one statement.
TURSO_BATCH_CHUNK = 30

# Photo uploads shell out to the Vercel CLI, which costs ~0.6s of Node
# startup per photo. At ~3000 photos that is ~30 minutes of pure process
# spawn, serially. The uploads are independent -- each writes its own
# pathname -- so they parallelize cleanly. Kept modest: this is a personal
# sync, not a load test, and every worker is a real `vercel` process.
PHOTO_UPLOAD_WORKERS = 8

# Vercel bills every upload as an "Advanced Operation" and the Hobby tier
# includes only 2,000 per month. Backfilling all ~3,000 local photos costs
# more than the whole monthly budget in one run -- and with the CLI's
# multipart default it cost 11,000. The list view needs one photo per
# listing and the detail gallery is comfortable with a handful, so cap what
# gets hosted. 85 listings x 8 = ~680 operations, which fits the free tier
# with room to spare. Raise it (or set it to 0 for no cap) on a paid plan.
MAX_PHOTOS_PER_LISTING = int(os.environ.get("MAX_PHOTOS_PER_LISTING", "8"))

# Every table pruning must consider: the keyed tables above, plus the
# per-listing tables and the Turso-only hosted_photos table. This is the
# only source of table names _prune_deleted_listings uses -- it is a fixed
# internal list, never derived from external input, so interpolating a name
# from it into SQL cannot become an injection path.
PRUNABLE_TABLES = KEYED_TABLES + ["amenities", "photo_urls", "hosted_photos"]

# Config publish.py needs from the environment. Checked up front so a
# missing var fails with a readable message instead of a bare KeyError.
REQUIRED_ENV_VARS = [
    "TURSO_DATABASE_URL",
    "TURSO_AUTH_TOKEN",
    "BLOB_READ_WRITE_TOKEN",
    "SHORT_LIST_URL",
    "REVALIDATE_SECRET",
]


def _sync_keyed_tables(local_conn, turso_conn) -> tuple[int, int]:
    synced = 0
    failed = 0
    for table in KEYED_TABLES:
        # Batched: each round-trip to hosted Turso is ~240ms, so one
        # statement per row made a full sync take ~22 minutes. A failure now
        # costs its whole chunk rather than a single row -- an acceptable
        # trade at this scale, and the run still continues to the next chunk.
        rows = local_conn.execute(f"SELECT * FROM {table}").fetchall()
        for start in range(0, len(rows), TURSO_BATCH_CHUNK):
            chunk = rows[start:start + TURSO_BATCH_CHUNK]
            try:
                upsert_rows(turso_conn, table, chunk)
                synced += len(chunk)
            except BatchRowErrors as exc:
                # upsert_rows already retried each row individually, so only
                # genuinely bad rows are in exc.rows -- the rest landed.
                failed += len(exc.rows)
                synced += len(chunk) - len(exc.rows)
                for row in exc.rows:
                    print(f"  {table}/{row['listing_id']}: row sync failed")
            except Exception as exc:
                failed += len(chunk)
                print(f"  {table}: batch of {len(chunk)} row(s) failed ({exc})")
                continue
    return synced, failed


def _sync_per_listing_tables(local_conn, turso_conn, listing_ids: list[str]) -> int:
    failed = 0
    for listing_id in listing_ids:
        try:
            amenity_rows = local_conn.execute(
                "SELECT * FROM amenities WHERE listing_id = ?", (listing_id,)
            ).fetchall()
            replace_listing_rows(turso_conn, "amenities", listing_id, amenity_rows)

            photo_url_rows = local_conn.execute(
                "SELECT * FROM photo_urls WHERE listing_id = ?", (listing_id,)
            ).fetchall()
            replace_listing_rows(turso_conn, "photo_urls", listing_id, photo_url_rows)
        except Exception as exc:
            failed += 1
            print(f"  {listing_id}: per-listing table sync failed ({exc})")
            continue
    return failed


def _prune_deleted_listings(turso_conn, listing_ids: list[str]) -> int:
    """Deletes rows for any listing_id no longer present in the current
    local sync from every mirrored table (PRUNABLE_TABLES) -- the
    counterpart to upsert that KEYED_TABLES never had, so a delisted home
    (scrape.py/check.py already exclude non-Active listings) stayed in the
    viewer forever instead of disappearing like it does locally.

    Table names are only ever drawn from the fixed PRUNABLE_TABLES list
    above; listing ids are always passed as bound parameters -- never
    interpolated into the SQL string -- so this cannot become a table-name
    or value injection path.

    If listing_ids is empty, prune is skipped entirely: that almost
    certainly means the local read failed rather than "every listing was
    delisted", and `NOT IN ()` would otherwise match (and delete) every
    row in the mirror.
    """
    if not listing_ids:
        return 0
    placeholders = ", ".join("?" for _ in listing_ids)
    pruned = 0
    for table in PRUNABLE_TABLES:
        row = turso_conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE listing_id NOT IN ({placeholders})",
            listing_ids,
        ).fetchone()
        table_pruned = row[0] if row is not None else 0
        if table_pruned:
            turso_conn.execute(
                f"DELETE FROM {table} WHERE listing_id NOT IN ({placeholders})",
                listing_ids,
            )
            pruned += table_pruned
    if hasattr(turso_conn, "commit"):
        turso_conn.commit()
    return pruned


def _collect_pending_photos(turso_conn, listing_ids: list[str]) -> list[tuple[str, int, Path]]:
    """Walks the local photo directories and returns the photos still needing
    upload.

    Fetches the whole hosted_photos key set in ONE query instead of calling
    already_uploaded() per photo. Against hosted Turso every query is a real
    HTTP round-trip -- measured at ~240ms -- so the per-photo version spent
    ~12 minutes on ~3000 photos before a single upload could start. Runs on
    the main thread because it reads the shared Turso connection."""
    already: set[tuple[str, int]] = set()
    for row in turso_conn.execute("SELECT listing_id, position FROM hosted_photos"):
        already.add((row[0], int(row[1])))

    pending: list[tuple[str, int, Path]] = []
    for listing_id in listing_ids:
        photo_dir = PHOTOS_DIR / listing_id
        if not photo_dir.exists():
            continue
        photo_paths = sorted(photo_dir.glob("*.jpg"))
        if MAX_PHOTOS_PER_LISTING > 0:
            photo_paths = photo_paths[:MAX_PHOTOS_PER_LISTING]
        for photo_path in photo_paths:
            try:
                position = int(photo_path.stem)
            except ValueError:
                print(f"  {listing_id}/{photo_path.name}: skipped (name is not NN.jpg)")
                continue
            if (listing_id, position) in already:
                continue
            pending.append((listing_id, position, photo_path))
    return pending


def _upload_new_photos(turso_conn, listing_ids: list[str], rw_token: str) -> tuple[int, int]:
    """Uploads every not-yet-hosted photo and records its URL.

    Three phases, deliberately: the pending list is built on the main thread
    (it reads Turso), the uploads run in parallel (they only touch the CLI
    and the filesystem), and the hosted_photos writes happen back on the main
    thread. The shared Turso connection is therefore never touched from a
    worker. A photo whose upload fails records no hosted_photos row, so a
    rerun retries it -- the same idempotency the serial version had."""
    pending = _collect_pending_photos(turso_conn, listing_ids)
    if not pending:
        return 0, 0

    print(f"uploading {len(pending)} new photo(s) with {PHOTO_UPLOAD_WORKERS} workers")

    def _do_upload(item: tuple[str, int, Path]) -> tuple[tuple[str, int, Path], str | None, Exception | None]:
        listing_id, position, photo_path = item
        try:
            return item, upload_photo(photo_path, listing_id, position, rw_token), None
        except Exception as exc:
            return item, None, exc

    uploaded = 0
    failed = 0
    with ThreadPoolExecutor(max_workers=PHOTO_UPLOAD_WORKERS) as pool:
        # Results are consumed in submission order rather than completion
        # order so the printed failures stay grouped by listing, and so the
        # hosted_photos writes happen one at a time on this thread.
        for item, url, exc in pool.map(_do_upload, pending):
            listing_id, position, photo_path = item
            if exc is not None:
                failed += 1
                print(f"  {listing_id}/{photo_path.name}: upload failed ({exc})")
                continue
            try:
                turso_conn.execute(
                    "INSERT OR REPLACE INTO hosted_photos (listing_id, position, blob_url) "
                    "VALUES (?, ?, ?)",
                    (listing_id, position, url),
                )
                turso_conn.commit()
            except Exception as record_exc:
                failed += 1
                print(f"  {listing_id}/{photo_path.name}: hosted_photos record failed ({record_exc})")
                continue
            uploaded += 1
    return uploaded, failed


def _revalidate(short_list_url: str, secret: str) -> None:
    try:
        response = requests.post(
            f"{short_list_url.rstrip('/')}/api/revalidate",
            headers={"Authorization": f"Bearer {secret}"},
            timeout=10,
        )
        response.raise_for_status()
        print("revalidated the hosted site")
    except Exception as exc:
        print(f"warning: revalidate call failed ({exc}) -- cache will expire naturally")


def main() -> None:
    env = {**dotenv_values(".env"), **os.environ}

    missing = [key for key in REQUIRED_ENV_VARS if not env.get(key)]
    if missing:
        sys.exit(
            "publish.py: missing required environment variable(s): "
            + ", ".join(missing)
            + " (set them in .env -- see .env.example)"
        )

    # upload_photo() shells out to the `vercel` CLI (see src/blob_upload.py).
    # It's an undeclared dependency requirements.txt can't express: if it's
    # missing, subprocess.run raises FileNotFoundError once per photo, which
    # _upload_new_photos already catches and counts as a failure -- so
    # without this check, a missing CLI silently fails every photo instead
    # of failing fast with a clear cause.
    if shutil.which("vercel") is None:
        sys.exit(
            "publish.py: the `vercel` CLI is required for photo uploads but "
            "was not found on PATH (npm install -g vercel)"
        )

    local_conn = get_connection(DB_PATH)
    turso_conn = turso_serverless.connect(
        env["TURSO_DATABASE_URL"], auth_token=env["TURSO_AUTH_TOKEN"]
    )
    try:
        ensure_schema(turso_conn)

        rows = query_listings(local_conn)
        listing_ids = [row["listing_id"] for row in rows]

        synced, row_failed = _sync_keyed_tables(local_conn, turso_conn)
        listing_failed = _sync_per_listing_tables(local_conn, turso_conn, listing_ids)
        summary = f"synced {synced} rows across {len(KEYED_TABLES)} tables for {len(listing_ids)} listings"
        if row_failed or listing_failed:
            summary += f" ({row_failed} row failures, {listing_failed} listing failures)"
        print(summary)

        pruned = _prune_deleted_listings(turso_conn, listing_ids)
        print(f"pruned {pruned} rows for listings no longer in the local sync")

        uploaded, upload_failed = _upload_new_photos(turso_conn, listing_ids, env["BLOB_READ_WRITE_TOKEN"])
        photo_summary = f"uploaded {uploaded} new photos"
        if upload_failed:
            photo_summary += f" ({upload_failed} failed)"
        print(photo_summary)

        _revalidate(env["SHORT_LIST_URL"], env["REVALIDATE_SECRET"])
    finally:
        local_conn.close()
        turso_conn.close()

    # A partial sync must not exit 0 -- otherwise a caller (cron, CI) has no
    # signal that some rows/listings/photos never made it to Turso, and the
    # viewer silently serves stale or incomplete data.
    total_failures = row_failed + listing_failed + upload_failed
    if total_failures > 0:
        sys.exit(
            f"publish.py: completed with {total_failures} failure(s) "
            f"({row_failed} row, {listing_failed} listing, {upload_failed} photo) -- see log above"
        )


if __name__ == "__main__":
    main()
