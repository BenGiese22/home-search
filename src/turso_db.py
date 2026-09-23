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
import time
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

# Every one of the pipeline's 6 stages calls stage_connection() at startup,
# and none of them had any retry: a transient DNS blip or a dropped
# connection crashed the stage before it did anything else. 3 attempts, not
# compute_commutes' open-ended rate-limit wait -- there is no Retry-After to
# read here, and a genuinely dead credential should still fail in a few
# seconds rather than have its failure notification delayed by minutes of
# backoff. One wait between each pair of attempts, so the schedule is
# exactly CONNECT_MAX_ATTEMPTS - 1 long.
CONNECT_MAX_ATTEMPTS = 3
CONNECT_BACKOFF_SECONDS = (1, 2)

# turso_serverless has no status attribute on its errors: session.py turns a
# non-200 response into ProtocolError("HTTP status NNN: ..."), and
# connection.py re-raises that as OperationalError with the same message.
# The prefix is the driver's own text, not echoed SQL, so it is a reliable
# enough discriminator for the two statuses that mean "this credential is
# refused" -- and those fail identically on every attempt, so retrying them
# only delays the alert. Any other status (a 502 from a proxy blip) is still
# retried.
#
# Because this depends on the driver's wording, requirements.txt pins
# turso_serverless to an exact version (tests/test_turso_db.py checks the
# pin matches what is installed). Re-read session.py before bumping it.
_AUTH_FAILURE = re.compile(r"HTTP status (401|403)\b")


def _is_retryable(exc: Exception) -> bool:
    return not _AUTH_FAILURE.search(str(exc))


def _retry_with_backoff(attempt_fn: Callable, sleep: Callable[[float], None]):
    """Retries `attempt_fn` up to CONNECT_MAX_ATTEMPTS times with a short
    backoff. An auth rejection is raised at once: it is not transient.

    Configuration errors never reach this: both callers check the env before
    the first attempt, because a missing URL is just as missing a second
    later.
    """
    for attempt in range(CONNECT_MAX_ATTEMPTS):
        try:
            return attempt_fn()
        except Exception as exc:
            if attempt == CONNECT_MAX_ATTEMPTS - 1 or not _is_retryable(exc):
                raise
            sleep(CONNECT_BACKOFF_SECONDS[attempt])


def _credentials(env: Mapping[str, str] | None) -> tuple[str, str]:
    """The URL and token, or a RuntimeError naming what is missing."""
    env = load_env() if env is None else env
    missing = [key for key in REQUIRED_ENV_VARS if not env.get(key)]
    if missing:
        raise RuntimeError(
            "cannot connect to Turso: missing "
            + ", ".join(missing)
            + " (set them in .env -- see .env.example)"
        )
    return env["TURSO_DATABASE_URL"], env["TURSO_AUTH_TOKEN"]


def _open(connect_fn: Callable, url: str, auth_token: str):
    """One connection attempt, no retry. Sets the row factory."""
    conn = connect_fn(url, auth_token=auth_token)
    conn.row_factory = ROW_FACTORY
    return conn


def _close_quietly(conn) -> None:
    """Best effort. The connection being abandoned is usually the one that
    just failed, so close() raising is expected and must not replace the
    error that actually matters."""
    try:
        conn.close()
    except Exception:
        pass


def connect(
    env: Mapping[str, str] | None = None,
    connect_fn: Callable = turso_serverless.connect,
    *,
    sleep: Callable[[float], None] = time.sleep,
):
    """Opens the hosted Turso connection the ops/ scripts read and write.

    One place remembers to set the row factory, so no caller has to. `env`
    defaults to the merged .env/process-environment lookup; `connect_fn` is
    injected so tests never open a real session, and `sleep` so a test never
    waits out a real backoff.

    turso_serverless.connect() itself does no network I/O -- it only builds a
    Session/Connection object -- so this retry is cheap insurance against a
    driver version that connects eagerly. The stages go through
    stage_connection(), which retries around the real first round-trip
    instead and does not stack this retry inside its own.
    """
    url, auth_token = _credentials(env)
    return _retry_with_backoff(lambda: _open(connect_fn, url, auth_token), sleep)

