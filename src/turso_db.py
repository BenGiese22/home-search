"""Hosted Turso: the connection factory and the batched write path.

This module was `src/turso_sync.py`, whose job was mirroring a local SQLite
database into Turso. Under the single-source-of-truth architecture there is
nothing to mirror -- these writes ARE the write path, so the module is named
for the database rather than for the copying it used to do.

Standing rule for everything in here: no per-row round-trips and no
check-then-act loops. Every statement against hosted Turso is an HTTP
round-trip measured at ~240ms, and this project has already paid for
forgetting that twice -- a one-statement-per-row sync that took 22 minutes,
and a per-photo existence check that burned 12 minutes before the first
upload started.
"""
import re
import sqlite3
from collections.abc import Mapping
from typing import Callable

import turso_serverless

from src.config import load_env

# A turso_serverless connection defaults row_factory to None, which makes
# conn.execute(...).fetchone() return a bare tuple. Every caller in src/db.py
# reads columns by name (row["listing_id"]), so without this the cutover
# breaks everywhere at once with TypeError: tuple indices must be integers.
# turso_serverless.Row is otherwise a faithful sqlite3.Row: same access by
# name and by index, same keys(), same dict()/iteration behaviour. The single
# documented difference is the exception for an unknown column -- sqlite3.Row
# raises IndexError, this raises KeyError -- which is pinned by a test rather
# than normalised, because nothing here indexes a column it did not SELECT.
ROW_FACTORY = turso_serverless.Row

REQUIRED_ENV_VARS = ("TURSO_DATABASE_URL", "TURSO_AUTH_TOKEN")


def connect(
    env: Mapping[str, str] | None = None,
    connect_fn: Callable = turso_serverless.connect,
):
    """Opens the hosted Turso connection the stages read and write.

    One place remembers to set the row factory, so no stage has to. `env`
    defaults to the merged .env/process-environment lookup; `connect_fn` is
    injected so tests never open a real session.
    """
    env = load_env() if env is None else env
    missing = [key for key in REQUIRED_ENV_VARS if not env.get(key)]
    if missing:
        raise RuntimeError(
            "cannot connect to Turso: missing "
            + ", ".join(missing)
            + " (set them in .env -- see .env.example)"
        )
    conn = connect_fn(env["TURSO_DATABASE_URL"], auth_token=env["TURSO_AUTH_TOKEN"])
    conn.row_factory = ROW_FACTORY
    return conn

TURSO_SCHEMA_EXTRA = """
CREATE TABLE IF NOT EXISTS hosted_photos (
    listing_id TEXT NOT NULL,
    position INTEGER NOT NULL,
    blob_url TEXT NOT NULL,
    PRIMARY KEY (listing_id, position)
);
"""

_CREATE_TABLE_RE = re.compile(
    r"CREATE TABLE IF NOT EXISTS\s+(\w+)\s*\((.*?)\)\s*;", re.DOTALL
)
_TABLE_CONSTRAINT_KEYWORDS = {"PRIMARY", "FOREIGN", "UNIQUE", "CHECK", "CONSTRAINT"}


def _split_top_level(body: str) -> list[str]:
    """Splits a CREATE TABLE column list on commas that are not nested
    inside parentheses (e.g. the comma-free `REFERENCES listings(listing_id)`
    inline constraints already used throughout _SCHEMA)."""
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    for ch in body:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    parts.append("".join(current))
    return parts


def _parse_columns(schema_sql: str) -> dict[str, dict[str, str]]:
    """Parses `{table_name: {column_name: column_def_sql}}` out of a block of
    `CREATE TABLE IF NOT EXISTS` statements, where column_def_sql is the
    column's type plus constraints (minus PRIMARY KEY / REFERENCES, which
    SQLite's ALTER TABLE ... ADD COLUMN does not accept on an existing
    table), ready to append after `ALTER TABLE t ADD COLUMN <name> `."""
    tables: dict[str, dict[str, str]] = {}
    for match in _CREATE_TABLE_RE.finditer(schema_sql):
        table_name, body = match.group(1), match.group(2)
        columns: dict[str, str] = {}
        for part in _split_top_level(body):
            part = part.strip()
            if not part:
                continue
            tokens = part.split(None, 1)
            if len(tokens) != 2:
                continue
            name, rest = tokens
            if name.upper() in _TABLE_CONSTRAINT_KEYWORDS:
                continue  # table-level constraint, not a column definition
            rest = re.sub(r"PRIMARY KEY", "", rest, flags=re.IGNORECASE)
            rest = re.sub(r"REFERENCES\s+\w+\s*\([^)]*\)", "", rest, flags=re.IGNORECASE)
            columns[name] = re.sub(r"\s+", " ", rest).strip()
        if columns:
            tables[table_name] = columns
    return tables


