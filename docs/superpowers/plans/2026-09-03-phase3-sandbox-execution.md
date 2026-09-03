# Phase 3 — Lift pipeline execution into a Vercel Sandbox

Date: 2026-09-03
Status: PLAN — for execution by subagents, one PR per task. Nothing here is
implemented.
Supersedes: §4/§9 "Phase 3" of `2026-08-31-architecture-options.md` and §5 of
`2026-08-31-vercel-pipeline.md`, both of which predate the gate results and
the current Sandbox platform and are wrong in the specifics called out in §2.
Companion: `2026-09-03-phase3-reference.md` (how the finished system works and
how to operate it).

## 0. Summary

The pipeline already speaks Turso natively (Phase 2). Phase 3 gives it a second
execution home that does not depend on Ben's laptop being on:

- A **cron in the short-list project** hits a **launcher** route, which
  resumes one **named, persistent Vercel Sandbox** (`home-search-pipeline`),
  syncs the public `home-search` repo to `main`, seeds the Compass session, and
  starts `pipeline.py` detached. It returns in seconds.
- A second cron hits a **reaper** route every 10 minutes. It reads two tiny
  marker files out of the sandbox, uploads the refreshed Compass session to the
  private Blob store, stops the sandbox when the run is done, and raises an
  alarm when a run has hung. A 3-hour `timeout` on the sandbox is the backstop.
- The runner is **`pipeline.py`, unchanged in shape**. The sandbox holds only
  the credentials the stages already need (Compass, Turso RW, photo Blob,
  Anthropic, revalidate). It holds **no Vercel credential and no private-store
  credential**: all Blob-state and Sandbox-control calls happen in the
  functions, where OIDC is automatic and auto-refreshing.
- The **vision-batch checkpoint moves into Turso**, not Blob. That is a
  deliberate deviation from issue #25 and is argued in §2.4.
- The **local JSON store is retired**: every gate it provided is derivable
  from Turso in one query each, and `data/listings.csv` / `data/gallery.html`
  become exports of Turso rather than of the store.
- **Photos become content-keyed.** `hosted_photos` gains `source_url`, and
  both the on-disk filename and the Blob pathname carry a hash of it. The
  "already have this photo" decision compares the freshly fetched URL set
  against what is hosted — not `(listing_id, position)`, which a production
  incident on 2026-09-03 showed is unsound across delist/relist and photo
  changes. Superseded and delisted blobs are deleted. A one-time orphan sweep
  of the store is scoped out to its own ticket.
- The **two-week reCAPTCHA canary** is built first, as the vertical slice
  through cron → function → sandbox → Chromium → Compass, and runs while the
  rest is built. The pipeline cron is only enabled after it passes.

Cost at 6-hourly: ≈ $2–6/month compute plus ≈ $0.30/month snapshot storage,
inside Pro's $20 credit.

Three milestones, 21 tasks. Ten tasks can start immediately in parallel
(§4.1).