# source_url is what a hosted photo actually IS. (listing_id, position) is
# only where it sits: a listing can relist under the same id with entirely
# different photos in the same positions, and the upload skip believed them
# identical -- 6085 West 82nd Drive came back with 44 stale rows that all
# matched positionally. Added as a nullable column so _migrate_missing_columns
# can ALTER an existing table into it; ops/backfill_hosted_source_urls.py
# fills the pre-existing rows, and until it runs a NULL means "identity
# unknown", which collect_pending_photos treats as needing re-upload.
#
# pipeline_lock is the cross-home run lock. `pipeline.py`'s fcntl.flock is
# per-machine and cannot see the other execution home, so once the Phase 3
# cron is live two runs against one database are possible and nothing stops
# them. The concrete damage is not duplicated effort: the photo migration
# had a window in which a concurrent scrape would have re-downloaded ~700 MB
# from Compass, which is the exact traffic profile the reCAPTCHA durability
# canary is measuring.
#
# lock_name is the primary key and there is only ever one value in it, which
# is what turns "who wins" into a conflict the database resolves rather than
# one the application has to. lease_token identifies the RUN, not the home:
# a restarted desktop run must not quietly steal its own predecessor's lease.
# Every timestamp is written by datetime('now') inside the statement, i.e.
# the database's clock, because the two homes' clocks are not the same clock.
#
# Deliberately NOT keyed on listing_id, for the same reason as
# vision_batches: tables_child_first and delete_orphaned_rows both discover
# child tables by that column, and a lock enrolled in the delisting cascade
# would be deleted out from under a live run by an unrelated listing going
# away.
TURSO_SCHEMA_EXTRA = """
CREATE TABLE IF NOT EXISTS pipeline_lock (
    lock_name TEXT PRIMARY KEY,
    lease_token TEXT NOT NULL,
    held_by TEXT NOT NULL,
    acquired_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS vision_batches (
    batch_id TEXT PRIMARY KEY,
    garage_expected_by_id TEXT NOT NULL,
    submitted_at TEXT NOT NULL,
    submitted_by TEXT NOT NULL
);

-- Houses Ben has said no to. Keyed on the PROPERTY, deliberately.
--
-- Compass keys its own notInterested on the listing id, so a relist mints a
-- new one and the rejection is forgotten -- we would re-scrape, re-photograph
-- and re-pay for vision scoring on a house already rejected. The property id
-- survives that, which is the whole reason #86 chose it as the identity.
--
-- No foreign key, and it lives here rather than in _SCHEMA, for the same
-- reason as change_events and vision_batches: a rejection has to outlive
-- every listing of the house it is about, and an FK would enrol it in the
-- cascade it exists to survive.
--
-- The address is denormalised on purpose. A rejection outlives the listing it
-- was made against -- that is the entire point -- so by the time anyone reads
-- one back, nothing else in the database knows what `1272DA` was. Keying on
-- identity without carrying legibility made `--list` print six opaque
-- characters and a date.
--
-- `listing_ref` is the Compass listing id, captured at rejection time so the
-- rejection can be pushed back to Compass (#92). It has to be captured then,
-- because a rejected listing is dropped from the corpus on the very next run
-- -- that is what rejecting it does -- and property_ids goes with it. By the
-- time we want to tell Compass, nothing else in the database still knows
-- which listing the house was.
--
-- The name is `listing_ref`, not `listing_id`, for the same load-bearing
-- reason as change_events below: delete_orphaned_rows finds child tables by
-- looking for a `listing_id` column and deletes rows whose listing is gone.
-- Calling it `listing_id` would enrol the rejections table in a sweep that
-- deletes precisely the rejections that are doing their job.
CREATE TABLE IF NOT EXISTS rejections (
    property_id TEXT PRIMARY KEY,
    address TEXT,
    city TEXT,
    listing_url TEXT,
    listing_ref TEXT,
    reason TEXT,
    rejected_at TEXT NOT NULL,
    compass_synced_at TEXT,
    -- Why we stopped asking. "confirmed" means the listing was found in
    -- Compass's discarded pile. "absent" means Compass does not hold that
    -- listing id at all, so re-sending would never do anything. Both stop
    -- the retry, and only one of them is good news -- collapsing them into
    -- a bare timestamp is what let 5012 West 77th Drive read as confirmed.
    compass_sync_note TEXT
);

-- What changed in a run, so a later stage can report it.
--
-- Its own table because the report has to outlive the stage that produces
-- it: scrape knows what is new, but rank and composite do not exist until
-- score.py has run, and a digest worth reading needs both.
--
-- The column is `listing_ref`, not `listing_id`, and that is load-bearing.
-- delete_orphaned_rows discovers child tables by that column name and prunes
-- rows whose listing is gone -- which would delete every delisting event, the
-- one kind that is ABOUT a listing being gone. Naming it differently makes
-- the table invisible to that sweep with no exclusion list to keep in step,
-- and hand-maintained lists going stale is what orphaned a visual_scores row
-- per delisting for months.
CREATE TABLE IF NOT EXISTS change_events (
    event_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    listing_ref TEXT NOT NULL,
    detail TEXT,
    detected_at TEXT NOT NULL,
    notified_at TEXT
);

CREATE TABLE IF NOT EXISTS hosted_photos (
    listing_id TEXT NOT NULL,
    position INTEGER NOT NULL,
    blob_url TEXT NOT NULL,
    source_url TEXT,
    PRIMARY KEY (listing_id, position)
);
"""

