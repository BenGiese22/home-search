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