Baseline at planning time (2026-09-03, `main` = `02ca03d`, 561 tests):
`hosted_photos` 3,255 rows (5,068 before today's orphan cleanup), zero
orphans in every child table, ≈100 listings and moving (a full run is in
flight from another session against the same database).

## 1. Architecture

### 1.1 Components and where state lives

```
 Vercel project: short-list (Next.js 16, production)
 ┌──────────────────────────────────────────────────────────────────────────┐
 │  vercel.json crons                                                       │
 │    0 */6 * * *   GET /api/pipeline/run           (Authorization: CRON_SECRET)
 │    */10 * * * *  GET /api/pipeline/reap                                  │
 │    30 3 * * *    GET /api/pipeline/run?job=canary (Milestone A only)     │
 │                          │                          │                    │
 │  proxy.ts: api/pipeline/* excluded from the /enter redirect  <-- TRAP    │
 │                          ▼                          ▼                    │
 │  ┌──────────────────────────────┐   ┌──────────────────────────────┐    │
 │  │ launcher route               │   │ reaper route                 │    │
 │  │ @vercel/sandbox (OIDC auto)  │   │ @vercel/sandbox (OIDC auto)  │    │
 │  │ @vercel/blob    (OIDC auto)  │   │ @vercel/blob    (OIDC auto)  │    │
 │  │ 1 getOrCreate(name)          │   │ 1 get(name, resume:false)    │    │
 │  │ 2 skip if run in progress    │   │ 2 read data/.run/{started,   │    │
 │  │ 3 git fetch/reset main       │   │     done}                    │    │
 │  │ 4 bootstrap.sh (idempotent)  │   │ 3 done  -> upload session,   │    │
 │  │ 5 seed compass_state.json    │   │           stop()  (snapshot) │    │
 │  │   from Blob if disk lacks it │   │ 4 hung  -> stop(), ntfy      │    │
 │  │ 6 runCommand(run.py,detached,│   │ 5 idle  -> no-op             │    │
 │  │   env=<allowlisted secrets>) │   └───────────┬──────────────────┘    │
 │  └───────────┬──────────────────┘               │                        │
 └──────────────┼──────────────────────────────────┼────────────────────────┘
                │ Sandbox API (project-scoped OIDC) │
                ▼                                  ▼
 ┌──────────────────────────────────────────────────────────────────────────┐
 │  Vercel Sandbox  name=home-search-pipeline  persistent  iad1  2 vCPU/4GB │
 │  timeout 3h per session; keepLastSnapshots {count:1}                     │
 │  /vercel/sandbox            <- git clone of BenGiese22/home-search @main │
 │    venv/  + Chromium        <- bootstrap.sh, survives via snapshot       │
 │    data/photos/<id>/NN-<hash8>.jpg <- cache: only photos not yet hosted │
 │    data/.auth/compass_state.json   <- session (seeded/collected by fns)  │
 │    data/.run/{started,done}        <- markers written by ops/sandbox/run.py
 │    data/logs/                      <- per-run stage logs                 │
 │                                                                          │
 │  ops/sandbox/run.py  ──flock──>  python pipeline.py --max-age=5h         │
 │        scrape.py -> compute_commutes.py -> score_photos.py -> score.py   │
 │        -> POST short-list /api/revalidate -> ntfy summary                │
 └───┬────────────────┬───────────────────┬───────────────────┬─────────────┘
     │ Turso RW       │ Blob RW (photos)  │ Anthropic Batches │ Compass /
     ▼                ▼                   ▼                   │ Nominatim /
 ┌────────────┐  ┌──────────────┐  ┌──────────────┐           │ OSRM (egress
 │ Turso (SSOT│  │ Blob public  │  │ vision API   │           ▼ from iad1)
 │ + vision_  │  │ short-list-  │  │ (paid; never │
 │   batches  │  │ photos       │  │  twice)      │
 │   checkpt) │  └──────┬───────┘  └──────────────┘
 └─────┬──────┘         │
       │ RO token       │ public URLs          ┌─────────────────────────────┐
       ▼                ▼                      │ Blob PRIVATE                │
 ┌──────────────────────────────┐              │ home-search-state           │
 │ short-list viewer (unchanged │              │  state/compass_state.json   │
 │ 'use cache' + cacheTag)      │              │ written by: reaper (OIDC),  │
 └──────────────────────────────┘              │   ops/state.py push (laptop)│
                                               │ read by: launcher (OIDC),   │
 Laptop (second home, unchanged):              │   ops/state.py pull         │
   systemd timer -> pipeline.py -> same Turso  └─────────────────────────────┘
```

### 1.2 One run, end to end

```mermaid
sequenceDiagram
    autonumber
    participant Cron as Vercel Cron
    participant L as /api/pipeline/run
    participant SB as Sandbox home-search-pipeline
    participant B as Blob private (state)
    participant T as Turso
    participant R as /api/pipeline/reap
    participant V as short-list viewer
    participant N as ntfy.sh

    Cron->>L: GET (Authorization: Bearer CRON_SECRET)
    L->>SB: Sandbox.getOrCreate({name, source: git main, timeout: 3h})
    Note over SB: first time only: clone + bootstrap (venv, Chromium ~25s)
    L->>SB: readFileToBuffer(data/.run/started, data/.run/done)
    alt started exists and done does not
        L-->>Cron: 200 {skipped: "in-progress"}
    else
        L->>SB: git fetch --depth 1 origin main && git reset --hard origin/main
        L->>SB: bash ops/sandbox/bootstrap.sh   (no-op when satisfied)
        L->>SB: exists? data/.auth/compass_state.json
        opt disk has no session
            L->>B: get(state/compass_state.json, access: private)   [OIDC]
            L->>SB: writeFiles(data/.auth/compass_state.json)
        end
        L->>SB: runCommand({cmd: venv/bin/python, args: [ops/sandbox/run.py, pipeline], env: secrets, detached: true, timeoutMs: 3h})
        L-->>Cron: 202 {started: true}
    end
    SB->>SB: run.py: flock, write data/.run/started
    SB->>T: scrape (batched writes), commutes, score_photos, score
    SB->>T: vision_batches: INSERT at submit, DELETE at consume
    SB->>V: POST /api/revalidate
    SB->>N: run summary (ok / FAILED stage X)
    SB->>SB: run.py: write data/.run/done {exit, finished_at}
    loop every 10 min
        Cron->>R: GET
        R->>SB: Sandbox.get({name, resume: false}); status?
        alt running and done marker present
            R->>SB: readFileToBuffer(data/.auth/compass_state.json)
            R->>B: put(state/compass_state.json, access: private)   [OIDC]
            R->>SB: stop()  (filesystem snapshotted)
        else running, started > 3h ago, no done
            R->>SB: stop()
            R->>N: "pipeline hung; stopped"
        else stopped / no markers
            R-->>Cron: 200 {idle}
        end
    end
```

### 1.3 State, and who moves it

| State | Where it lives | Written by | Read by | If lost |
| --- | --- | --- | --- | --- |
| Listings, scores, commutes, visual scores, hosted_photos | Turso (SSOT) | every stage | viewer, every stage | n/a — it is the system of record |
| **Vision batch checkpoint** (`vision_batches`) | **Turso** | `score_photos.py`, immediately at submit | `score_photos.py` on the next run, from either home | **double payment** — this is why it is in the SSOT, not on a disk or in Blob |
| Compass session `compass_state.json` | sandbox disk (snapshotted) + private Blob (durable copy) | `src/auth.py` every run; reaper uploads to Blob after each run; `ops/state.py push` from laptop | launcher seeds disk from Blob when disk lacks it | cold login next run (works per spikes; kept rare) |
| Photos on disk `data/photos/<id>/NN-<hash8>.jpg` | sandbox disk (cache) | `scrape.py`, only for photos whose `(listing_id, position, source_url)` is not yet hosted | `score_photos.py`, upload | re-downloaded for exactly the photos still missing from `hosted_photos` |
| Hosted photo index `hosted_photos(listing_id, position, blob_url, source_url)` | Turso | upload step, one batched write per chunk | the "do we already have this photo" gate in both homes; the viewer | the only handle on what exists in Blob — pruning a row without deleting its blob strands the blob (§2.8) |
| `data/.run/started`, `data/.run/done` | sandbox disk | `ops/sandbox/run.py` | launcher (skip), reaper (stop/alarm) | reaper falls back to the 3h age rule; `timeout` is the hard stop |
| venv + Chromium | sandbox disk (snapshot) | `bootstrap.sh` | runner | rebuilt in ≈1–2 min by the idempotent bootstrap |
| Secrets | short-list project env (`vercel env add`) | Ben | launcher → `runCommand({env})`, per run, never on disk | rotate in the dashboard |

## 2. Decisions and rationale

Each decision states what was **verified** (read in code or current docs on
2026-09-03) and what is **assumed**.

### 2.1 JS or Python SDK? Both, each where it is native; the sandbox needs neither

- The trigger must be a Vercel Function in the short-list project (cron →
  route). short-list is Next.js 16, so the launcher and reaper are TypeScript
  route handlers using `@vercel/sandbox@^3.2` and `@vercel/blob@^2.8`.
  **Verified:** `@vercel/sandbox` 3.2.1 exports `Sandbox.getOrCreate`,
  `runCommand({detached: true})` → `Command`, `readFileToBuffer` →
  `Buffer | null`, `writeFiles`, `stop()`, `source: {type: 'git', url,
  revision?, depth?}`, per-command `timeoutMs` (read from the package's
  `.d.ts`).
- The runner is `pipeline.py`. It needs no Vercel SDK because nothing inside
  the sandbox talks to Vercel's control plane (see 2.5). So the `vercel` pip
  package (which drags in pydantic, httpx, anyio, cbor2, websockets) stays
  **out of `requirements.txt`**.
- The Python SDK (`vercel.sandbox`, verified present in `vercel==0.10.0`,
  Python ≥3.10) is the right tool for **operator scripts** in home-search:
  a one-off supervised run against a throwaway DB (what gate 4 was), and
  local debugging. It goes in `ops/requirements-ops.txt`.
- The plan's reason for assuming JS ("Workflow DevKit is TypeScript-only")
  never applied to Sandbox. But the conclusion — TypeScript in the function —
  stands for a different reason: the function lives in a Next.js project.

### 2.2 The JSON store: retire it, derive every gate from Turso

`src/store.py` provides four things to `scrape.py` (and `check.py`,
`backfill_photos.py`, `src/diff.py`): `is_scraped`, `needs_field_backfill`,
`save_listing`/`delete_stored_listing`, and `load_all_listings` for the CSV
and gallery. In a sandbox with a fresh disk every one of them misbehaves
(gate 4 produced a two-listing gallery; `is_scraped()` would trigger a full
photo re-download). Persistence (2.3) would paper over this by keeping
`data/listings/*.json` on the snapshot, but the first run, every snapshot
loss, and every laptop-vs-cloud divergence would still hit it. The gates have
to come from the shared database.

Options weighed:

| Option | Behaviour | Verdict |
| --- | --- | --- |
| Scope out (skip CSV/gallery and treat everything as unscraped when running in a sandbox) | Two code paths; every cloud run re-downloads ≈3,250 photos (bot exposure, ~20+ min); gallery divergence persists | No |
| Hybrid (union of disk and Turso signals; keep JSON writes) | Keeps a dual-write nobody reads in the cloud; store rots exactly the way the plan said `publish.py` would | No |
| **Port to Turso and delete the store** | One code path, one query per gate, CSV/gallery become exports of the SSOT, `-450` lines | **Yes** |

Verified: every field in `Listing` (`src/models.py`) is a column in
`listings` or a row in `amenities`/`photo_urls`, and `score.py` already
rebuilds `Listing` objects from Turso rows (`_row_to_listing`). The store
holds nothing Turso lacks.

The replacements, each **one statement** (round-trip counts asserted in tests):

- `is_scraped(id)` → a **content comparison**, not an existence check.
  `hosted_photo_index(conn)` reads `hosted_photos` once (it is the same
  single query `collect_pending_photos` already issues for the upload skip
  set — share it) into `{(listing_id, position): source_url}`. A freshly
  fetched listing needs photo work iff any `(id, i, url)` from its current
  `photo_urls` is absent from that index; a listing with zero URLs needs
  none. Pure function, no round-trip per listing, and it is **sound across
  delist/relist and photo changes** because the Compass CDN URL (which embeds
  a content hash) is the identity, not the slot number. See §2.8 for why the
  positional key had to go. For the explicit-URL loop's *pre-fetch* skip
  (before the detail page is scraped there are no URLs to compare) the
  weaker "has any hosted row, or has no `photo_urls` rows in Turso" check is
  used, then the strong check after scraping.
  *Rejected alternative:* a `photos_fetched_at`/generation column on
  `listings` — `bulk_upsert_listings` is `INSERT OR REPLACE` and would wipe
  it every run unless the core write path changed to `ON CONFLICT DO UPDATE`,
  and a generation counter still cannot see a photo change that happens
  without a delist. The URL comparison can.
- `needs_field_backfill(id, fields)` → `get_listing_ids_missing_fields(conn,
  fields)`: `SELECT listing_id FROM listings WHERE hoa_annual IS NULL OR ...`.
  Strictly better than parsing JSON (same null-means-stale rule).
- `load_all_listings()` → `query_listings` + `get_amenities_by_listing` +
  new `get_photo_urls_by_listing` (3 statements, ≈1 s) feeding
  `write_csv`/`write_gallery` unchanged. `render_gallery` already tolerates a
  missing photo directory.
- `save_listing` / `delete_stored_listing` → deleted; `bulk_upsert_listings`
  and `bulk_delete_listings` already do the durable equivalent.
  `apply_delisting`/`run_delisting` lose their `store_dir` parameter.
- `backfill_db.py` (JSON → Turso) and `backfill_hoa.py` (rewrites JSON)
  exist only to serve the store. Recommendation: delete them in the same
  commit that deletes `src/store.py`; they remain in git history. **Ben's
  call** — the lower-risk alternative is to leave them and the module they
  import, clearly marked historical, but that is exactly the "safety net
  that silently rots" the Phase 2 plan argued against for `publish.py`.
- `data/listings/*.json` on the laptop is untouched by any of this (the
  directory is gitignored data). Nothing deletes it.

Consequence worth stating plainly: after this change **`BLOB_READ_WRITE_TOKEN`
is required for incremental scraping in both homes** — a run without it
uploads nothing, so nothing is ever marked scraped. Locally the token is
already in `.env`; `scrape.py` should now fail fast when it is missing rather
than print "skipping photo upload".

### 2.3 One named persistent sandbox, not snapshots-with-Chromium

**Verified (docs 2026-08-25):** persistence is the default for every
`Sandbox.create`/`getOrCreate`; stopping (including by timeout, per the KB
"duration and persistence" guide) snapshots the filesystem; the next resume
boots from it with a fresh session timeout; snapshots expire 30 days after
last use by default; `keepLastSnapshots: {count: 1}` keeps storage flat; a
sandbox whose snapshot expired is transparently recreated by `getOrCreate`
(and `onCreate` fires again). Pro max session 24 h. Sandboxes on SDK ≥3 get
64 GB of disk.

The 2026-08-31 plan's "admin script bakes a snapshot with Chromium, runner
falls back to cold install if it 404s" is therefore **obsolete**: the platform
does it. And with Chromium at ~25 s (measured) the fallback costs almost
nothing anyway.

Why one **named** sandbox (`home-search-pipeline`) rather than a fresh
anonymous one per run:

1. **Mutual exclusion for free.** Two launcher invocations (cron double
   delivery is documented as possible) resume the *same* VM, so
   `pipeline.py`'s existing `flock` on `data/.pipeline.lock` does its job.
   Two anonymous sandboxes would each hold a *separate* view of "what is
   in flight" and could both submit vision batches — the one failure mode
   the never-pay-twice guarantee exists to prevent. The launcher also skips
   when `data/.run/started` exists without `data/.run/done`.
2. venv, Chromium, the photo cache and the session survive between runs.
3. `sandbox connect home-search-pipeline` gives Ben a shell into the exact
   environment the cron uses.

Costs of the choice: snapshot storage (image + Chromium + venv + ≤1 GB
photos ≈ 3–4 GB × $0.08/GB-mo ≈ **$0.30/mo**); a few seconds of
snapshot/restore per run; and "environment drift" — mitigated by the
bootstrap script being idempotent and by `git reset --hard origin/main` on
every resume (`data/` is gitignored, so it survives).

**Assumed:** a resumed session inherits the sandbox's configured 3 h
`timeout`. If it turns out to inherit the 5-minute default instead, the
launcher must call `sandbox.extendTimeout()` after resume — Task A4's
acceptance criteria include checking `sandbox.timeout` after resume.

### 2.4 State round-trips: checkpoint to Turso, session to Blob — via the functions

Issue #25 says both files "round-trip through Blob". The brief adds: OIDC,
not a second long-lived token. Two facts, both verified today, make the
literal reading of that unworkable, and one of them argues for a better
home for the checkpoint.

**Fact 1 — OIDC tokens are short-lived and unrefreshable from inside a
sandbox.** Vercel Functions get an OIDC token with a 2-hour TTL, and *reuse
the same token for up to 90 minutes*, so a token handed to the sandbox at
launch has anywhere between 30 minutes and 2 hours left. Nothing inside the
sandbox can mint a new one (the sandbox has no Vercel credential — that is
the point). `score_photos.py` legitimately runs for hours. So "the sandbox
writes its checkpoint to Blob with OIDC" fails exactly on the long runs
where it matters, and the plan's "runner's last act is to stop its own
sandbox with the forwarded OIDC token" fails the same way. **The OIDC path
only works if the Blob and Sandbox calls are made by the functions**, where
the SDKs read `VERCEL_OIDC_TOKEN`/`x-vercel-oidc-token` and refresh it
automatically. (The `@vercel/blob` docs explicitly warn against passing an
OIDC token explicitly for this reason.)

**Fact 2 — both homes share one database, so a per-home checkpoint is a
double-pay hazard.** A laptop run that submits a batch and is interrupted
(lid closed) leaves its checkpoint in a local file. The next cron run sees
listings with no `visual_scores` row and *no* checkpoint (Blob would hold
only whatever the cloud last wrote) and resubmits them. Real money, and it
will eventually happen once both homes are live. A file — on disk or in
Blob — cannot fix this; the record of "already submitted" has to live where
both homes look: **Turso**.

Decision:

- **`vision_batches` table in Turso** (`batch_id TEXT PRIMARY KEY`,
  `garage_expected_by_id TEXT` JSON, `submitted_at`, `submitted_by`
  hostname/`sandbox`). `score_photos.py` loads it in one `SELECT`, appends
  one row per submitted batch in one `INSERT` (the same instant the batch is
  created — zero window), and deletes the row after consuming results.
  ≈3 round-trips per run. Declared in `TURSO_SCHEMA_EXTRA` alongside
  `hosted_photos` (it has no `listing_id`, so it must stay out of `_SCHEMA`,
  which `tables_child_first`/`delete_listing` iterate with `WHERE listing_id`).
  A one-time migration imports a legacy `data/.photo_scoring_batch_state.json`
  if present, then removes it. The reaper never has to touch it, and a
  sandbox killed at the 3 h limit loses nothing.
- **`compass_state.json` → private Blob store `home-search-state`, moved by
  the functions.** Launcher: if the sandbox disk has no session (first run,
  snapshot expired), `get('state/compass_state.json', {access:'private',
  storeId})` and `writeFiles` it in. Reaper: after every finished run,
  `readFileToBuffer` the (always re-saved by `src/auth.py`) session and `put`
  it. Disk wins over Blob when both exist, because the disk copy is newer.
  Manual recovery path: after a local login, `ops/state.py push` uploads the
  laptop's session with `BLOB_STATE_READ_WRITE_TOKEN` (already in Ben's
  `.env`, per commit `c32725f`). That is the one legitimate remaining use of
  the RW token and of `src/blob_state.py`, and it stays on the laptop.
