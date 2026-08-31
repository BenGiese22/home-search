# Pipeline Automation — Options, Costs, and Implementation Plan

Date: 2026-08-30
Original Author: Benjamin Giese

## Findings (read this before anything else)

### What the pipeline actually is

Five stages, run in order, each a top-level script. Verified against the code
on 2026-08-30 — runtimes, dependencies, and secrets below are from reading the
scripts, not from memory:

| Stage | What it needs | Typical steady-state runtime | Secrets |
| --- | --- | --- | --- |
| `scrape.py` | **Playwright + headless Chromium**, persisted session at `data/.auth/compass_state.json`, writes `data/listings/*.json`, `data/photos/`, `data/listings.db` | ~1–4 min (login + ~5s/page collection API; photo downloads only for new listings, jittered 0.15–0.5s each) | `COMPASS_EMAIL`, `COMPASS_PASSWORD`, `COMPASS_COLLECTION_URL`/`LISTING_URLS` |
| `compute_commutes.py` | plain HTTPS to **Nominatim** (geocode, self-rate-limited 1 req/s) + public **OSRM** router. No browser. | 0s when nothing is missing; ~5–10s per new listing | none |
| `score_photos.py` | **Anthropic Message Batches API** over local `data/photos/*.jpg`; polls every 60s; checkpoints to `data/.photo_scoring_batch_state.json` | 0s when nothing is missing; **minutes to hours** when a batch is in flight | `ANTHROPIC_API_KEY` |
| `score.py` | pure computation over SQLite | seconds | none |
| `publish.py` | Turso HTTP API, **Vercel Blob via `vercel` CLI subprocess**, revalidate POST | ~1–3 min steady-state | `TURSO_DATABASE_URL`, `TURSO_AUTH_TOKEN`, `BLOB_READ_WRITE_TOKEN`, `SHORT_LIST_URL`, `REVALIDATE_SECRET` |

**Every stage is already incremental.** This is the property that makes any
cadence safe:

- `scrape.py`: `is_scraped()` gates photo downloads and store writes;
  `needs_field_backfill()` gates `--backfill-missing`; `download_photos()`
  skips files already on disk.
- `compute_commutes.py`: `get_listing_ids_missing_commute()`.
- `score_photos.py`: `get_listing_ids_missing_visual_score()` plus the batch
  checkpoint file — **nothing is ever submitted (and paid for) twice**, even
  across crashes.
- `score.py`: recomputes everything, but it's free local math.
- `publish.py`: upserts are idempotent; `hosted_photos` gates photo
  re-uploads; exits non-zero on partial failure so a cron caller has a signal.

**A no-change run costs ~$0 in API spend.** Photo scoring is the only paid
stage and it only scores listings with no `visual_scores` row. Cadence does
not change Anthropic spend — only *state loss* does. A rebuilt DB with an
empty `visual_scores` table would re-score the corpus at roughly $10–20 of
avoidable spend. This is a real argument for treating the local DB carefully
in any cloud design.

### Where state lives (the crux)

`data/` on Ben's disk is the source of truth: `listings.db`, `listings/*.json`,
`photos/` (~1 GB), `.auth/compass_state.json`, `.photo_scoring_batch_state.json`.
**Turso is a downstream mirror** — `publish.py` pushes local → Turso and there
is no code path that pulls Turso → local. Vercel Blob holds only a capped
subset of photos. Any design that runs the pipeline off this machine must
answer: where does this state go, and what happens to the embedded
vision-scoring work if it's lost.

### How fast does the collection actually change

Slowly. A full second pipeline pass on 2026-08-28 found **4 new and 2
delisted** listings over a multi-day gap. Listings appear over days, not
minutes. No scenario here makes an hourly signal beat a twice-daily one — and
every extra scrape is another authenticated headless-Chromium session against
Compass's bot-detection surface.

### Verified Vercel facts (fetched 2026-08-30 from vercel.com/docs)

- **Cron Jobs**: 100 per project on every plan. **Pro: once-per-minute minimum
  interval, per-minute precision.** Cron itself is free; you pay for the
  function it invokes.
