# Pipeline Architecture — Consolidated Decision Document

Date: 2026-08-31
Status: DECISION PENDING (Ben's call). Presents options with trade-offs and a
recommendation; disagreeing with the recommendation is meant to be easy.
Supersedes: `2026-08-30-pipeline-automation.md` (Options A/B/C, local-systemd
recommendation) and `2026-08-31-vercel-pipeline.md` (spike results, Option
Four). Both remain as records of what was known when; this document
reconciles them with everything learned since.

## 0. Decision summary

**The question:** where does the pipeline run, and how many databases exist?
Today: five stages run locally against local SQLite, and `publish.py` mirrors
the result to Turso + Vercel Blob for the short-list viewer. The local
orchestrator + systemd timer (PR #12) already automates this. The Vercel
Sandbox spike succeeded (2026-08-30, $0.03–0.05): Compass works from a
datacenter IP — warm session *and* cold login, reCAPTCHA Enterprise and AWS
WAF both passed silently. That removes the one hard blocker the first plan
identified, so "full cloud" is now a real option rather than a theoretical one.

Only four axes actually differentiate the options.

| | 1. Local-only (shipped) | 2. Turso-SSOT, scrape local | 3. Full cloud (Cron→Fn→Sandbox) | 4. Phased: 2 now, 3 gated (recommended) |
| --- | --- | --- | --- | --- |
| Fresh while laptop is off | No (catches up ≤5h after boot) | No (same) | **Yes** | After phase 3, yes |
| Databases / drift surface | 2 (SQLite + mirrored Turso) | **1 (Turso)** | 1 (requires 2's work first) | 1 after the first phase |
| New IP/bot-detection exposure | Zero — residential IP, warm session | **Zero** — scrape unchanged | Scheduled datacenter sessions; Nominatim + photo-CDN from cloud IPs **untested** | Deferred until spikes + canary pass |
| Cost (days / $ per month) | 0 more days / $0 | 2–3 days / ~$0 | 3–5 days / ~$2–10, inside Pro's $20 credit | 2–3 days now, ~1.5–2 later |

**Recommendation: Option 4** — do the Turso-single-source-of-truth cutover
now, and lift execution into the Sandbox later *only if* three cheap spikes
and a two-week durability canary pass, and only if laptop-off freshness still
feels worth it then.

## 1. Facts every option is built on (measured, not assumed)

- **Compass works from a Vercel Sandbox** (iad1, AWS egress IP): warm session
  returned 145 collection listings; cold login passed invisible reCAPTCHA
  Enterprise and an AWS WAF challenge with no interaction. One pass proves
  *feasibility, not durability* — reCAPTCHA is score-based and could degrade
  under scheduled repetition. Untested: photo-CDN downloads and
  Nominatim/OSRM from datacenter IPs.
- **Every Turso statement is an HTTP round-trip ≈ 240 ms.** One-per-row made a
  full sync take ~22 min (~5,400 writes); batching (chunk 30) cut it to ~110
  round-trips. **If anything writes Turso directly, batching is correctness,
  not optimization**, and every check-then-act loop must be audited (a
  per-photo `already_uploaded()` check once cost 12 minutes before the first
  upload; fixed by one whole-set query).
- **Blob uploads bill as Advanced Operations, per part.** The CLI's
  `--multipart true` default turned a 3,000-photo backfill into 11,000 ops and
  a 30-day store suspension. Any new upload path must be single-part REST.
  Ben is on Pro now; the 2,000-op Hobby cap behind `MAX_PHOTOS_PER_LISTING=8`
  no longer binds.
- **Turso enforces foreign keys; local SQLite didn't until PR #9.** That
  divergence caused real bugs twice (PR #8's prune order, PR #9 itself).
  PR #9 closed the FK gap locally, but two databases remain two databases.
- **Every stage is incremental** and a no-change run costs ~$0; the photo
  batch checkpoint guarantees vision scoring is never paid twice. The corpus
  is 129 listings / 4,560 photos / 991 MB and changes over days, not hours.
- **The `src/auth.py` cold-login race is fixed** (PR #13).
- **Ben's machine uptime is unpredictable** — PR #12's freshness guard was
  designed for exactly this.

## 2. Option 1 — Local-only: `pipeline.py` + systemd (shipped; the baseline)

**What it is.** What PR #12 delivers: five stages as subprocesses with flock
overlap protection, per-run logs, fail-stop semantics, and the freshness
guard; timers fire liberally and `--max-age` collapses the redundancy.
`publish.py` mirrors local SQLite → Turso + Blob and POSTs revalidate.

**What changes.** Nothing. Merge PR #12, install the units, done.

**Costs.** $0/month, 0 additional days.

**What breaks.** Nothing — this is the do-no-more baseline, and it deserves a
fair hearing: it exists, it is tested, it encodes the 22-minute-sync and
11,000-op lessons, and Compass keeps seeing the same residential IP and warm
session it always has. Staleness is bounded by laptop-off time + 5h, against
a corpus that changes over days.

**What it doesn't buy.** Two databases with a mirror script between them;
`publish.py`'s full-table sync (~110 round-trips even when nothing changed)
runs every cycle; nothing happens while the laptop is off.

**publish.py:** retained, load-bearing. **pipeline.py:** load-bearing.
**short-list:** untouched.

## 3. Option 2 — Turso as single source of truth, scrape stays local

**What it is.** Every stage reads and writes hosted Turso directly; local
SQLite and `publish.py` are deleted. Scraping, photo downloads, and the
Playwright session stay on Ben's machine (residential IP, unchanged bot
posture). The systemd timer keeps the schedule. This is *not* the old
rejected "B2 hybrid" — that kept both databases and added a handoff; this
eliminates the second database.

**What changes.**
- `src/turso_db.py`: connection + row-compat layer; the batched write
  machinery moves there from `src/turso_sync.py` — it stops being "sync" and
  becomes *the* write path.
- `scrape.py` restructured to collect-then-bulk-write: today's per-listing
  `upsert_listing` is fine at local-SQLite speed but ~645 round-trips
  ≈ 2.6 min against Turso. Batched: ~a dozen round-trips.
- Photo upload (Blob REST PUT, single-part) moves into the scrape stage; skip
  set = one `hosted_photos` query.
- `compute_commutes.py` / `score_photos.py` point at the Turso connection;
  `score.py`'s full recompute becomes one batched write.
- Pipeline ends with the revalidate POST (~10 lines — the one part of
  `publish.py` that survives). `pipeline.py`'s `STAGES` drops publish.

**Costs.** ~$0/month. **2–3 days**, largely deletion-shaped.

**What breaks / risks.**
- Every run now requires network for *every* stage, including `score.py`,
  which is currently free local math.
- A mid-run failure leaves Turso partially updated. Acceptable because every
  write is an idempotent upsert of internally-valid rows, the viewer's cache
  hides mid-run state until revalidate, and the next run converges — but it
  must keep the exit-nonzero-on-partial-failure discipline.
- `turso_serverless` row-shape quirks vs `sqlite3.Row` — contained by the
  compat layer and its tests.
- Migration risk on a working system. The honest version of this option's
  benefit is "one database and ~450 fewer lines to maintain," not "fixes
  active bugs."

**What it buys.** One database — the FK-divergence *class* disappears rather
than being patched; `publish.py` and its full-mirror sync deleted; faster
steady-state runs; and — the strategic part — it eliminates full-cloud's
hardest prerequisite: `turso_restore.py` exists only because stages are
local-SQLite-centric. Once stages speak Turso natively, an ephemeral sandbox
connects to the same DB the laptop does; nothing needs restoring.

**publish.py:** deleted. **pipeline.py:** load-bearing, minus one stage.
**short-list:** untouched.

## 4. Option 3 — Full cloud: Cron → Function → Sandbox

**What it is.** A cron in the already-deployed short-list project (which
already holds `TURSO_*` and `REVALIDATE_SECRET`) hits `/api/run-pipeline`
(guarded by `CRON_SECRET`); the function creates a Sandbox from a snapshot
with Chromium preinstalled (code via `GitSource` on the public repo),
fire-and-forgets `python pipeline.py`, returns well under the 800s cap; the
sandbox runs the stages against Turso and stops itself. Small state
(`compass_state.json`, the photo-batch checkpoint) round-trips through Blob.
`is_scraped()` swaps its local-file check for one scraped-ID-set query.

Sandbox is the only viable primitive: plain Functions die on the 800s cap
(`score_photos.py` legitimately waits minutes-to-hours); Workflow DevKit is
TypeScript-only. Verified 2026-08-30: Pro sandbox sessions up to 24h, iad1
Active CPU $0.128/hr (I/O wait unbilled), memory $0.0212/GB-hr.

**Costs.** ~$3.50/mo at 6-hourly, ~$10/mo hourly — inside Pro's $20 credit,
so ≈$0 incremental. Effort: **3–5 days from today**, or **~1.5–2 days if
Option 2 lands first**.

**What breaks / risks.** Three untested externals (Section 5). New failure
surface: cron, function, snapshot expiry, OIDC lifetimes, self-stop. Failure
visibility moves off the laptop; needs a notification path. Secrets move to
Vercel project env vars — both repos are public, so nothing may ever enter
the repo.

**What it buys.** The pipeline runs whether the laptop is on or not. Being
honest about the size of that prize: listings appear over days, and the local
freshness guard already bounds staleness at laptop-off-time + 5h. The delta
is "fresh during multi-day laptop-off stretches" — real, but modest.

**publish.py:** already gone. **pipeline.py:** load-bearing — it is precisely
what the sandbox executes. Only the systemd *timer units* become a fallback
rather than the primary trigger, and they double as the rollback path.
**short-list:** untouched, plus a cron entry and one API route (additive).

## 5. What is still unknown, and what each option risks

| Unknown | Options exposed | If it resolves badly | Mitigation / cost to find out |
| --- | --- | --- | --- |
| **reCAPTCHA Enterprise durability** under weeks of scheduled datacenter logins | 3 only | Scheduled scrapes fail login; falls back to local posture — nothing stranded if phased | Warm-session-first makes cold logins rare. **Canary: a cron'd sandbox doing only a warm-session collection fetch, 2 weeks, ~$0.50** |
| **Nominatim from cloud IPs** (per-IP policy, shared/abused cloud ranges throttled) | 3 only | Geocoding fails; new listings get neutral commute scores until a local run | Volume is tiny, UA is policy-compliant. Fallback: run commutes locally (trivial once stages share one DB). ~30 min spike |
| **Photo-CDN downloads from cloud IPs** (spike hit the collection API, not the image CDN) | 3 only | New listings' photos and vision scores wait for a local run | ~30 min spike |
| Turso free-tier quotas | 2, 3 | Upgrade or reduce cadence | Well inside limits today |
| `turso_serverless` row-shape quirks | 2, 3 | Contained in one module | Compat layer + tests |

**Every hard unknown attaches to Option 3 only.** That asymmetry is the core
of the recommendation.

## 6. The `publish.py` question, answered

**Under Option 1: retained and load-bearing** — it is the publish stage.

**Under Options 2/3/4: deleted, in its own commit — not retained as a
fallback.** A mirror script you no longer run is drift you no longer notice;
within weeks it would silently rot while appearing to be a safety net. Its
three hard-won behaviors survive, dissolved into the stages: batching (the
240 ms lesson) → `src/turso_db.py`; child-first prune ordering
(`tables_child_first`) → the delisting path; the revalidate POST → ~10 lines
at pipeline end.

The real fallback is the rollback story: the cutover archives
`data/listings.db` (fully fresh from one final `publish.py` run), and
reverting two commits restores the old flow immediately, because Turso's
schema never changes shape. `visual_scores.raw_response` is already in Turso,
so the paid vision work survives regardless.

## 7. short-list, and the work already paid for

**short-list: nothing changes, under every option.** Re-confirmed against the
code on 2026-08-31: `lib/db.ts` reads `TURSO_DATABASE_URL` via
`@libsql/client`; `lib/queries.ts` wraps reads in `'use cache'` +
`cacheTag('listings')`; `app/api/revalidate/route.ts` exists and is what
`publish.py` POSTs today. The viewer's only contract is "the Turso DB it
reads is fresh, and someone POSTs revalidate after writing." Every option
honors it.

**PR #12 (`pipeline.py` + systemd): load-bearing under every option; wasted
under none.**
- Option 1: obviously load-bearing.
- Option 2: load-bearing minus one entry in `STAGES`.
- Option 3: the orchestrator is *what the sandbox runs* — ordering,
  fail-stop, and logging transfer intact. Only the systemd timer units stop
  being the primary trigger, and even they are the rollback path.

There is no sequencing in which PR #12 becomes stranded work.

## 8. Recommendation — and the case against it

**Recommended: Option 4 — the phased path.** Merge PR #12 and run Local-only
today; do the Turso cutover next; gate the Sandbox lift behind three
~30-minute spikes and a two-week ~$0.50 canary, and build it only if
laptop-off freshness still feels worth ~2 more days then.

**The single strongest reason:** every phase is independently valuable and
none is stranded if Ben stops halfway. Local-only is already the good
outcome. The Turso cutover pays for itself *and* happens to convert full
cloud from a 3–5-day state re-architecture into a ~2-day lift. All three hard
unknowns attach only to the last phase, which is optional and reversible by
disabling a cron.

**The honest case against, so disagreeing is easy:**
- **"Stop at Option 1" is genuinely defensible.** The strongest argument for
  the cutover — FK divergence — was largely *already fixed* by PR #9, and
  schema drift is structurally prevented by `ensure_schema` deriving from
  `_SCHEMA`. The two-DB system is working, battle-tested, and free. Option
  2's marginal benefit today is "one database and ~450 fewer lines," bought
  with 2–3 days of migration risk on trust-critical code and a pipeline that
  now needs network for every stage.
- **"Jump straight to Option 3" is also defensible.** Ben leans Vercel; the
  spike succeeded; Pro's credit makes it ~free; and the phased path's caution
  costs calendar time — the canary alone is two weeks. If laptop-off
  freshness is the thing Ben actually wants, the direct route is 3–5 days and
  the unknowns get answered by building rather than waiting.
- The middle phase makes local runs network-dependent and puts 240 ms
  round-trip latency inside every stage; the batching discipline that manages
  it is a standing tax on all future write-path code.

**Measure before committing to phase 3:** the photo-CDN spike, the
Nominatim/OSRM spike, one full supervised sandbox run against a throwaway
Turso DB, and the two-week warm-session canary. Total: about a dollar and two
weeks of elapsed (not working) time.

## 9. Staged implementation path

### Phase 0 — groundwork worth doing under every option
1. **Merge PR #12** (Local-only lands).
2. **`src/auth.py` login-race fix** — done (PR #13).
3. **`src/blob_upload.py`: replace the `vercel` CLI subprocess with a direct
   single-part REST PUT.** Kills ~0.6s Node startup per photo, the undeclared
   CLI dependency, and the multipart billing foot-gun. Tests: injected HTTP
   callable (existing idiom).
4. **Unify env handling**: adopt `{**dotenv_values(".env"), **os.environ}` in
   `scrape.py`, `score_photos.py`, `check.py`, backfills. Required for any
   sandbox; harmless locally.
5. **Raise `MAX_PHOTOS_PER_LISTING` 8 → 0** (Pro absorbs it: 4,560 JPGs
   ≈ $0.02 one-time + ~$0.02/mo). Only after step 3.

### Phase 2 — Turso becomes the single source of truth
Standing rule: no per-row round-trips, no check-then-act loops.
1. `src/turso_db.py`: connection factory + row-compat layer; move
   `upsert_rows`/`replace_listing_rows`/`ensure_schema` there.
2. Batched write API: `bulk_upsert_listings()` — listings parent-first (Turso
   enforces FKs), then children via `replace_listing_rows`, chunked.
3. `scrape.py` restructure: collect, then one bulk write. Delisting prunes
   child-first via `tables_child_first()`.
4. Photo upload into the scrape stage; skip set = one `hosted_photos` SELECT.
5. `compute_commutes.py` / `score_photos.py` / `score.py` → Turso connection.
6. Cutover commit: final `publish.py` run, archive `data/listings.db`, flip
   the connection factory, pipeline ends with revalidate, `STAGES` drops
   publish.
7. Delete `publish.py` in its own commit.

**Rollback**: revert commits 6+7; the archived DB resumes as source of truth.
**Verification**: suite green; one supervised end-to-end run; short-list spot-
checked; timer still firing.

### Phase 3 (optional, gated) — lift execution into the Sandbox
**Gate first**: photo-CDN spike; Nominatim/OSRM spike; one full supervised
sandbox run against a throwaway Turso DB; two-week warm-session canary.
1. Cron in the short-list project → `/api/run-pipeline` (`CRON_SECRET`);
   secrets via `vercel env add` only.
2. Function creates the sandbox (snapshot with Chromium; `GitSource` on the
   public repo; cold-install fallback), starts `pipeline.py` detached.
3. `compass_state.json` + batch checkpoint round-trip through Blob.
4. `is_scraped()` → one scraped-ID-set query; photo dir becomes a per-run cache.
5. Self-stop via SDK + `execution_time_limit` backstop; ntfy.sh notification.

**Rollback**: disable the cron. The local pipeline + timer is still fully
functional — both execution homes share one database.

## 10. Open questions / risks
- **reCAPTCHA durability** — the one thing a single spike cannot prove; the
  canary is the cheapest answer. Manual recovery is one local run.
- **Nominatim/photo-CDN from cloud IPs** — phase-3 gates; per-stage local
  fallback exists because both homes share one DB.
- **Concurrent writers** (laptop run overlapping a sandbox run in phase 3):
  idempotent upserts make it safe but wasteful; keep the local flock, stagger
  the cron, add a `pipeline_runs` gate only if it bites.
- **Concurrent sessions in this checkout**: the tree changed mid-analysis of
  this document. Phase-2 work should run in a git worktree.
- **Vercel pricing/limits verified 2026-08-30**; re-verify before phase 3.
- **`score_photos.py` wall-time**: a vision batch can hold the flock for
  hours; fine under the freshness guard, moot in a sandbox with a 3h limit.
