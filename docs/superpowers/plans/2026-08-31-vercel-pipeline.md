# Vercel Pipeline — Empirical Spike Result and Migration Plan

Date: 2026-08-31
Status: spike executed for real on 2026-08-30 (not simulated); plan conditioned on its results.
Prior analysis: `docs/superpowers/plans/2026-08-30-pipeline-automation.md`.

## 1. Spike result: Compass works from a Vercel Sandbox — VERIFIED

The single blocker the prior plan could not resolve from documentation was
whether Compass tolerates Playwright from a datacenter IP. It was tested
empirically on 2026-08-30 and **it works — both warm-session and cold-login.**

### Method

- Vercel Sandbox, `vercel/sandbox/universal` image (Ubuntu 26.04, Python
  3.14.4, passwordless sudo), 2 vCPU / 4 GB, region iad1, egress IP
  `3.88.128.217` (AWS — a genuine datacenter IP).
- Copied only `src/{auth,config,scraper,json_extract,listing_parser,models,structured_fields,hoa}.py`
  plus a driver; `pip install playwright beautifulsoup4` +
  `python3 -m playwright install --with-deps chromium` (apt works on the
  Ubuntu image; whole install ≈ 2–3 min, 115 MB Chromium download).
- Secrets passed only as process env vars via the SDK's `run_process(env=...)`;
  nothing written to disk outside the sandbox, sandbox destroyed afterwards
  (verified 404 on lookup).

### Warm session (local `compass_state.json` copied in) — WORKS

```
goto https://www.compass.com/login/ -> 200, redirected to /overview/  (already authenticated)
fetch_collection_listings -> TOTAL_LISTINGS: 145
5/5 parsed cleanly (listing_id | address | price all correct)
```

Compass honored the residential-born session from an AWS IP and served the
authenticated collection API without any challenge.

### Cold login (no storage state) — ALSO WORKS, and exposed a latent bug

First attempt "failed" with `ensure_logged_in`'s own error (password field
still present after Sign In). Network-level retry showed why:

```
POST https://www.compass.com/login/          -> 200
GET  https://www.compass.com/login/          -> 302
t+10s url=https://www.compass.com/overview/  password_fields=0   LOGIN SUCCEEDED
```

The login page loads **reCAPTCHA Enterprise (invisible/score-based)** and an
**AWS WAF challenge** (`awswaf.com/.../challenge.js`, `mp_verify` -> 200).
Both passed from the datacenter IP with zero interaction. The real failure was
a race in `src/auth.py::ensure_logged_in`: it checks for the password field
immediately after `networkidle`, which resolves while WAF telemetry is still
settling — a few seconds before the 302 lands. **This race can flake locally
too** and needs fixing regardless of hosting (Phase 0 below).

### What the spike proves — and what it does not

Proven: datacenter IP is not blocked; warm-session API access works; cold
scripted login works; the exact install path for Chromium in the universal
image works; no interactive captcha appeared.

NOT proven (stated honestly):

- **Durability.** reCAPTCHA Enterprise is score-based. One pass from one AWS
  IP is not a guarantee under weeks of scheduled repetition. Mitigation:
  warm-session-first design so cold logins are rare, plus a loud failure
  signal and a documented manual re-auth path (run scrape once locally; the
  refreshed state uploads on the next cycle).
- **Photo CDN downloads** from a datacenter IP (the spike hit the collection
  API, not the image CDN). Likely fine — photos route through the
  authenticated page context — but untested.
- **Nominatim/OSRM** from a datacenter IP (stage 2). Untested, and
  Nominatim's usage policy is friendlier to identifiable low-volume clients
  than to anonymous cloud IPs. Small risk, cheap to test the same way.

Spike cost: ≈ **$0.03–0.05** (~20 min sandbox at 2 vCPU/4 GB). Sandbox
destroyed at the end.

## 2. The decision: two viable shapes, judged on merits

The spike unblocks **Option A** (everything in a scheduled Sandbox). A fourth
option was also put on the table with measured facts from the short-list
build: **laptop keeps the browser, cloud keeps the data** — scraping stays
local/residential, but every stage writes DIRECTLY to hosted Turso as the
single source of truth, and `publish.py`'s mirror is deleted. This is not the
rejected "B2 hybrid" (which kept local SQLite *and* added a handoff); it
eliminates the second database entirely.