- The sandbox therefore holds **no Vercel token and no private-store
  token**. Its env is exactly what a laptop run has, minus the RW state token.

Deviation from #25, stated for the record: the checkpoint does *not*
round-trip through Blob. It round-trips through the SSOT, which is stronger
on every axis that matters (window, credentials, cross-home).

### 2.5 Self-stop: a reaper cron, not a token in the sandbox

Provisioned memory bills for the whole session, not for CPU use: a 20-minute
run that idles to a 3-hour limit wastes 4 GB × 2.7 h × $0.0212 ≈ $0.23, ×4/day
≈ **$28/month** — more than the entire Pro credit. So something must stop the
sandbox promptly. Fact 1 above rules out the plan's self-stop. The
alternatives:

| Mechanism | Idle waste | Credentials in sandbox | Verdict |
| --- | --- | --- | --- |
| Runner calls `stop()` with forwarded OIDC | none | OIDC token (expires mid-run) | fails on long runs |
| Runner calls `stop()` with a `VERCEL_TOKEN` | none | a **team-wide** Vercel access token, in a VM that runs Chromium against a third-party site | worst blast radius of any option; also "a second long-lived token" |
| Function awaits the run | none | none | impossible: 800 s (1800 s beta) cap |
| **Reaper cron every 10 min** | ≤10 min ≈ $0.014/run | **none** | **Yes** |
| Vercel Workflow (durable TS orchestration) | none | none | correct but a new runtime concept in short-list for 20 lines of cron; revisit if the reaper ever feels inadequate |

Backstops, layered: `timeout: 3h` on the sandbox (hard platform kill,
snapshots on the way out), `timeoutMs: 3h` on the detached command, and the
reaper's own rule "started > 3 h ago and no done marker → stop and alert".

### 2.6 Notification

`NTFY_TOPIC` env var (treated as a secret: whoever knows the topic can read
and post). `src/notify.py` posts to `https://ntfy.sh/<topic>` with an
injected `post`, never raises. `pipeline.py` owns the per-run message (it
knows which stage failed and how long things took) so laptop runs get it too;
the reaper posts only for what the runner cannot report: hung runs, sandbox in
`failed` state, launcher exceptions.

### 2.7 The canary is the first deliverable, not the last gate

The canary needs: cron, proxy exclusion, launcher, reaper, bootstrap, the
session seeded into the sandbox, Chromium, and a warm-session collection
fetch. That is 80 % of the mechanism at 1 % of the runtime (≈1 min/day,
≈$0.01/run). Building it first means the integration risk is retired in
week 1, the two-week clock starts immediately, and the pipeline changes
(Milestone B) proceed in parallel with nothing depending on them. The
pipeline cron is added only when the canary has passed.

### 2.8 Photo identity and blob lifecycle (added after two production findings, 2026-09-03)

**Finding 1 — the positional skip key is unsound.** `collect_pending_photos`
skips a photo when `(listing_id, position)` is already in `hosted_photos`.
A listing can be delisted and later relist under the same `listing_id` with
different photos in the same positions, and a live listing can have its
photos re-shot or reordered. In both cases the upload is skipped and the
viewer keeps serving the old images with nothing failing. Caught for real:
6085 West 82nd Drive came back with 44 stale rows from its previous listing
(they predated the hosted_photos prune in `e4b38d1` and were cleaned by
hand). The local disk cache (`NN.jpg`) and the old JSON `is_scraped()` have
the identical flaw. Replacing `is_scraped()` with any Turso set keyed on
`(listing_id, position)` would have extended the flaw into the gate that
decides whether to download at all, so this plan does not do that.

Decision — **make the photo's source URL its identity**, priced at ≈ one M
task (B0) plus small touches in B1/B2:

- `hosted_photos` gains `source_url TEXT` (`ALTER TABLE ... ADD COLUMN`,
  which `ensure_schema`'s `_migrate_missing_columns` already applies to
  `TURSO_SCHEMA_EXTRA` fragments — verified). Backfilled once from
  `photo_urls` by `(listing_id, position)` join. That backfill *assumes* the
  current rows match the current URLs; after today's cleanup and with the
  prune-on-delist path in place the residual risk is "a photo changed without
  a delist between its upload and today", which is bounded and historical.
  `ops/rehost_photos.py --all` is the paranoid alternative (one overnight
  laptop run: ≈3,255 downloads, ≈700 MB, ≈$0.02 of Blob operations).