- **Functions / Fluid Compute**: default 300s, **Pro max 800s (13.3 min)**.
  iad1: Active CPU **$0.128/hr**, Provisioned Memory **$0.0106/GB-hr**,
  invocations $0.60/M. Bundle limit 5 GB; OCI/Docker container images are a
  supported deployment target (the docs name Chromium explicitly) — so
  Playwright-in-a-Function is not flatly impossible, but inherits the 800s cap.
- **Vercel Sandbox**: GA. Firecracker microVMs, full root, Ubuntu, Python 3.14,
  a Python SDK, 32 GB NVMe disk. **Pro: max session 24 hours**, up to
  8 vCPU/16 GB (default 2 vCPU/4 GB). iad1: Active CPU **$0.128/hr** (I/O wait
  not billed), Provisioned Memory **$0.0212/GB-hr**, creations $0.60/1M,
  egress $0.15/GB (downloads free).
- **Workflow DevKit**: TypeScript-only. Cannot run the existing Python code.
- **Vercel Blob**: on Pro, usage draws from the monthly credit; advanced ops
  $5.00/1M on-demand, storage $0.023/GB-mo. The 2,000-op Hobby cap that
  motivated `MAX_PHOTOS_PER_LISTING=8` **does not bind on Pro**.
- **Pro plan**: $20/mo platform fee **includes $20/mo usage credit** applied
  across Sandbox, Functions, and Blob before any on-demand billing.

## Option A — Fully Vercel-hosted, scheduled (Cron → Function → Sandbox)

### Why Sandbox and not the other primitives

- **Plain Functions**: the 800s Pro cap is the killer — `score_photos.py`
  legitimately waits minutes-to-hours on a batch. Container-image Functions
  get Chromium+Python in, but not past the duration cap.
- **Workflow DevKit**: right shape (durable steps, hours-long waits) but
  TypeScript-only — Python/Chromium still doesn't run inside it.
- **Sandbox**: a real Linux VM with root, Python, a 24h Pro session cap, and
  disk. The existing scripts run **nearly unchanged**. This is the only
  primitive where the pipeline ports instead of being rewritten.

### Architecture

1. One cron entry in `vercel.json` (e.g. `0 13 * * *`) hitting
   `/api/run-pipeline`, guarded by `CRON_SECRET`.
2. The function calls `Sandbox.create()` from a snapshot with
   apt/pip/Playwright-Chromium preinstalled, fire-and-forgets
   `python pipeline.py`, and returns well under 800s.
3. The sandbox restores state, runs the five stages, pushes state back, stops.

### What has to change about state (no hand-waving)

- **Turso becomes the system of record.** New `src/turso_restore.py` — the
  inverse of `src/turso_sync.py` — hydrates a fresh `data/listings.db` at run
  start. `visual_scores.raw_response` is already synced, so the paid work
  survives. This module does not exist today.
- **Photos move to Blob entirely.** Raise `MAX_PHOTOS_PER_LISTING` to 0.
  `score_photos.py` needs photos locally only for listings missing a visual
  score — exactly the listings the same run's scrape just downloaded. No bulk
  restore needed; the local photo dir becomes a per-run cache.
- **`data/listings/*.json` staleness gates need a rethink.** `is_scraped()`
  checks local file existence; in an ephemeral sandbox every listing looks
  unscraped and every run re-downloads every photo. Either persist the JSON
  store to Blob or change `is_scraped` to consult the DB. This touches the
  oldest, most load-bearing skip logic in the repo.
- **Small state to Blob**: `compass_state.json` and the batch checkpoint both
  need upload-at-end/download-at-start. Losing the checkpoint breaks the
  no-double-billing guarantee.
- **Env handling unification**: `scrape.py` and `score_photos.py` read secrets
  via `dotenv_values(".env")` only; a sandbox supplies process env vars. Every
  stage needs the `{**dotenv_values(".env"), **os.environ}` merge `publish.py`
  already uses.
- **`publish.py`'s CLI shell-out**: replace with a direct Blob API `PUT`.

