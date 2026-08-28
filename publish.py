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


def _sync_keyed_tables(local_conn, turso_conn) -> int:
    synced = 0
    for table in KEYED_TABLES:
        for row in local_conn.execute(f"SELECT * FROM {table}"):
            upsert_row(turso_conn, table, row)
            synced += 1
    return synced


def _sync_per_listing_tables(local_conn, turso_conn, listing_ids: list[str]) -> None:
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
            print(f"  {listing_id}: per-listing table sync failed ({exc})")
            continue


def _upload_new_photos(turso_conn, listing_ids: list[str], rw_token: str) -> int:
    uploaded = 0
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
                print(f"  {listing_id}/{photo_path.name}: upload failed ({exc})")
                continue
            turso_conn.execute(
                "INSERT OR REPLACE INTO hosted_photos (listing_id, position, blob_url) "
                "VALUES (?, ?, ?)",
                (listing_id, position, url),
            )
            turso_conn.commit()
            uploaded += 1
    return uploaded


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
    ensure_schema(turso_conn)

    rows = query_listings(local_conn)
    listing_ids = [row["listing_id"] for row in rows]

    synced = _sync_keyed_tables(local_conn, turso_conn)
    _sync_per_listing_tables(local_conn, turso_conn, listing_ids)
    print(f"synced {synced} rows across {len(KEYED_TABLES)} tables for {len(listing_ids)} listings")

    uploaded = _upload_new_photos(turso_conn, listing_ids, env["BLOB_READ_WRITE_TOKEN"])
    print(f"uploaded {uploaded} new photos")

    _revalidate(env["SHORT_LIST_URL"], env["REVALIDATE_SECRET"])

    local_conn.close()
    turso_conn.close()


if __name__ == "__main__":
    main()