- Filename and Blob pathname carry a hash: `data/photos/<id>/NN-<hash8>.jpg`
  and `photos/<id>/NN-<hash8>.jpg` where `hash8 = sha1(source_url)[:8]`.
  Two consequences, both wanted: the disk skip becomes sound, and a changed
  photo gets a **new blob URL** instead of overwriting the old pathname — an
  overwrite would sit behind Blob's CDN cache (default `cacheControlMaxAge`
  one month) and the viewer would keep showing the old image anyway.
- `collect_pending_photos` skips on `(listing_id, position, source_url)`;
  the hosted-row write carries `source_url`; the round-trip count is
  unchanged (one read, chunked writes).
- Globs move from `*.jpg` to the `??-????????.jpg` shape so a leftover
  old-format file is never counted or scored; `ops/migrate_photo_files.py`
  renames the laptop's existing `NN.jpg` files by the same join (or Ben
  deletes `data/photos/` and lets the next run rebuild it).

**Finding 2 — nothing in this project can delete a blob.** `hosted_photos.
blob_url` is the only record of what exists in the store; pruning rows
strands blobs. 1,813 orphaned rows (≈371 MB) had accumulated and were
exported to `data/archive/orphaned-hosted-photos-20260903.json` before being
deleted. On Pro the storage cost is negligible and nothing here is designed
around it — but a scheduled cloud runner delists more often than a laptop
does, and content-keyed pathnames add a new source of orphans (every photo
change). Scope call:

- **In Phase 3 (B0):** `src/blob_upload.py::delete_blobs(urls, token)` (the
  SDK's `del()` — `POST {BLOB_API_URL}/delete` with `{urls}`; confirm the
  path and header names against `@vercel/blob@2.8.0`'s dist exactly as
  `BLOB_API_URL` was), called in two places: after a superseded photo's
  replacement upload succeeds, and in `apply_delisting` *before*
  `bulk_delete_listings` prunes the rows (read the blob URLs in the same
  statement that the prune already needs — no new round-trip). Failures to
  delete are logged, never fatal: a stranded blob is cheap; a failed run is
  not. Rule: **never delete a `hosted_photos` row without first deleting or
  exporting its blob.**
- **Out of Phase 3, its own ticket:** a one-time orphan sweep (`list` the
  store under `photos/`, diff against `hosted_photos`, delete the rest) and
  purging the 1,813 archived URLs. It needs `delete_blobs` from B0 and
  nothing else, and it runs from the laptop.

### 2.9 Verified vs assumed — consolidated

Verified today: everything in 2.1 and 2.3; `@vercel/blob` credential
resolution order (explicit `token` > OIDC `oidcToken`/`storeId` or env
`VERCEL_OIDC_TOKEN`/`BLOB_STORE_ID` > `BLOB_READ_WRITE_TOKEN`); connecting a
store to a project via OIDC injects `BLOB_STORE_ID` (+ `VERCEL_OIDC_TOKEN`,
`BLOB_WEBHOOK_PUBLIC_KEY`) and **not** a RW token; the dashboard offers an
env-var prefix in "Advanced Options" when a store is created from a project;
Pro crons run per-minute, only on production, GET, `Authorization: Bearer
$CRON_SECRET` set automatically, no retries, best-effort delivery with
possible duplicates, redirects not followed; function max 800 s GA / 1800 s
beta; Sandbox pricing and quotas as in §0; the private state store exists
(`store_Ce1Z8XXqZxnFXJrl`, iad1) and `src/blob_state.py` has never been run
against it; short-list's `TURSO_AUTH_TOKEN` is **read-only** by design
(`docs/DEPLOYMENT.md`), so the launcher needs a separate RW token variable;
`proxy.ts` redirects every path except `enter`, `api/revalidate`, `_next/*`,
`favicon.ico`; Next.js requires the matcher to be a literal.

Assumed (each has a task that confirms it): the prefix option also appears in
the store's *Projects → Connect to Project* dialog (if not, accept
`BLOB_STORE_ID` and pass `storeId` explicitly — the collision the brief
feared is with the RW token name, which OIDC connect does not create);
resumed sessions inherit the configured timeout; the short-list project has
"Secure backend access with OIDC federation" enabled (it is the default for
projects created recently; check Settings → Security); `git` and `sudo` are
present in `vercel/sandbox/universal` (spikes used both).

## 3. Contradictions found while verifying

Recorded so the executing agents do not re-derive them and so Ben can
correct me where I am wrong.

1. **Gate 4 is recorded as BLOCKED in `docs/journal/decisions.md` but has in
   fact run.** `src/turso_db.py` says the stream-expiry bug was "found by
   gate 4, not by reasoning: a sandbox scrape bulk-wrote 88 listings ... and
   died on the query_listings() that starts the upload step" (PR #51). The
   journal entry predates that run and needs a follow-up (Task B7). The
   brief's "three stages, EXIT=0" implies `score-photos` was not part of the
   gate — reasonable, it costs money — so the vision stage has never run in
   a sandbox. Milestone C's supervised first run is where that happens, and
   the Turso checkpoint (B4) must be in place before it.
2. **"OIDC for Compass session storage" cannot mean "the sandbox uses
   OIDC".** See 2.4, Fact 1. The decision is honoured by moving the I/O into
   the functions.
3. **#25's "checkpoint round-trips through Blob" is weaker than a Turso
   checkpoint** and leaves a real cross-home double-pay hole (2.4, Fact 2).
4. **The 2026-08-31 plan's snapshot strategy, cold-install fallback, and
   OIDC self-stop are all obsolete or non-viable** (2.3, 2.5).
5. **short-list does not "already hold" a usable Turso token** for the
   pipeline: its token is read-only on purpose. A `PIPELINE_TURSO_AUTH_TOKEN`
   (RW) must be added to the short-list project env.
6. **`.env.example` calls `BLOB_STATE_READ_WRITE_TOKEN` "Phase 3 only"** and
   implies the runner uses it. Under this plan it is laptop-only (ops
   tooling). Update the comment (B7).
7. Minor: `@vercel/sandbox` is at **3.x**, not the 1.x the older SDK docs
   describe; `Sandbox.get({sandboxId})` is v1 API — use `{name}`.
8. Minor: the brief counts 17 JSON-store references in `scrape.py`; by call
   site it is 12 plus the import and `STORE_DIR`. Immaterial to the decision.
9. **The original brief's "`is_scraped()` → one scraped-ID-set query" would
   have re-created the positional-key flaw** at the download gate (§2.8).
   The gate is a URL comparison instead; still one query.
10. **`hosted_photos` was 5,068 rows in earlier notes; it is 3,255** after the
    2026-09-03 orphan cleanup. Any sizing that used the old figure is 36 %
    high.

## 4. Task breakdown

Conventions: every task is one PR on a `bgiese/phase3-<slug>` branch cut from
`origin/main`, conventional commits, TDD (tests first, watch them fail).
`./venv/bin/python -m pytest -q` must stay green in home-search (561 on
`main` at `02ca03d`);
`npm test` and `npx tsc --noEmit` in short-list. Sizes: S ≤ 2 h, M ≤ half a
day, L ≤ a day. "Repo" says which checkout the task touches; nothing touches
both. **short-list is a live production site — every short-list PR gets a
preview deployment checked before merge.**

### 4.1 Dependency graph

```
Milestone A (canary slice)              Milestone B (cloud-ready pipeline)
  A1 proxy exclusion        ─┐            B0 content-keyed photos + delete_blobs ─┐
  A2 vercel.json (canary)    │            B1 db queries ─────────────────────────┴─> B2 scrape.py port ──> B3 delete store
  A3 pure helpers ──> A4 launcher         B4 vision_batches checkpoint  (independent)
                  └─> A5 reaper           B5 pipeline.py notify   (needs A8)
  A6 sandbox scripts        ─┤            B6 ops/sandbox_run.py  (needs A6; optional)
  A7 canary script (needs A8)│            B7 docs/journal/.env.example (needs B3, B4)
  A8 src/notify.py          ─┤
  A10 ops/state.py          ─┘          Milestone C (go-live, after gate 1 passes)
  A9 Ben: Vercel setup + first canary     C1 pipeline crons in vercel.json (needs all A, B0–B5)
     (needs A1–A8, A10)                   C2 Ben: supervised first cloud run, retire timer
                                          C3 short-list docs (needs C1)
  Out of phase: blob orphan sweep ticket (needs B0's delete_blobs)
```

**Start immediately, in parallel (no dependencies):** A1, A2, A3, A6, A8,
A10, B0, B1, B4. Then A4 + A5 (after A3), A7 (after A8), B2 (after B0 and
B1), B5 (after A8). Then A9 (Ben), B3, B6. Then B7. Then C1–C3 after the
canary passes. B0 and B1 touch different functions in `src/db.py`; merge B0
first and rebase B1 if both land the same day.

### 4.2 Milestone A — the canary vertical slice

#### A1 — Exclude the pipeline routes from the auth redirect (short-list, S)

- Files: `proxy.ts`, new `proxy.test.ts`.
- Change: the matcher's negative lookahead gains `api/pipeline(?:/|$)` (one
  entry covers `run` and `reap`). It must stay a **literal** — Next.js ignores
  computed matchers.