Alternative considered: **persistent sandboxes / Drives** keep `data/` alive
across runs with near-zero code change. Rejected as primary: Drives are beta,
snapshots expire 30 days after last use, and making a Firecracker VM image the
only copy of a database with paid scoring in it is a bad gamble.

### What breaks / risks

- **Compass bot-detection from datacenter IPs is the big unverifiable.** All
  scraping today comes from a residential IP with a long-lived session.
  Sandbox egress comes from cloud IP ranges. **This cannot be verified from
  documentation — only by trying it.** If Compass blocks it, Option A is dead
  regardless of cost.
- Failure visibility moves to the Vercel dashboard; needs a notification path.

### Cost at hourly / 6-hourly / daily (iad1, 2 vCPU / 4 GB)

Assumptions: steady-state run ≈ 6 min wall / ~1 min active CPU (mostly network
I/O, which Sandbox does not bill as CPU); ~20 runs/month carry new listings and
wait on a vision batch, ≈ 75 min wall / 3 min CPU each.

| Cadence | Runs/mo | Provisioned memory | Active CPU | Est. total | Fits Pro's $20 credit? |
| --- | --- | --- | --- | --- | --- |
| Hourly | 720 | ~380 GB-hr → $8.05 | ~15 hr → $1.92 | **≈ $10/mo** | Yes (~half the credit) |
| Every 6h | 120 | ~140 GB-hr → $2.97 | ~4 hr → $0.51 | **≈ $3.50/mo** | Yes, comfortably |
| Daily | 30 | ~81 GB-hr → $1.72 | ~2 hr → $0.26 | **≈ $2/mo** | Yes, trivially |

**Honest answer on "is Pro enough": yes — financially, with real headroom.**
Even hourly lands around $10/mo against a $20 credit that currently goes
unused. **The reasons not to pick Option A are not billing**: they are (1) the
state re-architecture above (~3–5 days touching the most trust-critical code
in the repo), and (2) the unverified Compass-from-a-datacenter-IP risk, which
could strand all that work.

## Option B — Hybrid

- **B1: Vercel cron → webhook → Ben's box.** Requires exposing the box
  (Tailscale Funnel / cloudflared). Everything still runs where the state is;
  Vercel contributes only a clock. The box already has several good clocks.
- **B2: scrape locally, host stages 2–5.** Splits state worse than Option A:
  all of A's state cost, none of its "no machine involved" benefit — the box
  still has to be on for the only stage that cannot be skipped.

Not recommended. The one hybrid idea worth keeping: `publish.py` already *is*
the sync protocol between local and cloud. The right division of labor exists.

## Option C — Local `pipeline.py` + systemd timer (recommended)

One orchestrator, run manually or by a systemd user timer. The five stages
already behave like pipeline stages; the only thing missing is the thing that
runs them in order.

### Design

`pipeline.py` (new, top-level, stdlib-only):

- `STAGES` — ordered `(name, argv)` pairs: `scrape` → `commutes` →
  `score-photos` → `score` → `publish`, each run as a subprocess so every
  script keeps its own `__main__` behavior, `.env` loading, and crash
  isolation. The runner is an injected callable (project idiom), so tests
  never spawn processes.
- Flags: `--dry-run`; `--from=STAGE` / `--only=STAGE`; `--skip-publish`;
  passthrough scrape flags (`--limit=N`, `--new-listing`,
  `--backfill-missing`, `--skip-photos`) forwarded to the scrape stage only.
- **Lock file** (`data/.pipeline.lock` via `fcntl.flock`, non-blocking): a
  timer must never overlap a photo-scoring run waiting on a batch. Second
  invocation exits 0 with "already running" — a skipped run is not a failure.
- **Logging**: tee each stage to `data/logs/pipeline-YYYYMMDD-HHMMSS.log`;
  end-of-run summary (stage → status → duration).
- **Failure semantics**: a nonzero exit stops the pipeline and propagates, so
  systemd marks the unit failed. `publish.py`'s partial-failure exit still
  leaves valid local state — report, exit nonzero, let the next run retry.

`ops/systemd/` (new, committed, installed manually):

