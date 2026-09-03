# Phase 3 reference — how the cloud pipeline works and how to run it

Date: 2026-09-03
Status: REFERENCE for the system described in
`2026-09-03-phase3-sandbox-execution.md`. Written ahead of implementation so
the executing agents and the operator share one mental model; update it as
tasks land. Assumes you know the repo (`pipeline.py`, the four stages, Turso
as the source of truth) but not the plan.

## 1. The one-paragraph version

Every six hours a Vercel cron in the **short-list** project calls
`/api/pipeline/run`. That function resumes a single named, persistent Vercel
Sandbox called `home-search-pipeline`, pulls `main` of the public
`home-search` repo into it, makes sure a venv and Chromium exist, seeds the
Compass session file if the sandbox has lost it, and starts
`ops/sandbox/run.py pipeline` **detached**. The function returns in seconds.
Inside the sandbox, `pipeline.py` runs the four stages against the same Turso
database the laptop uses, POSTs the viewer's revalidate hook, posts a summary
to ntfy, and writes a `done` marker. Every ten minutes `/api/pipeline/reap`
looks at the markers: when the run is done it copies the refreshed session
out to a private Blob store and **stops** the sandbox (which snapshots its
disk); if a run has been going for more than three hours it stops it and
alerts. The sandbox never holds a Vercel or private-store credential; the
functions do all Vercel-side I/O with their own auto-refreshing OIDC token.
The laptop's systemd timer keeps working unchanged against the same database
and is the rollback.

## 2. Components and responsibilities

### 2.1 short-list (Next.js 16, Vercel, production)