- Tests (vitest): import `config` from `./proxy`; build
  `new RegExp('^' + config.matcher[0] + '$')`; assert `/api/pipeline/run`,
  `/api/pipeline/reap`, `/api/revalidate`, `/enter` do **not** match and `/`,
  `/listing/123` do. Add a comment that this approximates path-to-regexp
  semantics and that A9 confirms with a real request.
- Acceptance: `npm test` green; preview deployment: `curl -sI
  https://<preview>/api/pipeline/run` returns **401** (from A4) or **404**
  (before A4) — never **307** to `/enter`. Cron invocations do not follow
  redirects, so a 307 here is a silent total failure.

#### A2 — `vercel.json` with the canary cron (short-list, S)

- Files: new `vercel.json`.
- Content: `{"$schema": "https://openapi.vercel.sh/vercel.json", "crons":
  [{"path": "/api/pipeline/run?job=canary", "schedule": "30 3 * * *"},
  {"path": "/api/pipeline/reap", "schedule": "*/10 * * * *"}]}` — verify
  that a query string is accepted in `path` (docs show plain and dynamic
  paths; if rejected, use `/api/pipeline/canary` as a thin alias route that
  calls the same launcher with `job='canary'`).
- Acceptance: `vercel crons ls` lists both after deploy; the Cron Jobs
  settings page shows them; `vercel crons run /api/pipeline/reap` returns
  `{idle: true}` once A5 is live.

#### A3 — Pure helpers for the two routes (short-list, S–M)

- Files: new `lib/pipeline/env.ts`, `lib/pipeline/auth.ts`,
  `lib/pipeline/markers.ts`, `lib/pipeline/reap-decision.ts` and their
  `.test.ts`.
