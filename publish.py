import os
import sys
from pathlib import Path

import requests
import turso_serverless
from dotenv import dotenv_values

from src.db import get_connection, query_listings
from src.turso_sync import ensure_schema, replace_listing_rows, upsert_row
from src.blob_upload import already_uploaded, upload_photo

DATA_DIR = Path("data")
DB_PATH = DATA_DIR / "listings.db"
PHOTOS_DIR = DATA_DIR / "photos"

# Every mirrored table that has a real per-row primary key -- synced with
# upsert_row. amenities/photo_urls are handled separately (Step 6 below)
# since they have no primary key of their own.
KEYED_TABLES = ["listings", "commute", "scores", "visual_scores"]

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
        for row in local_conn.execute(f"SELECT * FROM {table}"):
            try:
                upsert_row(turso_conn, table, row)
                synced += 1
            except Exception as exc:
                failed += 1
                print(f"  {table}/{row['listing_id']}: row sync failed ({exc})")
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


def _upload_new_photos(turso_conn, listing_ids: list[str], rw_token: str) -> tuple[int, int]:
    uploaded = 0
    failed = 0
    for listing_id in listing_ids:
        photo_dir = PHOTOS_DIR / listing_id
        if not photo_dir.exists():
            continue
        for photo_path in sorted(photo_dir.glob("*.jpg")):
            position = int(photo_path.stem)
            if already_uploaded(turso_conn, listing_id, position):
                continue
            try:
                url = upload_photo(photo_path, listing_id, position, rw_token)
            except Exception as exc:
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
            except Exception as exc:
                failed += 1
                print(f"  {listing_id}/{photo_path.name}: hosted_photos record failed ({exc})")
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


if __name__ == "__main__":
    main()