| Piece | Responsibility | Must never |
| --- | --- | --- |
| `vercel.json` `crons` | Schedule: `/api/pipeline/run` (`0 */6 * * *`), `/api/pipeline/reap` (`*/10 * * * *`), optionally `/api/pipeline/run?job=canary` daily | run on preview (Vercel only fires crons on production) |
| `proxy.ts` | Redirect unauthenticated humans to `/enter`; **excludes** `api/revalidate` and `api/pipeline/*` | gain a route without gaining an exclusion — crons don't follow redirects, so a 307 is a silent no-op |
| `app/api/pipeline/run/route.ts` (launcher) | Auth (`CRON_SECRET`), `Sandbox.getOrCreate`, skip if a run is active, `git fetch/reset`, `bootstrap.sh`, seed session from Blob, `runCommand(... detached)`; `maxDuration = 300` | await the pipeline; log env values; pass any Vercel/OIDC token into the sandbox |
| `app/api/pipeline/reap/route.ts` (reaper) | Auth, `Sandbox.get({resume:false})`, read markers, upload session to Blob, `stop()`, alert on hung/failed | resume a stopped sandbox (`stop`/`update` never auto-resume; everything else does — only call `readFileToBuffer` when `status === 'running'`) |
| `lib/pipeline/*.ts` | Pure helpers: env allowlist, auth check, marker parsing, reap decision | do I/O |
| Project env (`vercel env add`) | Holds every secret the sandbox needs plus `CRON_SECRET`, `PIPELINE_TURSO_AUTH_TOKEN` (RW; the viewer's `TURSO_AUTH_TOKEN` is RO), `STATE_BLOB_STORE_ID`, `NTFY_TOPIC`, optional `PIPELINE_GIT_REVISION` | be checked into either repo |
| Viewer pages, `lib/db.ts`, `lib/queries.ts`, `/api/revalidate` | Unchanged | — |

### 2.2 The sandbox (`home-search-pipeline`, iad1, 2 vCPU / 4 GB, persistent)

| Path | What | Lifetime |
| --- | --- | --- |
| `/vercel/sandbox` | clone of `BenGiese22/home-search`, reset to `origin/main` (or `PIPELINE_GIT_REVISION`) on every launch | snapshot |
| `venv/` | Python deps from `requirements.txt` (floors, not pins — the image is Python 3.14) | snapshot; rebuilt by `bootstrap.sh` if missing |
| Chromium (Playwright) | `playwright install --with-deps chromium`, ≈25 s cold | snapshot |
| `data/photos/<id>/NN-<hash8>.jpg` | photo cache; only photos not yet in `hosted_photos` are ever downloaded | snapshot (harmless if lost) |
| `data/.auth/compass_state.json` | Compass session; re-saved by `src/auth.py` on every run | snapshot + Blob copy |
| `data/.run/started`, `data/.run/done`, `data/.run/lock` | run markers written by `ops/sandbox/run.py`; `lock` is the `flock` file | snapshot (stale `done` is removed at the next start) |
| `data/.pipeline.lock`, `data/.pipeline-last-success.json` | `pipeline.py`'s own lock and freshness marker; `--max-age=5h` works here exactly as on the laptop | snapshot |
| `data/logs/<job>-<ts>.log` | per-run stage logs (also streamed to the command's stdout) | snapshot |

Session `timeout` 3 h (hard platform stop, snapshot taken on the way out);
`keepLastSnapshots: {count: 1}`; snapshots expire 30 days after last use,
after which `getOrCreate` transparently recreates the sandbox and
`onCreate` re-runs the bootstrap.

### 2.3 home-search (the runner and its tooling)

| Piece | Responsibility |
| --- | --- |
| `pipeline.py` | Unchanged orchestration: scrape → commutes → score-photos → score → revalidate; now also posts the run summary to ntfy (`NTFY_TOPIC`) |
| `ops/sandbox/bootstrap.sh` | Idempotent environment setup (venv, pip, Chromium) |
| `ops/sandbox/run.py <pipeline\|canary>` | `flock` on `data/.run/lock`; write `started`; run the job with output tee'd to `data/logs/`; write `done {exit_code}`; exit with the child's code. Exit 75 without touching markers when another run holds the lock |
| `ops/canary.py` | Warm-session collection fetch only; prints `{egress_ip, warm_session, counts, pass}`; ntfy PASS/FAIL; **no Turso, no downloads** |
| `ops/state.py push\|pull` | Laptop-side copy of `compass_state.json` to/from the private Blob store using `BLOB_STATE_READ_WRITE_TOKEN` (the only place that token is used) |
| `ops/sandbox_run.py` (optional) | Python-SDK harness for a supervised one-off run in a **non-persistent** sandbox, optionally against a throwaway DB — the gate-4 procedure made repeatable |
| `src/notify.py` | ntfy client; never raises |
| `src/blob_upload.py` | photo PUT (unchanged) and now `delete_blobs` |
| `src/photo_upload.py` | pending set = fetched `(listing_id, position, source_url)` minus `hosted_photos`, one query; superseded blobs deleted after the replacement upload |
| `src/db.py` additions | `hosted_photo_index`, `needs_photo_work`, `get_listing_ids_missing_fields`, `get_photo_urls_by_listing`, `listings_from_rows`, `load/record/clear_vision_batch` |
| `src/turso_db.py` `TURSO_SCHEMA_EXTRA` | `hosted_photos(+source_url)`, `vision_batches` |
| gone | `src/store.py` and `data/listings/*.json` as a gate; `.photo_scoring_batch_state.json` (replaced by `vision_batches`) |

### 2.4 External services

| Service | Auth from the sandbox | Auth from the functions |
| --- | --- | --- |
| Turso (SSOT) | `TURSO_AUTH_TOKEN` = RW token (injected per run from `PIPELINE_TURSO_AUTH_TOKEN`) | not used |
| Blob public `short-list-photos` | `BLOB_READ_WRITE_TOKEN` (photo uploads and deletes) | not used |
| Blob private `home-search-state` | **none** | OIDC (`VERCEL_OIDC_TOKEN` + `STATE_BLOB_STORE_ID`, SDK-managed) |
| Vercel Sandbox API | **none** | OIDC (SDK-managed, project-scoped) |
| Compass | session file + `COMPASS_EMAIL/PASSWORD` for cold login | — |
| Anthropic Batches | `ANTHROPIC_API_KEY` | — |
| Nominatim / OSRM / Compass photo CDN | unauthenticated | — |
| short-list `/api/revalidate` | `REVALIDATE_SECRET`, `SHORT_LIST_URL` | — |
| ntfy.sh | `NTFY_TOPIC` | `NTFY_TOPIC` |

## 3. Data flow

### 3.1 A normal run, as a timeline

```
t+0:00   cron GET /api/pipeline/run   -> 202 {started:true}          (function: ~5–20 s)
t+0:00   run.py: flock, started marker, pipeline.py --max-age=5h
t+0:01   scrape.py: warm session -> collection fetch (favorites, matches)
         bulk_upsert_listings (≈6–12 round-trips), hosted_photo_index (1)
         download only photos whose (id, pos, url) is not hosted; upload them;
         record hosted_photos rows with source_url; delete superseded blobs;
         delist: rows first, then their blobs; CSV + gallery from Turso
t+0:05   compute_commutes.py: new listings only (Nominatim 1 req/s)
t+0:06   score_photos.py: load vision_batches (1); submit batches for listings
         lacking visual_scores and not already in vision_batches; INSERT one
         row per batch immediately; poll every 60 s; on ended: upsert
         visual_scores, DELETE the batch row
t+0:06–?  ... minutes to hours of polling (I/O wait: CPU not billed, memory is)
t+X      score.py: three set reads, one batched write, ranked_report.csv
t+X      POST /api/revalidate; ntfy "home-search: ok in …"; done marker
t+X+≤10  cron GET /api/pipeline/reap -> reads done -> uploads session to Blob
         -> stop() -> snapshot -> 200 {stopped:true, exit_code:0}
```

Every write is an idempotent upsert; a run that dies at any line converges
on the next run. The one thing that must not be redone is a paid vision
submission, and `vision_batches` is written the instant a batch is created.

### 3.2 Where the two small state files go, and why

- **Compass session** — sensitive (a bearer credential for Compass). Lives on
  the sandbox disk between runs and in the *private* Blob store as the
  durable copy. Moved only by code holding OIDC (the functions) or by Ben
  from the laptop (`ops/state.py`). Losing it costs one cold login, which
  works from iad1 (spikes 2026-08-30 and 2026-09-03) but is what the canary
  is watching.
- **Vision checkpoint** — not sensitive, but losing or forking it costs real
  money. Lives in Turso, the one place both execution homes already look, so
  a laptop-submitted batch is visible to a cloud run and vice versa.

### 3.3 The photo identity rule

A photo is identified by `(listing_id, position, source_url)`. The Compass
CDN URL embeds a content hash, so a re-shot or re-ordered photo — or a
relisting under the same `listing_id` — produces a new URL and therefore a
new download, a new Blob pathname (`photos/<id>/NN-<hash8>.jpg`), a new
`hosted_photos` row, and deletion of the old blob. `(listing_id, position)`
alone was the key until 2026-09-03 and served stale images silently (6085
West 82nd Drive, 44 stale rows). The on-disk cache uses the same hashed
filename so the same rule holds locally.

## 4. Failure modes

| Symptom | Likely cause | Where to look | What happens automatically | Manual fix |
| --- | --- | --- | --- | --- |
| No ntfy message at the usual hour | cron did not fire, or launcher 401/307/500 | Vercel → short-list → Logs, filter `requestPath:/api/pipeline/run`; Cron Jobs settings page | nothing (crons are not retried) | `vercel crons run /api/pipeline/run`; check `CRON_SECRET` and `proxy.ts` |
| Launcher returns `{skipped:'in-progress'}` every time | a previous run never wrote `done` (crashed runner) and the reaper has not aged it out yet | reaper logs; `sandbox connect home-search-pipeline` → `cat data/.run/started` | reaper stops it at 3 h and alerts | `rm data/.run/started` inside the sandbox, or stop the sandbox from the dashboard |
| ntfy "canary FAIL ... warm_session:false" | Compass rejected the session and a cold login was needed / failed | the canary's JSON line in the run log; `sandbox connect` → `data/logs/canary-*.log` | next run tries again | one local `python scrape.py --skip-photos` to refresh the session, then `python ops/state.py push` |
| ntfy "FAILED at scrape (exit 1)" with `stream not found` | Turso dropped an idle libsql stream and the one-retry wrapper hit it inside a transaction | stage log | next run converges | rerun; if recurring in `score_photos`, add a keep-alive `SELECT 1` to its poll loop |
| ntfy "hung after 3h; stopped" | a vision batch took > 3 h, or Playwright hung | stage log; Anthropic console for the batch state | batch stays recorded in `vision_batches`; next run resumes polling | nothing unless it repeats |
| Viewer shows old photos for a listing | pre-B0 data (positional key), or `source_url` backfill masked a change | compare `photo_urls` vs `hosted_photos.source_url` for that listing | after B0, the next run detects any URL change | `ops/rehost_photos.py --listing <id>` |
| Sandbox running with nothing to do; bill creeping | reaper broken or its cron disabled | Sandboxes page (Observability); Spend Management alert | `timeout` stops it at 3 h | Stop Sandbox in the dashboard; fix the reaper |
| Launcher 500: "missing env: PIPELINE_TURSO_AUTH_TOKEN, ..." | env var not set for this environment (production vs preview) | function logs (names only, never values) | — | `vercel env add <NAME> production` and redeploy |
| Launcher 500 from `@vercel/blob`: 403 | OIDC federation disabled on the project, or the state store is not connected | Settings → Security; store → Projects tab | — | enable OIDC; connect the store with prefix `STATE_` |
| Sandbox recreated (`created: true` in launcher logs) | snapshot expired after 30 idle days, or someone removed the sandbox | launcher logs | bootstrap runs (~2 min), session seeded from Blob | none |
| Photos re-download for the whole collection | `BLOB_READ_WRITE_TOKEN` missing (nothing gets marked hosted), or `hosted_photos.source_url` not backfilled | scrape log: "uploading N new photo(s)" | — | set the token; run `ops/backfill_hosted_source_urls.py` |
| Two ntfy summaries minutes apart | laptop timer and cloud cron overlapped | both logs | safe: idempotent writes, shared checkpoint | stagger the systemd calendar or retire the timer (`ops/DECOMMISSION.md` §A) |
| A blob delete failed | Blob API hiccup | scrape log prints the URLs it could not delete | never fatal | paste the URLs into the orphan-sweep script |

## 5. Operating it

### 5.1 Trigger a run now

```bash
cd ~/code/short-list
vercel crons run /api/pipeline/run                 # or: "/api/pipeline/run?job=canary"
vercel logs --follow                               # launcher and reaper output
```

Or from anywhere: `curl -H "Authorization: Bearer $CRON_SECRET"
https://<site>/api/pipeline/run`.

### 5.2 Watch a run

```bash
npx vercel@latest sandbox connect home-search-pipeline --scope ben-gieses-projects --project short-list
tail -f data/logs/*.log
cat data/.run/started data/.run/done
```

The Sandboxes page under short-list → Observability shows status, the
current session, command history and snapshots.

### 5.3 Stop a run

Dashboard → Sandboxes → `home-search-pipeline` → Stop Sandbox; or
`npx vercel@latest sandbox stop home-search-pipeline ...`. The reaper will
have done this within ten minutes of a normal finish anyway.

### 5.4 Disable the cloud entirely

Remove the `crons` entries from `short-list/vercel.json` and redeploy (or
Settings → Cron Jobs → Disable). Then `sandbox remove home-search-pipeline`
if you want the snapshot storage back. The laptop timer is unaffected —
`ops/DECOMMISSION.md` §B.

### 5.5 Refresh the Compass session by hand

```bash
cd ~/code/home-search
python scrape.py --skip-photos       # logs in from the laptop's residential IP, re-saves the session
python ops/state.py push             # uploads it to the private store; the next launcher seeds it
```

### 5.6 Rotate a secret

`vercel env rm NAME production && vercel env add NAME production`, then
redeploy. The sandbox receives env per run, so the next launch picks it up;
nothing is baked into the snapshot.

### 5.7 Start from a clean sandbox

`sandbox remove home-search-pipeline` (snapshots may need `sandbox snapshots
delete` separately). The next launcher call recreates and bootstraps it.

### 5.8 Change the code the cloud runs

Merge to `main`. The next launch does `git fetch --depth 1 origin main &&
git reset --hard FETCH_HEAD`. To pin: `vercel env add PIPELINE_GIT_REVISION
production` with a tag or sha.

## 6. Debugging checklist

1. Did the cron fire? Cron Jobs settings → View Logs. No entry → schedule or
   deployment problem (crons only exist on production deployments).
2. Did the launcher get past auth? A 401 means `CRON_SECRET` mismatch; a 307
   means `proxy.ts` swallowed it.
3. Did the sandbox start? Sandboxes page → status. `failed` → read the
   launcher's exception in function logs.
4. Did `run.py` write `started`? If not, bootstrap failed — `sandbox connect`
   and run `bash ops/sandbox/bootstrap.sh` by hand.
5. Which stage failed? `data/logs/pipeline-*.log` inside the sandbox, or the
   ntfy message.
6. Did the reaper stop it? Reaper logs (`requestPath:/api/pipeline/reap`);
   if the sandbox is still `running` with a `done` marker, the reaper's Blob
   `put` probably threw (403 → OIDC/store connection).
7. Money: Anthropic console batch count should equal `vision_batches` rows
   submitted; Vercel Usage → Sandbox for compute; Blob store size for the
   photo store.

## 7. Invariants to preserve when changing anything

- One statement per set: no query inside a per-listing or per-photo loop
  against Turso. Tests assert counts.
- The vision checkpoint is written in the same breath as the batch is
  created, and only to Turso.
- Photo identity is the source URL; a row is never removed from
  `hosted_photos` without deleting or exporting its blob.
- The sandbox holds only the credentials a laptop run holds (minus the
  state-store token). No Vercel token, no OIDC token, ever.
- Every new route in short-list is added to the `proxy.ts` exclusion, as a
  literal, with a test.
- Secrets enter only via `vercel env add`; both repositories are public.
- `pipeline.py` stays the single orchestrator for both homes; `ops/sandbox/`
  wraps it, never replaces it.