- `env.ts`: `buildRunnerEnv(source: NodeJS.ProcessEnv): Record<string,string>`
  — an **allowlist**: `COMPASS_EMAIL`, `COMPASS_PASSWORD`,
  `COMPASS_COLLECTION_URL`, `COMPASS_COLLECTION_TABS?`, `LISTING_URLS?`,
  `TURSO_DATABASE_URL`, `TURSO_AUTH_TOKEN` ← **from
  `PIPELINE_TURSO_AUTH_TOKEN`** (RW; the viewer's own is RO),
  `BLOB_READ_WRITE_TOKEN` (photo store), `ANTHROPIC_API_KEY`,
  `REVALIDATE_SECRET`, `SHORT_LIST_URL` ← `https://${VERCEL_PROJECT_PRODUCTION_URL}`,
  `NTFY_TOPIC?`, `MAX_PHOTOS_PER_LISTING?`, plus `PYTHONUNBUFFERED=1`,
  `HOME_SEARCH_HOME=sandbox`. Throws listing the *names* of missing required
  vars; never includes values in any error or log.
- `auth.ts`: `isCronAuthorized(header, secret)` — false when secret unset
  (the same guard `app/api/revalidate/route.ts` uses).
- `markers.ts`: `parseStarted(buf)`, `parseDone(buf)` for the JSON the runner
  writes (`{started_at, job}` / `{exit_code, finished_at, job}`); tolerate
  null/garbage → `null`.
- `reap-decision.ts`: `decideReap({status, started, done, now, maxAgeMs})`
  → `'noop' | 'collect-and-stop' | 'stop-hung' | 'alert-failed'`. Pure, and
  where the interesting behaviour lives; test every branch including
  `started` present + `done` absent + age > 3 h, sandbox `failed`, both
  markers absent while `running` (bootstrap still going → `noop`).
- Acceptance: all branches unit-tested; `buildRunnerEnv` test asserts the
  output has exactly the allowlisted keys and that an error message contains
  a key name but never a value.

#### A4 — Launcher route (short-list, M)

- Files: new `app/api/pipeline/run/route.ts`; `package.json` gains
  `@vercel/sandbox@^3.2.1` and `@vercel/blob@^2.8.0`.
- Behaviour (GET; `export const maxDuration = 300`; `export const dynamic =
  'force-dynamic'`):
  1. `isCronAuthorized` else 401.
  2. `job = searchParams.get('job') ?? 'pipeline'`; only `pipeline|canary`.
  3. `Sandbox.getOrCreate({name: 'home-search-pipeline', source: {type:'git',
     url: 'https://github.com/BenGiese22/home-search.git', revision:
     process.env.PIPELINE_GIT_REVISION ?? 'main', depth: 1}, resources:
     {vcpus: 2}, timeout: 3*60*60*1000, keepLastSnapshots: {count: 1},
     tags: {app: 'home-search'}, onCreate: bootstrap})`.
  4. Read `data/.run/started` and `data/.run/done`; if a run is in progress
     → `200 {skipped: 'in-progress'}`.
  5. `runCommand git fetch --depth 1 origin <rev> && git reset --hard
     FETCH_HEAD` then `bash ops/sandbox/bootstrap.sh` (awaited; idempotent;
     ≈ seconds when satisfied).
  6. If `data/.auth/compass_state.json` is absent (`readFileToBuffer` →
     null): `get('state/compass_state.json', {access: 'private', storeId:
     process.env.STATE_BLOB_STORE_ID})`; if found, `writeFiles` it
     (mode 0o600). Not found → proceed (cold login).
  7. `runCommand({cmd: 'venv/bin/python', args: ['ops/sandbox/run.py', job],
     env: buildRunnerEnv(process.env), detached: true, timeoutMs:
     3*60*60*1000})` → `202 {started: true, job, sessionId}`.
  8. Any exception → ntfy (if `NTFY_TOPIC`) + 500 with a message that names
     no secret.
  Log `sandbox.timeout` after resume (confirms the 2.3 assumption; if it is
  not ≈3 h, `extendTimeout` to reach it and record the finding).
- Tests: route logic factored so the Sandbox/Blob clients are injected
  (`createRunHandler({sandbox, blob, env, now})`); vitest covers 401, in-progress
  skip, seed-from-Blob only when disk lacks the file, allowlisted env passed
  to `runCommand`, `detached: true`, 202 shape. No network in tests.
- Acceptance: unit tests green; on a preview deployment with `CRON_SECRET`
  set, a manual `curl -H "Authorization: Bearer $CRON_SECRET"
  ".../api/pipeline/run?job=canary"` returns 202 and the Sandboxes
  dashboard shows `home-search-pipeline` running.

#### A5 — Reaper route (short-list, M)

- Files: new `app/api/pipeline/reap/route.ts`.
- Behaviour (GET, `maxDuration = 60`): 401 guard; `Sandbox.get({name,
  resume: false})` (404/absent → `200 {idle: true}`); read markers;
  `decideReap` →
  - `collect-and-stop`: `readFileToBuffer('data/.auth/compass_state.json')`
    → `put('state/compass_state.json', buf, {access:'private',
    addRandomSuffix:false, allowOverwrite:true, contentType:
    'application/json', storeId})`; then `stop()`; return
    `{stopped: true, exit_code, job}`.
  - `stop-hung`: `stop()`, ntfy `"home-search: <job> hung after 3h; stopped"`.
  - `alert-failed`: ntfy `"home-search: sandbox in failed state"`.
  - `noop`: `{idle: true}` or `{running: true, since}`.
  `stop()` and `update()` never auto-resume (verified), so the reaper cannot
  accidentally wake a stopped sandbox.
- Tests: injected clients as in A4; every `decideReap` outcome exercised;
  Blob `put` called with `access: 'private'` and the state pathname; `stop`
  called exactly once for the stop outcomes and never for `noop`.
- Acceptance: unit tests green; after A9's canary run, `vercel crons run
  /api/pipeline/reap` stops the sandbox and `vercel blob list --prefix state/`
  (with the state store's RW token, or the dashboard) shows a fresh
  `compass_state.json` timestamp.

#### A6 — Sandbox entry scripts (home-search, M)

- Files: new `ops/sandbox/bootstrap.sh`, `ops/sandbox/run.py`,
  `tests/test_sandbox_run.py`.
- `bootstrap.sh` (idempotent, `set -euo pipefail`): create `venv/` if absent;
  `venv/bin/pip install -q -r requirements.txt`; `sudo venv/bin/python -m
  playwright install-deps chromium` and `venv/bin/python -m playwright install
  chromium` (both no-ops when present); print versions. Must finish in ≤2 min
  cold, ≤15 s warm.
- `run.py <job>`: acquire `flock` on `data/.run/lock` non-blocking (exit 75
  and touch nothing if held — protects the markers from a duplicate launch);
  remove stale `data/.run/done`; write `data/.run/started`
  `{started_at, job, git_sha}`; run `pipeline.py --max-age=5h` (job
  `pipeline`) or `ops/canary.py` (job `canary`) as a subprocess with
  stdout/stderr tee'd to `data/logs/<job>-<ts>.log` **and** to its own stdout
  (so `cmd.logs()` works from the SDK); write `data/.run/done`
  `{exit_code, finished_at, job}`; exit with the child's code. Never reads
  or prints env values.
- Tests: with a fake subprocess runner and `tmp_path`: markers written in the
  right order and shape; lock contention → exit 75 and no marker changes;
  child failure → `done.exit_code` non-zero; both job names dispatch to the
  right script; unknown job → exit 2.
- Acceptance: tests green; `shellcheck ops/sandbox/bootstrap.sh` clean;
  bootstrap verified once by hand in a sandbox (`npx vercel@latest sandbox
  create --name bootstrap-check ...` per `ops/spikes/README.md`, then
  removed).

#### A7 — Canary script (home-search, S–M)

- Files: new `ops/canary.py`, `tests/test_canary.py`.
- Behaviour: `load_config(load_env())`; `launch_authenticated_page` with
  `data/.auth/compass_state.json`; record whether a login form was
  encountered (warm vs cold — instrument via a small hook or by checking
  `page.url` after `goto`); `fetch_collection_tabs`; PASS iff every configured
  tab returned > 0 listings and no `fetch.errors`; print one JSON line
  `{egress_ip, warm_session, counts, errors, pass}`; ntfy
  `"canary PASS/FAIL ..."` (A8); exit 0/1. **Read-only: no Turso connection,
  no photo downloads, no writes.**
- Tests: inject fake page/fetch results; PASS/FAIL rules; message shape.
- Acceptance: tests green; running locally (laptop) prints PASS with the
  real collection counts.

#### A8 — ntfy client (home-search, S)

- Files: new `src/notify.py`, `tests/test_notify.py`.
- `notify(topic, title, message, *, priority='default', tags=(), post=
  requests.post) -> bool`; POST `https://ntfy.sh/{topic}`; 5 s timeout;
  never raises; returns False and prints a warning on any failure; no-op
  returning False when `topic` is empty.
- Acceptance: tests cover headers (`Title`, `Priority`, `Tags`), timeout,
  swallowed exceptions, empty topic.

#### A10 — Manual session push/pull (home-search, S)

- Files: new `ops/state.py`, `tests/test_ops_state.py`; `.env.example`
  comment update for `BLOB_STATE_READ_WRITE_TOKEN` (now: laptop-only ops).
- `python ops/state.py push` uploads `data/.auth/compass_state.json` via
  `src.blob_state.put_state`; `pull` downloads it (refusing to overwrite a
  newer local file without `--force`). Uses `BLOB_STATE_READ_WRITE_TOKEN`
  from `load_env()`.
- Acceptance: tests with injected `put`/`get`; **first real exercise of
  `blob_state.py`**: Ben runs `push`, then confirms in the dashboard (or
  `vercel blob list --rw-token ...`) that `state/compass_state.json` exists
  in `home-search-state`, then `pull --force` round-trips it byte-identical.
  Record the outcome in the PR — `blob_state.py`'s private-host URL format
  has only ever been verified against `@vercel/blob`'s source.

#### A9 — Vercel setup and the first canary (Ben, manual, S)

Checklist, in order. Nothing here enters either repo.

1. short-list → Settings → Security: confirm "Secure backend access with
   OIDC federation" is on (Team issuer).
2. Blob store `home-search-state` → Projects → Connect to Project →
   short-list, Production + Preview; **Advanced Options → prefix `STATE_`**
   so the injected id is `STATE_BLOB_STORE_ID`. If the dialog offers no
   prefix, accept `BLOB_STORE_ID` and set `STATE_BLOB_STORE_ID` yourself to
   the same value with `vercel env add`; the routes read only the prefixed
   name.
3. `vercel env add` (Production, and Preview for testing): `CRON_SECRET`
   (`openssl rand -hex 32`), `PIPELINE_TURSO_AUTH_TOKEN` (the RW token from
   `~/code/home-search/.env`), `COMPASS_EMAIL`, `COMPASS_PASSWORD`,
   `COMPASS_COLLECTION_URL`, `ANTHROPIC_API_KEY`, `NTFY_TOPIC` (a long random
   topic name; subscribe to it on your phone). `TURSO_DATABASE_URL`,
   `BLOB_READ_WRITE_TOKEN`, `REVALIDATE_SECRET` already exist.
4. `python ops/state.py push` from the laptop (A10) so the first canary has
   a warm session.
5. Merge A1–A8 + A10; deploy short-list to production.
6. `vercel crons run "/api/pipeline/run?job=canary"`; watch `vercel logs`
   and the Sandboxes page; expect a ntfy "canary PASS"; then `vercel crons run
   /api/pipeline/reap` → sandbox stopped, snapshot listed, Blob session
   refreshed.
7. Spend Management: set an alert at $10 so a reaper bug shows up as a
   notification, not an invoice.

Acceptance: one green canary end to end; the daily cron then runs
unattended for **14 days**. Gate 1 passes when ≥13/14 are PASS with
`warm_session: true` and no run needed a cold login more than once in a
row. Record the daily results in `docs/journal/decisions.md` (B7 owns the
entry's skeleton).

### 4.3 Milestone B — make the pipeline itself cloud-ready

#### B0 — Content-keyed photos and blob deletion (home-search, M–L)

Rationale in §2.8. Ships as one PR of four commits so each is revertable.

- Files: `src/turso_db.py` (`TURSO_SCHEMA_EXTRA`: `hosted_photos` gains
  `source_url TEXT`), `src/photos.py`, `src/photo_upload.py`,
  `src/blob_upload.py`, `src/diff.py`, `scrape.py` (upload call passes URL
  map), `backfill_photos.py`, new `ops/migrate_photo_files.py`, new
  `ops/backfill_hosted_source_urls.py`, tests: `test_photos.py`,
  `test_photo_upload.py`, `test_blob_upload.py`, `test_diff.py`, new
  `test_photo_identity.py`.
- Commit 1 — identity: `photo_hash(url) -> str` (sha1 hex[:8]) and
  `photo_filename(position, url) -> "NN-<hash8>.jpg"` in `src/photos.py`;
  `download_photos` writes and skips by that name; `count_downloaded_photos`
  and every `glob("*.jpg")` (`score_photos.py`, `collect_pending_photos`)
  use a `??-????????.jpg` pattern so old-format files are invisible;
  `parse_photo_filename(name) -> (position, hash8) | None`.
- Commit 2 — hosted index: `collect_pending_photos(conn, photos_dir,
  photo_urls_by_listing, ...)` reads `SELECT listing_id, position, source_url,
  blob_url FROM hosted_photos` **once** and skips iff `(listing_id, position,
  source_url)` matches; `upload_photo` pathname becomes
  `photos/<id>/NN-<hash8>.jpg`; `_record` writes `source_url`; a superseded
  row's old `blob_url` is collected and passed to `delete_blobs` after the
  new upload's row is written (delete failures logged, never raised).
- Commit 3 — deletion: `delete_blobs(urls, rw_token, post=requests.post)`
  in `src/blob_upload.py` (endpoint/headers read from `@vercel/blob@2.8.0`
  dist, cite the chunk file in the docstring as the existing code does;
  chunk to ≤100 URLs per call); `apply_delisting` reads the doomed rows'
  `blob_url`s in one statement, calls `bulk_delete_listings`, then
  `delete_blobs` — ordering deliberate: the rows go first so a failed blob
  delete cannot leave a row pointing at a missing blob, and the URLs are
  printed on failure so they are never lost silently. Needs the photo RW
  token: `run_delisting` gains an optional `blob_token` argument; when it is
  absent (a `--skip-photos` dev run) the URLs are printed instead.
- Commit 4 — migrations: `ops/backfill_hosted_source_urls.py` (one `UPDATE
  hosted_photos SET source_url = (SELECT url FROM photo_urls p WHERE ...)
  WHERE source_url IS NULL` — one statement; prints the count still NULL,
  which are rows whose listing has no matching `photo_urls` slot and must be
  treated as stale → deleted with their blobs); `ops/migrate_photo_files.py`
  renames the laptop's `NN.jpg` to `NN-<hash8>.jpg` using
  `get_photo_urls_by_listing` (B1) — or, if B1 is not merged yet, a local
  copy of that one query; leftovers with no URL are deleted. `ops/
  rehost_photos.py --all` is optional and NOT part of this PR (note it in
  the journal as the paranoid alternative).
- Tests: a relisted listing whose position 3 now has a different URL is
  pending and its old blob URL is queued for deletion; unchanged URLs are
  not pending; a listing with zero URLs yields nothing; statement count for
  `collect_pending_photos` is exactly 1 regardless of listing count;
  `delete_blobs` sends the SDK's endpoint with the token and chunking, and a
  non-2xx raises `RuntimeError`; `apply_delisting` deletes rows first and
  prints URLs when the blob delete fails; old-format `NN.jpg` files are not
  counted by `count_downloaded_photos`; filename parsing round-trips.
- Acceptance: suite green; **do not run against production during the PR**.
  Ben runs, in order, on the laptop: `ops/backfill_hosted_source_urls.py`
  (expect ≈3,255 updated, ~0 still NULL), `ops/migrate_photo_files.py`
  (expect ≈3,253 renames), then `python scrape.py` — it must report zero
  photo downloads and zero uploads for the unchanged collection. The
  orphan-sweep ticket (out of scope) is filed referencing `delete_blobs`.

#### B1 — Set-based queries that replace the JSON store (home-search, M)

- Files: `src/db.py`, `tests/test_db.py` (or new `tests/test_store_queries.py`).
- Add: `hosted_photo_index(conn) -> dict[tuple[str, int], str]` (one
  statement over `hosted_photos`, `(listing_id, position) -> source_url`;
  B0's `collect_pending_photos` should call this same function so the read
  exists once); `listing_ids_with_any_hosted_or_no_urls(conn) ->
  frozenset[str]` (one statement; the weak pre-fetch gate for the
  explicit-URL loop only); `needs_photo_work(listing, index) -> bool` (pure:
  any `(id, i, url)` of the listing's `photo_urls` absent from the index);
  `get_listing_ids_missing_fields(conn, fields) -> list[str]` (one
  statement, `IS NULL` per field, fields validated against `_SCHEMA` column
  names to keep it injection-proof); `get_photo_urls_by_listing(conn) ->
  dict[str, list[str]]` (one statement, ordered by position, listings with
  none get `[]`, same LEFT JOIN idiom as `get_amenities_by_listing`);
  `listings_from_rows(rows, amenities_by_id, photo_urls_by_id) ->
  list[Listing]` (moved from `score.py._row_to_listing`, now filling
  `photo_urls`, `property_type`, `localized_status`).
- Tests: against in-memory sqlite with `ensure_schema` (so `hosted_photos`
  exists with `source_url`); **assert statement counts** with the
  counting-connection idiom from `tests/test_score_batching.py`; semantics:
  zero-URL listing needs no work; a listing with URLs but no hosted rows
  does; a listing whose hosted rows match every current URL does not; a
  listing where one position's URL changed does (the relist case).
- Acceptance: tests green; `score.py` uses `listings_from_rows` with its
  statement count unchanged (`test_score_batching` still pins it).

#### B2 — `scrape.py` gates from Turso; CSV/gallery from Turso (home-search, M–L)

- Files: `scrape.py`, `src/diff.py`, `check.py`, `backfill_photos.py`,
  `tests/test_diff.py`, `tests/test_scrape_gating.py` (new).
- `scrape.py`: `hosted = hosted_photo_index(db_conn)` computed once after
  `stage_connection()` and reused by the upload step; every
  `is_scraped(STORE_DIR, listing.listing_id)` on a fetched `Listing` →
  `needs_photo_work(listing, hosted)`; the explicit-URL loop's pre-fetch
  `precheck_id` skip → `listing_ids_with_any_hosted_or_no_urls`; after a
  successful `_save_listing` the listing's `(id, i, url)` triples are added
  to the in-memory index so the collection loop does not repeat work the
  pinned loop just did; `needs_field_backfill` sites → membership in
  `set(get_listing_ids_missing_fields(db_conn, BACKFILL_FIELDS))` computed
  once (only when `--backfill-missing`); the tail rebuilds `all_listings`
  from Turso via the B1 functions and keeps `write_csv`/`write_gallery`;
  `save_listing` call removed; fail fast if `BLOB_READ_WRITE_TOKEN` is unset
  (see 2.2 consequence) unless `--skip-photos`; `run_delisting` receives
  the token for B0's blob deletion.
- `src/diff.py`: `apply_delisting`/`run_delisting` drop `store_dir` and the
  `delete_stored_listing` call; update the two callers and `check.py`.
- `backfill_photos.py`: same set-based gate; drop `save_listing`.
- Extract the gating decisions into small pure functions
  (`should_process(listing_id, *, force, backfill, scraped, stale)`) so they
  are unit-testable without Playwright; test the `--force`, `--new-listing`,
  `--limit`, `--backfill-missing` interactions.
- Round-trip budget for a steady-state scrape (assert in a test using a
  counting connection around the non-network parts): schema (~14) + snapshot
  (1) + pins (1) + hosted index (1, shared with upload) + weak pre-fetch set
  (1, only when `LISTING_URLS` is set) + bulk upsert (≈6–12) + CSV/gallery
  (3) — no term proportional to listing count except the chunked bulk write.
- Acceptance: suite green; after B0's migrations have run on the laptop,
  `python scrape.py` prints "skip (already scraped)" for the whole unchanged
  collection, downloads and uploads **zero** photos, and rewrites
  `data/listings.csv`/`gallery.html` with the full listing count (was:
  whatever the JSON store held). This is the same check a first cloud run
  must pass: no photo traffic to Compass for listings already hosted.

#### B3 — Delete the JSON store (home-search, S)

- Files: delete `src/store.py`, `tests/test_store.py`, `backfill_db.py`,
  `backfill_hoa.py` (Ben's call on the last two — see 2.2); grep for
  `STORE_DIR`, `src.store`, `data/listings` and remove; update
  `ops/DECOMMISSION.md`'s "never safe to delete" table (the store row goes;
  the laptop's `data/listings/` is now just an old artifact; the
  `data/photos/` row's wording changes — it is a cache that B0's URL gate
  can rebuild, not the only copy of anything).
- Acceptance: `grep -rn "src.store\|STORE_DIR" --include=*.py .` returns
  nothing; suite green.

#### B4 — Vision checkpoint in Turso (home-search, M)

- Files: `src/turso_db.py` (`TURSO_SCHEMA_EXTRA` gains `vision_batches`),
  `src/db.py` (`load_vision_batches`, `record_vision_batch`,
  `clear_vision_batch`), `score_photos.py`, `tests/test_vision_batches.py`
  (new), `tests/test_rescore_all.py` (extend).
- Schema: `CREATE TABLE IF NOT EXISTS vision_batches (batch_id TEXT PRIMARY
  KEY, garage_expected_by_id TEXT NOT NULL, submitted_at TEXT NOT NULL,
  submitted_by TEXT NOT NULL);`. No `listing_id` column, deliberately (see
  2.4) — add a test that `tables_child_first(extra_tables=("hosted_photos",))`
  does **not** include it and that `delete_orphaned_rows` ignores it.
- `score_photos.py`: `_load_checkpoint` → one `SELECT`; `_append_checkpoint`
  → one `INSERT` immediately after `client.messages.batches.create`;
  `_clear_checkpoint` → one `DELETE ... WHERE batch_id = ?` **per consumed
  batch, at the moment its results are processed** (finer-grained than the
  old clear-all-at-the-end, so a crash between two batches cannot re-process
  the first); `submitted_by` = `os.environ.get("HOME_SEARCH_HOME",
  socket.gethostname())`. One-time migration: if
  `data/.photo_scoring_batch_state.json` exists, import its entries (skip
  ids already present) and rename it `.migrated`.
- Tests: statement counts (load = 1, record = 1, clear = 1 per batch);
  never-pay-twice scenario across two "homes" sharing one connection —
  home A records a batch, home B's candidate selection excludes those
  listings; resume after a simulated crash mid-results still resolves
  `garage_expected_by_id` from the stored row; legacy-file migration.
- Acceptance: tests green; **do not run `score_photos.py`** — the live
  verification is Milestone C's supervised run.

#### B5 — Run summary notification in `pipeline.py` (home-search, S)

- Files: `pipeline.py`, `tests/test_pipeline.py`.
- `run_pipeline(..., notify_fn=None)` late-bound like `revalidate_fn`; after
  a run (success or first failure) post `"home-search: ok in 412s (scrape
  91s, commutes 4s, score-photos 300s, score 17s)"` or `"home-search: FAILED
  at score-photos (exit 1) after 300s"`; skipped/dry runs post nothing.
  Default implementation reads `NTFY_TOPIC` via `load_env()` and no-ops when
  unset (so nothing changes for anyone without a topic).
- Tests: called once with a message containing the failed stage on failure;
  called once on success; not called on `Skipped`/dry-run; the autouse
  fixture that blocks real revalidate gains a twin that blocks real ntfy.
- Acceptance: tests green; a local `python pipeline.py --only=score` with
  `NTFY_TOPIC` set produces a phone notification.

#### B6 — Operator tool: supervised one-off sandbox run (home-search, M, optional)

- Files: new `ops/sandbox_run.py`, `ops/requirements-ops.txt`
  (`vercel>=0.10`), `ops/spikes/README.md` update.
- `python ops/sandbox_run.py --job pipeline|canary [--db-url URL --db-token
  TOKEN] [--skip-score-photos]`: creates a **non-persistent** sandbox named
  `home-search-manual-<ts>` from the same GitSource, runs `bootstrap.sh`,
  copies the laptop's `data/.auth/compass_state.json` in via `fs.write_bytes`,
  runs `ops/sandbox/run.py <job>` with env from the laptop's `.env` (with the
  Turso URL/token overridable for a throwaway DB), streams logs, destroys the
  sandbox on exit. This is the gate-4 harness made repeatable. Authenticates
  with `VERCEL_OIDC_TOKEN` from `vercel env pull` in a linked directory or
  `VERCEL_TOKEN`+ids, per the Sandbox auth docs.
- Tests: argument parsing and env assembly only (no SDK in tests).
- Acceptance: one manual run against a throwaway DB reproduces gate 4's
  EXIT=0 and prints the stage timings.

#### B7 — Docs and journal (home-search, S)

- Files: `docs/journal/decisions.md` (new entry: Phase 3 design decisions
  as in §2, the gate-4-actually-ran correction, the canary results table
  skeleton), `ops/DECOMMISSION.md` (section A: how to disable the cloud —
  remove the crons from `vercel.json` or Disable in the dashboard, `sandbox
  stop`/`remove`; section B updated), `ops/systemd/README.md` (pointer),
  `.env.example` (`NTFY_TOPIC`; `HOME_SEARCH_HOME` explanation;
  `BLOB_STATE_READ_WRITE_TOKEN` is laptop-only), `ops/spikes/README.md`.
- Acceptance: a reader can go from "the site is stale" to the right runbook
  section in one hop; no secret values anywhere.

### 4.4 Milestone C — go-live (only after gate 1 passes)

#### C1 — Enable the pipeline cron (short-list, S)

- `vercel.json`: add `{"path": "/api/pipeline/run", "schedule": "0 */6 * *
  *"}`; keep the reaper; drop the canary entry (the pipeline run is now the
  canary) or keep it daily if Ben wants the independent signal.
- Acceptance: `vercel crons ls` shows it; first scheduled run produces a
  ntfy "ok" and the viewer's data changes (check a listing's `computed_at`).

#### C2 — Supervised first cloud run, then retire the local timer (Ben, manual)

- Before the cron fires: `vercel crons run /api/pipeline/run`, follow with
  `sandbox connect home-search-pipeline` → `tail -f data/logs/*.log`. This is
  the first time `score_photos.py` runs in a sandbox; confirm one batch is
  recorded in `vision_batches` and later cleared, and that Anthropic's usage
  page shows exactly one submission per listing.
- Keep the laptop timer for 1–2 weeks of overlap (both homes share the DB;
  `vision_batches` makes overlap safe). Then follow `ops/DECOMMISSION.md`
  §A. Record in the journal.

#### C3 — short-list operator doc (short-list, S)

- `docs/PIPELINE.md`: the two routes, env vars (names only), how to trigger,
  how to read logs, how to disable. Link from `docs/DEPLOYMENT.md`.

## 5. Risks and rollback

| Risk | Likelihood / cost | Detection | Mitigation | Undo |
| --- | --- | --- | --- | --- |
| reCAPTCHA degrades under scheduled datacenter logins (gate 1) | Unknown; cost = Phase 3 is pointless | Canary FAIL with `warm_session: false` streaks | Warm-session-first; canary before any pipeline cron; `ops/state.py push` re-seeds from a laptop login | Do not enable C1; delete the sandbox; everything else built is harmless |
| A reaper bug leaves sandboxes running | Medium / ≈$0.25 per idle 3 h, capped by `timeout` | Sandboxes page; Spend Management alert at $10 | 3 h `timeout` + `timeoutMs`; reaper every 10 min | Dashboard → Stop Sandbox; disable the run cron |
| Double vision payment | Low after B4 / real money | Anthropic usage page vs listing count | Checkpoint in Turso written at submit; named sandbox + flock; launcher skip | Nothing to undo; `vision_batches` rows are the audit trail |
| Persistent snapshot lost or expired (30 idle days) | Low / ≈2 min extra on next run + cold login | Launcher logs `created: true` | `getOrCreate` recreates; bootstrap idempotent; session seeded from Blob | none needed |
| `proxy.ts` matcher wrong → cron gets 307 | Medium if untested / silent total failure | A1 test + curl on preview; `vercel logs` show 307 | Literal matcher, test, manual curl in A9 | Revert one line |
| Secret leaks into a public repo | Low / severe | `git diff` review; both repos' `.gitignore` | Allowlist in `buildRunnerEnv`; secrets only via `vercel env add`; tests never contain real values; error messages name keys not values | Rotate: `vercel env rm/add`, Turso token rotate, Compass password |
| RW Turso token and Anthropic key now live in the viewer project's env | Accepted / same trust boundary (Ben's account) | — | Names only in code; viewer code never reads them | Remove from env; run locally |
| `git reset --hard origin/main` picks up a broken push to `main` | Medium / one failed run | ntfy FAILED + `data/.run/done.exit_code` | Set `PIPELINE_GIT_REVISION` to a tag/sha to pin; fix main | Set the env var; rerun |
| Two homes run at once (laptop + sandbox) | Medium during C2 overlap / wasted work only | Both post ntfy | Idempotent upserts; shared checkpoint; stagger the systemd calendar vs cron | none needed |
| Turso stream expires during a long idle stage | Known / one failed stage | `stream not found` in logs | Existing one-retry wrapper; `score_photos` polls every 60 s so streams rarely idle long | Rerun; consider a keep-alive `SELECT 1` in the poll loop if it bites |
| Sandbox disk fills (64 GB) | Very low | `df` via `sandbox connect` | Photos ≈1 GB; logs rotate by run | `rm -rf data/photos` in the sandbox |
| Playwright/Chromium version drift on the universal image | Low / bootstrap failure | Launcher 500 + ntfy | Floors in `requirements.txt`; `playwright install` is idempotent | Pin a floor higher; recreate sandbox |
| JSON-store deletion regresses local incremental scraping | Low / one full photo pass | `scrape.py` output | B2 acceptance test against the live collection first | `git revert` B2/B3; `data/listings/` was never deleted |
| Stale photos served after a relist or photo change (positional key) | Was certain over time / silent wrong images | Only by eye today; after B0, the URL diff | B0 content-keyed identity; `source_url` backfill | none — B0 is the fix; `ops/rehost_photos.py --all` if the backfill assumption proves wrong for specific listings |
| `source_url` backfill masks an already-stale row | Low, bounded to changes before 2026-09-03 without a delist | Visual spot-check of a few listings' galleries after B0 | Optional `--all` rehost overnight from the laptop | rehost the affected listing |
| Blob store grows with every delist / photo change (no deletion) | Certain without B0 / cents per month on Pro, but unbounded | Store size in the dashboard | B0 deletes superseded and delisted blobs; orphan sweep is its own ticket | none needed; `data/archive/orphaned-hosted-photos-20260903.json` holds the historical orphan URLs for the sweep |
| First laptop run after B0 re-downloads everything (filename scheme change) | Certain unless migrated / ≈25 min of Compass CDN traffic | `scrape.py` prints downloads | `ops/migrate_photo_files.py` renames in place | delete `data/photos/` and let one run rebuild it |