def _strip_sql_comments(sql: str) -> str:
    """Remove `-- ...` line comments.

    Applied before both splitting statements and parsing columns, because a
    comment is ordinary SQL that both steps mis-read. A `;` inside one
    truncates the statement it documents, and a comment line inside a table
    body parses as a column named `--`, which reaches sqlite as
    `ALTER TABLE t ADD COLUMN -- ...` and fails with "incomplete input".

    Both happened within a minute of each other while adding one column, and
    neither error names comments -- so the schema was one careless sentence
    away from a migration that could not run.
    """
    return "\n".join(line.split("--", 1)[0] for line in sql.splitlines())


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
    for match in _CREATE_TABLE_RE.finditer(_strip_sql_comments(schema_sql)):
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
        for statement in _strip_sql_comments(fragment).split(";"):
            statement = statement.strip()
            if not statement:
                continue
            conn.execute(statement)
        _migrate_missing_columns(conn, fragment)

    if hasattr(conn, "commit"):
        conn.commit()


# A libsql stream is server-side state addressed by a "baton". The server
# drops an idle one, and the next statement then fails with
# "HTTP status 404: stream not found". The driver resets its baton on any
# failure and documents that the next statement opens a fresh stream,
# explicitly leaving the retry decision to the application -- this is that
# decision.
#
# Found by gate 4, not by reasoning: a sandbox scrape bulk-wrote 88 listings,
# spent minutes downloading photos, and died on the query_listings() that
# starts the upload step. It is timing-dependent, which is why the first run
# passed and the second failed. score_photos.py is the worst case -- a vision
# batch can idle for hours before it writes anything.
STREAM_GONE_MARKERS = frozenset({
    "stream not found",
    "stream expired",
    "invalid baton",
    "baton not found",
})


def is_stream_gone(exc: Exception) -> bool:
    """True when this error means the server dropped our stream, rather than
    the statement itself being wrong. Deliberately narrow: retrying a
    constraint violation or a bad table name just fails twice and muddies the
    traceback."""
    message = str(exc).lower()
    return any(marker in message for marker in STREAM_GONE_MARKERS)