def _migrate_missing_columns(conn, schema_sql: str) -> None:
    """`CREATE TABLE IF NOT EXISTS` no-ops on a table that already exists, so
    a mirror created before a column was added to _SCHEMA never gains it
    (silently -- upsert_row then fails with "table X has no column named Y").
    Diffs each mirrored table's actual columns (PRAGMA table_info) against
    the columns _SCHEMA now declares and ALTER TABLE ... ADD COLUMN whatever
    is missing -- the same migration pattern src.db.init_db() already
    applies to the local sqlite db, generalized so new _SCHEMA columns never
    need a matching hand-written branch here."""
    for table, columns in _parse_columns(schema_sql).items():
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, definition in columns.items():
            if name in existing:
                continue
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


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
        _migrate_missing_columns(conn, fragment)

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


# Each conn.execute() against hosted Turso is an HTTP round-trip, measured
# at ~240ms. A full sync writes ~5400 rows (photo_urls alone is ~3000), so
# one-statement-per-row costs ~22 minutes. Batching them into multi-row
# INSERTs collapses that to a few dozen round-trips. Chunked rather than one
# giant statement to stay well inside SQLite's variable limit (999 by
# default): CHUNK * columns must remain under it, and 50 x 16 columns is the
# widest mirrored table's worst case.
class BatchRowErrors(Exception):
    """Raised when some rows in a batched write could not be inserted, after
    each was retried individually. Carries the failed rows so the caller can
    count and report them without losing the ones that succeeded."""

    def __init__(self, table: str, rows: list):
        self.table = table
        self.rows = rows
        super().__init__(f"{len(rows)} row(s) failed to sync into {table}")


# Rows per batched statement. CHUNK x (widest table's column count) must stay
# under SQLite's 999-variable default, which test_batch_chunk_stays_inside_
# sqlites_variable_limit enforces. `listings` reached 23 columns when the
# structured fields landed, and 50 x 23 = 1150 would have silently broken
# every Turso upsert -- lowered to 30 (30 x 23 = 690), which also leaves room
# for ~10 more columns before this needs revisiting. The cost is a few more
# round trips per sync, which is nothing against an 85-row corpus.
BATCH_CHUNK = 30


def upsert_rows(conn, table: str, rows: list[sqlite3.Row]) -> None:
    """Inserts or replaces many rows in as few round-trips as possible.

    Same semantics as calling upsert_row() per row -- INSERT OR REPLACE keyed
    on each table's own primary key -- but issues one multi-row statement per
    chunk instead of one per row. Rows are assumed to share a column set,
    which holds because they come from a single SELECT * on one table."""
    if not rows:
        return
    columns = list(rows[0].keys())
    col_list = ", ".join(columns)
    one = "(" + ", ".join("?" for _ in columns) + ")"
    failed_rows: list = []

    for start in range(0, len(rows), BATCH_CHUNK):
        chunk = rows[start:start + BATCH_CHUNK]
        values: list[object] = []
        for row in chunk:
            values.extend(row[c] for c in columns)
        try:
            conn.execute(
                f"INSERT OR REPLACE INTO {table} ({col_list}) VALUES "
                + ", ".join(one for _ in chunk),
                tuple(values),
            )
        except Exception:
            # One bad row would otherwise take its whole chunk with it.
            # Seen for real: visual_scores holds orphan rows whose listing no
            # longer exists, and Turso enforces the foreign key -- so a batch
            # of 50 lost 49 good rows to 1 bad one. Retry row by row so only
            # genuinely bad rows fail.
            for row in chunk:
                try:
                    upsert_row(conn, table, row)
                except Exception:
                    failed_rows.append(row)
    if hasattr(conn, "commit"):
        conn.commit()
    if failed_rows:
        raise BatchRowErrors(table, failed_rows)


def replace_listing_rows(conn, table: str, listing_id: str, rows: list[sqlite3.Row]) -> None:
    """For tables with no per-row primary key (amenities, photo_urls):
    deletes every existing row for this listing_id, then inserts the
    current set. Avoids duplicate accumulation across reruns."""
    conn.execute(f"DELETE FROM {table} WHERE listing_id = ?", (listing_id,))
    upsert_rows(conn, table, rows)
    if hasattr(conn, "commit"):
        conn.commit()