**Full rollback of Phase 3** = remove the `crons` from `vercel.json` (or
Disable Cron Jobs in the dashboard) and `sandbox remove home-search-pipeline`.
The laptop timer never stopped being able to run the same pipeline against
the same database. B1–B5 are independently correct on the laptop and need no
rollback.

## 6. What must NOT be done

- **Do not run `score_photos.py`** from any agent, against any database, at
  any point in Milestones A or B. It submits paid vision batches. B4's tests
  use fakes; the only live run is Ben's supervised C2.
- **Do not commit any secret** to either repo. Both are public. That includes
  `NTFY_TOPIC`, sandbox names combined with tokens, `.env.local` pulled by
  `vercel env pull`, and test fixtures containing real tokens. Secrets enter
  Vercel only through `vercel env add`. Review every diff for
  `vercel_blob_rw_`, `sk-ant-`, `eyJ`.
- **Do not add a route without adding it to `proxy.ts`'s exclusion.** A
  redirected cron is a silent no-op: crons do not follow redirects and a 307
  is logged as success.
- **Do not put a Vercel access token or an OIDC token into the sandbox env**,
  and do not pass `oidcToken` explicitly to `@vercel/blob`. Both fail after
  the token expires mid-run; the second is warned against in the SDK docs.
- **Do not lose or fork the vision checkpoint.** It lives in Turso
  (`vision_batches`) and nowhere else after B4. No per-home copies, no
  "clear all at the end", no `--force` path that ignores it.