class _StreamRecoveringConnection:
    """Retries a statement once when the server dropped an idle stream.

    Never retries inside a transaction. When a stream dies the server rolls
    its transaction back, so re-running one statement of a multi-statement
    write would commit a fragment of it -- a listings row whose amenities
    never landed. Failing loudly is correct there: every write in this
    project is an idempotent upsert, so repeating the run converges, while a
    half-applied batch does not announce itself.
    """

    def __init__(self, conn):
        object.__setattr__(self, "_conn", conn)

    def execute(self, sql, parameters=()):
        conn = self._conn
        in_transaction = getattr(conn, "in_transaction", False)
        try:
            return conn.execute(sql, parameters)
        except Exception as exc:
            if in_transaction or not is_stream_gone(exc):
                raise
            # The driver has already discarded the dead baton, so this call
            # opens a fresh stream. One retry only: a stream that will not
            # come back is a real outage, and retrying forever would hang.
            return conn.execute(sql, parameters)

    # Everything else is the underlying connection's own behaviour.
    def __getattr__(self, name):
        return getattr(self._conn, name)

    def __setattr__(self, name, value):
        setattr(self._conn, name, value)

    def __enter__(self):
        self._conn.__enter__()
        return self

    def __exit__(self, *exc_info):
        return self._conn.__exit__(*exc_info)


def with_stream_recovery(conn):
    """Wrap a connection so an expired stream costs one retry, not a run."""
    return _StreamRecoveringConnection(conn)


def stage_connection(
    env: Mapping[str, str] | None = None,
    connect_fn: Callable = turso_serverless.connect,
    *,
    sleep: Callable[[float], None] = time.sleep,
):
    """The database every stage reads and writes.

    This is the cutover in one function. Before it, each stage opened
    data/listings.db and publish.py mirrored the result up to Turso; now the
    stages talk to Turso directly and there is no second database to drift.

    ensure_schema() runs on every connect. It costs ~14 statements (~3.4s)
    per stage -- real, but small against a run measured in minutes, and it is
    what stops a new _SCHEMA column from silently never existing in the only
    database there is. The visual_scores orphan incident is what schema drift
    looks like when it goes unnoticed; three seconds a stage to make that
    structurally impossible is the right trade.

    This is also where a transient Turso failure actually surfaces in
    practice: connect() builds a Session/Connection object with no network
    call, so ensure_schema()'s first statement is every stage's real first
    round-trip. A blip there retries the whole thing -- fresh connection
    included, not just the schema check -- since a connection that failed
    partway through is not one worth reusing.
    """
    url, auth_token = _credentials(env)

    def _attempt():
        # One connect per attempt: going through connect() here would nest
        # its retry inside this one, 3 x 3 connects before a dead network
        # surfaced.
        conn = with_stream_recovery(_open(connect_fn, url, auth_token))
        try:
            ensure_schema(conn)
        except Exception:
            _close_quietly(conn)
            raise
        return conn

    return _retry_with_backoff(_attempt, sleep)


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


# SQLite's default limit on bound variables in one statement. Exceeding it
# does not degrade -- it fails outright, and it failed for real once:
# `listings` reached 23 columns when the structured fields landed, and a
# fixed chunk of 50 would have meant 1,150 variables and a broken write path.
MAX_SQL_VARIABLES = 999

# Upper bound on rows per statement regardless of how narrow the table is.
# Nothing here needs statements larger than this, and keeping them bounded
# keeps a single failed chunk cheap to retry row by row.
MAX_ROWS_PER_STATEMENT = 200


def chunk_size(column_count: int) -> int:
    """Rows per batched statement for a table of this width.

    Derived rather than fixed. A single constant has to be tuned for the
    widest table (`listings`, 23 columns), which then makes narrow tables pay
    for width they do not have: `amenities` has 2 columns, so a chunk of 30
    uses 60 of the 999 available variables and issues 17x more statements
    than it needs. At ~240ms per statement that is real minutes.
    """
    if column_count <= 0:
        return MAX_ROWS_PER_STATEMENT
    return max(1, min(MAX_ROWS_PER_STATEMENT, MAX_SQL_VARIABLES // column_count))


# Retained as the conservative rows-per-statement figure for the widest
# mirrored table; chunk_size() supersedes it for width-aware batching.
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
    rows_per_statement = chunk_size(len(columns))

    for start in range(0, len(rows), rows_per_statement):
        chunk = rows[start:start + rows_per_statement]
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