- `home-search-pipeline.service` — `Type=oneshot`, `WorkingDirectory` set (the
  scripts resolve `data/` and `.env` relative to cwd — load-bearing),
  `Environment=PATH=` including wherever the `vercel` CLI lives.
- `home-search-pipeline.timer` — `OnCalendar=07:00` and `19:00`,
  `Persistent=true` (**Ben shuts this box down**; a missed window runs at next
  boot), `RandomizedDelaySec=15m`.
- Install: `systemctl --user enable --now`, plus `loginctl enable-linger bengi`.

### Recommended cadence: twice daily

Argued, not assumed: listings appear over days; the viewer's freshness
requirement is "before Ben and Megan look at it", not "within the hour". Twice
daily gets same-day pickup while keeping authenticated Compass sessions to
2/day instead of 24. Hourly buys nothing — the paid stage is staleness-gated
so it would not cost API money, but it would cost scraping-surface risk for
zero information gain. If twice daily feels slow, `OnCalendar` is a one-line
change.

### What it costs / what it buys

$0/month. ~0.5–1 day of work. No state moves; the embedded vision scores stay
where they are; Compass keeps seeing the same residential IP and session.
Buys: ordering enforced, overlap protection, resumability, logs, unattended
twice-daily freshness. Does not buy: runs while the box is off (mitigated by
`Persistent=true` catch-up).

## Recommendation

**Do Option C now.** A day of work, zero dollars, zero new failure modes, and
it delivers the actual goal. **Keep Option A in the back pocket** — genuinely
viable on Pro (~$3.50/mo at 6-hourly, inside the $20 credit), gated not by
money but by a 3–5 day state re-architecture and an untestable-from-docs
question about Compass and datacenter IPs. If Option A is ever built, step 0
is a 15-minute spike: launch one Sandbox by hand, run
`scrape.py --limit=1 --skip-photos`, and see whether Compass lets it log in.
That single experiment decides the architecture and costs pennies.

## Staged Steps (dependency order, one commit each) — Option C

1. **`tests/test_pipeline.py` + `pipeline.py`**: stage list, ordering, flag
   parsing, dry-run. Injected runner records argv; assert order,
   `--from`/`--only`/`--skip-publish` slicing, scrape-flag passthrough,
   `--dry-run` runs nothing, first nonzero exit stops the chain.
2. **Lock + logging.** flock single-instance guard, log-file tee, summary line.
3. **`ops/systemd/*` + install docs.** Unit files committed; installation is a
   documented manual step for Ben.
4. **(Optional, independent) `src/blob_upload.py`**: replace the `vercel` CLI
   subprocess with a direct Blob REST `PUT`. Removes the Node-process-per-photo
   cost and the undeclared CLI dependency. Worth doing regardless of hosting;
   required only for Option A.
5. **Journal entry** in `docs/journal/decisions.md`: cadence decision and the
   Option A spike verdict if/when run.

Verification: full suite green; `python pipeline.py --dry-run` prints the five
commands; one real supervised run end-to-end; then `systemctl --user
list-timers` shows the next trigger.

## Open Questions / Risks

- **Does Compass tolerate datacenter-IP logins?** Unknown and undocumented;
  decides Option A's viability. Cheap to test.
- **`score_photos.py` wall-time under a timer**: a batch can hold the lock for
  hours; with twice-daily runs and `Persistent=true` this self-heals, but a
  `--no-wait` submit-only mode is a possible refinement.
- **`vercel` CLI on PATH under systemd** (until step 4): the unit file must set
  `Environment=PATH=` explicitly; `publish.py`'s fail-fast check catches a
  mistake loudly.
- **Nominatim usage policy** (1 req/s, identifying UA) is respected today.
- **Failure notification** is journald-only here. A systemd `OnFailure=` unit
  that curls ntfy.sh is one file if silent overnight failures bite.
- **Sandbox pricing/limits verified 2026-08-30** and Vercel changes these
  often. Re-verify before ever building Option A.
- **Blob photo cap**: `MAX_PHOTOS_PER_LISTING=8` is a Hobby-era constraint;
  raising it on Pro is ~$0.02 one-time and a cheap independent win.