- **No per-row Turso round-trips and no check-then-act loops.** Every new
  query in B0/B1/B4 is one statement; tests assert the count. Remember
  `turso_serverless.executemany` loops.
- **Do not decide "already have this photo" on `(listing_id, position)`
  alone** — on disk, in Blob, or in Turso. The identity is the source URL
  (§2.8). Any new gate must compare URLs.
- **Do not delete a `hosted_photos` row without deleting or exporting its
  blob.** `blob_url` is the only handle on what exists in the store.
- **Do not overwrite a photo at its old Blob pathname** to "fix" a stale
  image — the CDN cache keeps the old bytes for up to a month. New content
  gets a new hashed pathname.
- **Do not `await` the pipeline in the launcher**, do not raise
  `maxDuration` to "make it fit", and do not use Workflow to make it fit —
  the sandbox is the long-running primitive; the function returns in seconds.
- **Do not use `persistent: false` for the pipeline sandbox** — the shared
  VM is what makes the `flock` guard work across duplicate cron deliveries.
- **Do not make the sandbox anonymous** (`getOrCreate` without a name) for
  the same reason.
- **Do not change `src/auth.py`, `src/turso_db.py`'s stream wrapper, or the
  batched write path** as part of this phase; they are load-bearing and out
  of scope. If B2/B4 need a new query, add a function beside the others.
- **Do not touch `/home/bengi/code/home-search`** (Ben's main checkout) —
  work in worktrees cut from `origin/main`.
- **Do not build a `vercel.json`-level default `maxDuration` above 800 s**
  or rely on the 1800 s beta; nothing here needs it.