| | Option A (full Sandbox) | Option Four (local scrape → direct Turso) |
| --- | --- | --- |
| Runs with laptop off | **Yes** (the headline feature) | No (systemd timer, laptop must be on) |
| Compass risk | New: scheduled datacenter sessions, score drift unknown | **Zero delta** — same residential IP + session as today |
| Databases | One (Turso) after this plan | **One (Turso)** |
| FK divergence (bit twice; PR #9) | Fixed — one DB | Fixed — one DB |
| `publish.py` | Deleted | Deleted |
| New state plumbing | compass_state + batch checkpoint to Blob, is_scraped rework, snapshot mgmt, self-stop | **None** — disk, session, JSON store, checkpoint all stay local |
| New failure modes | cron, function, sandbox, snapshot expiry, token lifetimes | Network-dependent pipeline runs (retryable; writes stay idempotent) |
| $/mo | ~$2–10 inside Pro's $20 credit | ~$0 (Turso/Blob usage unchanged) |
| Est. effort | 3–5 days *after* the shared DB work | 2–3 days, largely deletion-shaped |

**Recommendation: do the work in three phases, cutting over to Turso-as-
source-of-truth first (Option Four), and only then — optionally — lifting
execution into the Sandbox (Option A).** This is not a consolation
sequencing; it is strictly better engineering:

- Phase 2 (Option Four) delivers the single strongest technical win — one
  database, FK enforcement everywhere, `publish.py` deleted — with zero
  Compass exposure and zero new infrastructure.
- Phase 2 **eliminates Option A's hardest prerequisite.** The prior plan's
  `src/turso_restore.py` (hydrate a fresh local DB each run) exists only
  because the stages are local-SQLite-centric. Once the stages speak Turso
  natively there is nothing to restore: an ephemeral sandbox connects to the
  same DB the laptop does. Option A shrinks from a state re-architecture to
  "run the same pipeline somewhere else + persist two small files."
- If Compass's tolerance ever degrades, Phase 3 is simply never built, and
  nothing is stranded.

**Effect on short-list: none — verified, not assumed.** `lib/db.ts` reads
`TURSO_DATABASE_URL` via `@libsql/client`; `lib/queries.ts` wraps reads in
`'use cache'` + `cacheTag('listings')`; `app/api/revalidate/route.ts` exists
and `publish.py` already POSTs it with `REVALIDATE_SECRET`. Any process that
writes the same Turso DB and ends with that same POST keeps the viewer
working untouched. The revalidate call is the one piece of `publish.py` that
survives (as ~10 lines at the end of the pipeline).

## 3. Phase 0 — groundwork worth doing under every option (one commit each)

1. **Fix the login race in `src/auth.py`** (spike finding). After clicking
   Sign In, poll for up to ~30 s for either the URL leaving `/login/` or the
   password field disappearing, instead of one post-`networkidle` check.
   Test: fake page object that "succeeds" 10 s after click.
2. **Unify env handling.** `scrape.py` and `score_photos.py` read
   `dotenv_values(".env")` only; adopt `{**dotenv_values(".env"), **os.environ}`
   (the merge `publish.py` already uses) so process env works everywhere.
   Required for the sandbox; harmless locally. Tests: config loads from
   process env when `.env` absent.
3. **`src/blob_upload.py`: replace the `vercel` CLI subprocess with a direct
   Blob REST PUT** (`PUT https://blob.vercel-storage.com/<pathname>` with
   `Authorization: Bearer <BLOB_READ_WRITE_TOKEN>`, `x-api-version`,
   single-part). Removes ~0.6 s Node startup per photo, the undeclared CLI
   dependency, and the multipart-billing foot-gun (the CLI's
   `--multipart true` default once turned 3,000 photos into 11,000 Advanced
   Operations and a 30-day store suspension). One REST PUT = one operation.
   Keep the "empty URL is an error" guarantee. Tests: injected HTTP callable.
4. **Raise `MAX_PHOTOS_PER_LISTING` default from 8 to 0 (uncapped)** — it is
   a Hobby-era cap; Pro's credit absorbs it. Sizing reality: the full corpus
   is **991 MB / 4,560 JPGs**; a one-time backfill ≈ 4,560 ops ≈ $0.02 of
   on-demand Advanced Operations plus ~$0.023/mo storage. Trivial on Pro —
   but only via the REST path from step 3.

## 4. Phase 2 — Turso becomes the single source of truth (Option Four)

Measured constraints that shape every step (from the short-list build):
each Turso statement is an HTTP round-trip ≈ **240 ms**; a row-at-a-time full
sync took ~22 min until multi-row INSERTs (chunk 30, `BATCH_CHUNK` in
`src/turso_sync.py`) collapsed ~5,400 round-trips to ~110; a per-photo
`already_uploaded()` check burned 12 min before being replaced by one
whole-set query. **Batching is correctness here, not optimization.** Every
step below is audited against "no per-row round-trips, no check-then-act
loops."

1. **`src/turso_db.py`: connection + row-compat layer.** Wraps
   `turso_serverless.connect()` and normalizes row access (index and by-name)
   to what `src/db.py` callers expect from `sqlite3.Row`. `src/turso_sync.py`'s
   `upsert_rows`/`replace_listing_rows`/`ensure_schema` move here (they are
   no longer "sync" — they are *the* write path). Tests keep running against
   in-memory sqlite3 (Turso is SQLite-compatible; existing test idiom).
2. **Batched write API.** `bulk_upsert_listings(conn, listings)` writes
   listings parent-first (Turso enforces FKs — local SQLite never did until
   PR #9; with one DB this divergence class disappears, which is the
   strongest argument for this whole phase), then amenities/photo_urls via
   `replace_listing_rows`, chunked. Steady state: ~129 listings ≈ a dozen
   round-trips, seconds not minutes.
3. **`scrape.py` restructure**: collect the collection results, then one
   bulk write — instead of `upsert_listing` per listing inside the loop
   (which against Turso would be ~645 round-trips ≈ 2.6 min every run).
   Price-snapshot read is already one query. `is_scraped()` and the JSON
   store are untouched — the laptop's disk still exists in this option.
   Delisting: `run_delisting` must prune child-first; `tables_child_first()`
   already provides the order (audit `src/diff.py` uses it against the
   Turso connection).
4. **Photo upload moves into the pipeline** (was `publish.py`'s job): after
   downloading a new listing's photos, upload via the Phase-0 REST client.
   Skip set = **one** `SELECT listing_id, position FROM hosted_photos` (the
   pattern `_collect_pending_photos` already gets right — keep it).
5. **`compute_commutes.py` / `score_photos.py`**: point at the Turso
   connection. Their write volume is a handful of rows per run — row-at-a-
   time is fine there; their "what's missing" reads are already single
   queries. `score.py`: switch its full recompute to one batched write.
6. **Cutover commit**: run `publish.py` one final time (mirror fully fresh),
   archive `data/listings.db` (cheap insurance for the ~$10–20 of embedded
   vision scoring; note `visual_scores.raw_response` is already in Turso so
   the paid work survives regardless), flip the stages' connection factory
   to Turso, end the pipeline with the revalidate POST.
7. **Delete `publish.py` in its own commit.** Not retained as a fallback: a
   mirror you no longer run is drift you no longer notice, and its three
   hard-won behaviors all live on — batching in `turso_db`, prune ordering
   in the delisting path, revalidate at pipeline end. Rollback story: revert
   this commit plus the connection-factory commit and the archived
   `listings.db` resumes as source of truth; nothing in Turso's schema
   changed shape, so the old flow works immediately.

Failure semantics: a mid-run network failure leaves Turso partially updated
— acceptable because every write is an idempotent upsert of internally-valid
rows, the viewer's cache hides mid-run states until revalidate, and the next
run converges. The pipeline exits non-zero on partial failure (keep
`publish.py`'s discipline).

Verification: full suite green; one supervised end-to-end run writing to
Turso; short-list spot-checked against known listings; `systemd` timer from
the Option C plan keeps the schedule.

## 5. Phase 3 (optional) — lift execution into the Sandbox (Option A)

Build only if laptop-off scheduling is actually wanted once Phase 2 lands.
After Phase 2 this is small:

1. **Trigger**: `vercel.json` cron (start 6-hourly) in the **short-list**
   project → `/api/run-pipeline`, guarded by `CRON_SECRET`. short-list is
   already deployed, already holds `TURSO_*` and `REVALIDATE_SECRET`; add
   `COMPASS_EMAIL`, `COMPASS_PASSWORD`, `COMPASS_COLLECTION_URL`,
   `ANTHROPIC_API_KEY`, `BLOB_READ_WRITE_TOKEN` via `vercel env add`
   (dashboard-held, encrypted). **Both repos are public** — secrets never
   enter either repo; `.env` stays gitignored locally (verified never
   committed) and everything cloud-side lives only in project env vars.
2. **Sandbox creation** from the function (OIDC is automatic on Vercel):
   `Sandbox.create({ source: { type: 'snapshot', ... } , resources, env: <secrets from process.env> })`,
   pipeline started detached, function returns well under 800 s.
   Code arrives via **`GitSource` pointing at the public home-search repo**
   — no tarball packaging step at all.
3. **Snapshot strategy** (each run must not reinstall Chromium): one admin
   script creates a snapshot after `pip install` + `playwright install
   --with-deps chromium` (the exact sequence the spike proved, ~2–3 min).
   Snapshots expire 30 days after last use — a 6-hourly cron perpetually
   refreshes; the runner falls back to cold-install if the snapshot 404s.
   Upgrade path if churn bites: bake a custom image into Vercel Container
   Registry (no expiry).
4. **Small state to Blob**: `data/.auth/compass_state.json` and
   `data/.photo_scoring_batch_state.json` — download at start, upload at
   end, via the Phase-0 REST client. Losing the checkpoint would break the
   never-pay-twice guarantee; losing the session forces a cold login (works,
   per spike, but should stay rare). No DB restore step exists anymore —
   Phase 2 removed it.
5. **Scrape gating without the local JSON store**: `is_scraped()` checks
   file existence and would see everything as new. Replace with one query
   for the scraped-ID set from Turso (never per-listing checks — the 240 ms
   lesson). The photo dir becomes a per-run cache: only new listings'
   photos are downloaded, scored, uploaded.
6. **Self-stop**: the function forwards its own short-lived OIDC token into
   the sandbox env; the runner's last act stops its own sandbox via the SDK.
   Backstop: `execution_time_limit` (e.g. 3 h to cover a vision batch) so a
   crashed runner can't idle-bill for a day.
7. **Failure visibility**: runner posts outcome to ntfy.sh (or similar) —
   the silent-overnight-failure risk the prior plan flagged.

Cost (unchanged from verified 2026-08-30 pricing, now with measured install
times): ~$3.50/mo at 6-hourly, ~$10/mo hourly, inside Pro's $20 credit.
Rollback: disable the cron; the Phase-2 local pipeline + systemd timer is
still fully functional — the two execution homes share one database.

Pre-build spikes still owed before Phase 3 (each ~30 min, pennies): photo
CDN fetch from sandbox; Nominatim/OSRM from sandbox; one full supervised
sandbox pipeline run against a throwaway Turso DB.

## 6. Open questions / risks

- **reCAPTCHA score durability under scheduled datacenter logins** — the one
  thing a single successful spike cannot prove. Warm-session reuse makes
  logins rare; alert on `LOGIN_FAILED`; manual recovery is one local run.
- Nominatim policy / OSRM availability from cloud IPs (Phase 3 only).
- `turso_serverless` row-shape quirks surface in Phase 2 step 1 — contained
  by the compat layer and its tests.
- Turso free-tier row-read/write quotas: the viewer plus a 6-hourly pipeline
  is well inside them today, but re-check when photos/backfills run.
- Concurrent writers (laptop run overlapping a sandbox run in Phase 3):
  upserts make this safe but wasteful; keep the flock locally and stagger
  the cron, or gate on a `pipeline_runs` row in Turso if it ever matters.
