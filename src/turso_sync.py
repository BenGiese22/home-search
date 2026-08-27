import sqlite3

TURSO_SCHEMA_EXTRA = """
CREATE TABLE IF NOT EXISTS hosted_photos (
    listing_id TEXT NOT NULL,
    position INTEGER NOT NULL,
    blob_url TEXT NOT NULL,
    PRIMARY KEY (listing_id, position)
);
"""


def ensure_schema(conn) -> None:
    """Mirrors home-search's local SQLite schema into the given connection
    (Turso in production, plain sqlite3 in tests), plus the Turso-only
    hosted_photos table. Reuses src.db._SCHEMA directly so the two schemas
    can never drift apart.

    Uses conn.execute() per statement rather than executescript(), since
    executescript() is a sqlite3-specific extension not guaranteed to exist
    on a DB-API-style connection such as turso_serverless's."""
    from src.db import _SCHEMA

    for fragment in (_SCHEMA, TURSO_SCHEMA_EXTRA):
        for statement in fragment.split(";"):
            statement = statement.strip()
            if not statement:
                continue
            conn.execute(statement)

    if hasattr(conn, "commit"):
        conn.commit()


def upsert_row(conn, table: str, row: sqlite3.Row) -> None:
    """Inserts or replaces one row using its own column names -- works for
    every mirrored table that has a real primary key (listings, commute,
    scores, visual_scores), since it never hardcodes a column list."""
    columns = row.keys()
    col_list = ", ".join(columns)
    placeholders = ", ".join("?" for _ in columns)
    values = tuple(row[c] for c in columns)
    conn.execute(
        f"INSERT OR REPLACE INTO {table} ({col_list}) VALUES ({placeholders})",
        values,
    )
    if hasattr(conn, "commit"):
        conn.commit()


def replace_listing_rows(conn, table: str, listing_id: str, rows: list[sqlite3.Row]) -> None:
    """For tables with no per-row primary key (amenities, photo_urls):
    deletes every existing row for this listing_id, then inserts the
    current set. Avoids duplicate accumulation across reruns."""
    conn.execute(f"DELETE FROM {table} WHERE listing_id = ?", (listing_id,))
    for row in rows:
        upsert_row(conn, table, row)
    if hasattr(conn, "commit"):
        conn.commit()
