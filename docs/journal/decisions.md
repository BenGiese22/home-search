# Decisions log

Append-only, dated entries capturing the *why* behind decisions in this
project — especially the ones that only ever happened in conversation and
were never written down anywhere else. Formal architecture lives in
`docs/superpowers/specs/` and `docs/superpowers/plans/`; this file is the
narrative connective tissue between them.

## 2026-07-19 — v0 pipeline: scrape, store, browse

First working version. Playwright drives an authenticated Compass.com
session, listings get scraped and written to a resumable per-listing JSON
store (`is_scraped()` skip-if-exists logic gates both photo downloads and
JSON writes), and a CSV + HTML gallery get regenerated from that store on
every run. No database yet — the JSON store was the source of truth.

## 2026-07-late — discovered the Compass collection API returns full listing data

Investigating the realtor-shared "collection" saved search, we found its
paginated internal JSON API (`/api/v3/collections/listings/paginated`)
returns the *same* full per-listing data (price, beds, baths, amenities,
photo URLs — everything) as an individual listing page's embedded JSON, in
one cheap (~5s) call per page. This single fact ended up seeding two later
features: it meant "check what's new" and "get full data" are the same
operation (change detection got cheap), and it meant a full metadata
re-scrape of the whole collection costs almost nothing (only the photo
downloads are slow).

## 2026-08-13 — Compass filters tightened: 323 → ~115 matches

Ben tightened the saved-search filters on the Compass portal directly
(price $480K–$655K, 3+ beds, Arvada/Broomfield/Westminster/Lafayette),
cutting the realistic match set from a historical ~323 down to ~114–115
current listings. This made "just re-scrape everything" a viable default
posture instead of something to be careful about.

## 2026-08-13 — SQLite over Docker Postgres

Considered spinning up a local Postgres in Docker for queryability; chose
SQLite instead — stdlib-only, no server process, no container to manage,
and the dataset (a few hundred listings at most) never needed anything
Postgres offers. A future cloud-DB + hosted-app architecture was discussed
and explicitly deferred as premature at this stage.

## 2026-08-13 — change detection built and code-reviewed

Because the collection API is cheap and returns full data, "detecting
changes" and "keeping the database fresh" turned out to be the same
mechanism: `check.py` snapshots prior prices, re-fetches the collection,
upserts everything, and diffs old vs. new. Built via a dispatched subagent,
then gated behind a `code-review` pass before merging — findings included
a missing try/except around the collection fetch, a duplicate-listing
double-count bug in the diff logic, and duplicated Playwright auth
bootstrap code between `scrape.py` and `check.py` (extracted into a shared
`launch_authenticated_page()` helper in `src/auth.py`). All three were
fixed before merge.

Shortly after, `scrape.py` itself was changed to upsert every fetched
listing into SQLite immediately, rather than only rebuilding the DB from
JSON at the end of a run — keeping the database live-fresh on every fetch,
not just after a full re-scrape.

## 2026-08-14 — empirically audited what Compass's own filters already enforce

Rather than guessing which structural cutoffs (baths, sqft, lot size,
parking, year built) still needed local enforcement, we live-fetched the
current 114-listing collection and computed the actual min/max/distribution
per field. Result: Compass's collection filter enforces price, beds, and
geography, but does **not** enforce baths, sqft, lot size, parking, or year
built — so those remained open questions for local scoring logic rather
than already-solved problems.

## 2026-08-14 — OSRM/Nominatim over an LLM-prompted commute estimate

The original v0 design plan assumed Claude would be prompted directly for
driving-distance/commute estimates. Before committing to that for the
baseline-scoring rubric, we ran a real side-by-side validation: model-
generated distance/time estimates vs. actual OSRM (routing) + Nominatim
(geocoding) results for 8 sample addresses. The LLM estimate was reasonably
close for Denver but off by 2–3x for the less-documented Medtronic/Lafayette
destination. OSRM and Nominatim are both free, keyless, public APIs, and
accurate — for this project's small scale (~115 listings), there was no
reason to prefer a guess over ground truth. Decision: use OSRM + Nominatim
for the commute factor, not an LLM prompt.

## 2026-08-14 — baseline-scoring v1 spec written

Full spec at `docs/superpowers/specs/2026-08-13-baseline-scoring-design.md`.
Key decisions baked in: two new hard cutoffs enforced locally as a
`passes_filters` flag rather than a hard delete — baths ≥ 2 and lot ≥ 6,000
sqft (Ben's stated deal-breakers, and also flagged as filters he should add
to the actual Compass portal search, which the scraper can't edit itself).
Composite weighting: commute 35% (80% Medtronic leg / 20% Denver leg,
curved around Megan's stated 20-minute-ideal / 30-minute-ceiling
commute), sqft 20%, condition 20% (renovation keywords dominant over raw
`year_built`), outdoor/hosting 15% (explicitly a weak keyword placeholder
until photo scoring exists), parking 10% (step function: 2+ = 100, 1 = 90,
0 = 0). Ben flagged discomfort with the commute weight feeling high and
general worry about over-fitting a fixed rubric to "SOOO many factors," but
approved on the basis that weights are easily iterable named constants, not
a one-shot decision.

## 2026-08-15 — baseline-scoring implementation begun via subagent-driven development

Wrote the full 8-task implementation plan
(`docs/superpowers/plans/2026-08-14-baseline-scoring.md`) and started
executing it via the `subagent-driven-development` process on branch
`bgiese/baseline-scoring`: a fresh implementer subagent per task, a
task-scoped reviewer after each, fix loops as needed. As of this entry,
Tasks 1–2 (the `src/commute.py` module: geocode/route parsing, then
destination resolution and per-listing commute computation) are complete
and merged into the branch.

## 2026-08-15 — photo-based scoring: brainstorming begun

With the stats-only baseline-scoring feature mid-implementation, Ben
turned attention to the next architectural piece: scoring listing photos
with a vision-capable Claude model, replacing the weak text-keyword
placeholders (outdoor/hosting, and part of condition) with real visual
judgment. Early direction agreed in conversation, ahead of the formal
spec: batch all of a listing's ~15–25 photos into one request rather than
one call per photo; use `output_config.format` for structured JSON output
(a 0–10 rubric per visual attribute, mirroring the existing sub-score
shape); use the Message Batches API (50% cost discount) since this is a
one-time-ish batch job across the ~115-listing collection; default to
Claude Sonnet 5 rather than Opus, since the task is straightforward visual
judgment rather than deep reasoning. This entry will be superseded by a
formal spec once the brainstorming session concludes.

## 2026-08-15 — baseline-scoring implementation completed

All 8 tasks of `docs/superpowers/plans/2026-08-14-baseline-scoring.md` landed
on `bgiese/baseline-scoring` via subagent-driven development. Notable along
the way: a controller ruling reverted an implementer's unauthorized 20-minute
floor added to the Denver commute leg (the design spec explicitly says that
leg has no threshold, unlike the Medtronic leg — the *test* the implementer
was chasing was the actual bug, not the code). The final whole-branch review
independently re-derived all 362 listings' composite scores directly from the
database with zero mismatches, but caught one dropped spec requirement: the
design spec's error-handling section calls for flagging listings with any
imputed (neutral-50) sub-score as having "incomplete data," and that half of
the requirement was never implemented across any of the 8 tasks (the
neutral-score-instead-of-zero half was). Fixed in one final fix wave —
`ScoreResult.has_incomplete_data`, a matching `scores` column, and a visible
marker in `score.py`'s report. On the real 362-listing collection this
affects exactly the 43 listings with failed geocodes.

## 2026-08-15 — photo-scoring v2 spec and plan written

Full spec at `docs/superpowers/specs/2026-08-15-photo-scoring-design.md`,
implementation plan at `docs/superpowers/plans/2026-08-15-photo-scoring.md`.
Superseded the "2026-08-15 — photo-based scoring: brainstorming begun" entry
above. Key decisions: room-by-room condition scoring (kitchen, bathrooms,
living space, basement, garage — not one blob score), weighted toward
kitchen (35%) and bathrooms (30%) per Ben's explicit priority; garage
applicability driven by the listing's own `parking_spaces` field rather than
asking the vision model to guess; basement gets a genuine "not applicable"
state for homes that don't have one; an omitted room (photographed listing,
missing that one category) scores low and flagged, but a listing with too
few photos overall (`< 5`) is excluded from vision scoring entirely rather
than penalized, since that usually means the listing is still being staged.
Implementation is queued behind finishing the photo backfill (only 61 of 362
listings have any downloaded photos as of this entry) and behind the
stale-listing removal work below, since both touch `src/db.py`.

## 2026-08-15 — stale-listing removal: hard delete, not soft-archive

Local storage (SQLite + JSON store + downloaded photos) has always been
purely additive — nothing has ever removed a listing once Compass's live
collection stops returning it. With 362 listings locally against ~117
currently live on the portal, this gap became worth closing. Initial design
was a soft-archive: a nullable `delisted_at` column on `listings`, excluded
by default from `query_listings`/the ranked report, but keeping the row (and
its `commute`/`scores` history) in case a listing relisted later — with only
its photos hard-deleted to avoid disk bloat.

Ben reconsidered and asked for a full hard delete instead: "they're just
never gonna be referenced again so might be better to just hard delete
everything." Confirmed the tradeoff explicitly before proceeding — a
delisted listing that later relists will be treated as brand new (re-scraped,
re-geocoded, re-scored, re-photographed from scratch) rather than resuming
history — and Ben accepted that tradeoff.

Final design: extend `diff.py`'s existing `ChangeReport`/`compute_changes()`
(which already has a `before` snapshot of every locally-known listing_id via
`get_price_snapshot()`) with a third field, `delisted_ids` — anything in
`before` but absent from the fresh collection fetch. On detection, cascade
hard-delete: `amenities`/`photo_urls`/`commute`/`scores` rows, the `listings`
row itself, the listing's photo directory, and its JSON store file. Wired
into both `scrape.py` and `check.py`, since both already do a full collection
fetch — not gated behind either one specifically. Not yet implemented as of
this entry; queued to start once `bgiese/baseline-scoring` merges, since this
work also touches `src/db.py`.

## 2026-08-16 — stale-listing removal implemented and hardened through five review rounds

Implemented the design from the entry above on `bgiese/stale-listing-removal`:
`is_pinned` column + migration on `listings`, `derive_pinned_ids_from_urls()`
regex backstop, `compute_changes()`'s new `delisted_ids` field, and a shared
`should_apply_delisting()` / `apply_delisting()` / `run_delisting()` in
`src/diff.py` wired into both `scrape.py` and `check.py`.

Five successive `/code-review` rounds each found a genuine new issue the
previous fix missed — worth recording since the pattern itself is the
lesson: an exception during collection fetch treated every local listing as
delisted (fixed via a `fetch_succeeded` gate); a *successful but empty*
fetch defeated that same gate (fixed via a fraction-based circuit breaker,
`should_apply_delisting`, capped at 50% of eligible listings with
deliberately no small-count exemption — a tiny bypass was tried and
rejected because 100% of a small collection is exactly as suspicious as
100% of a large one); `LISTING_URLS`-tracked listings looked delisted since
they're never returned by the collection fetch (fixed via a `pinned_ids`
parameter); a non-numeric-ID URL silently lost pin protection (fixed by
moving to a persisted `is_pinned` DB flag as the authoritative source, with
the URL-regex heuristic demoted to a backstop union — safe to use for
*granting* protection, unsafe as the sole gate for a *destructive* action);
`backfill_db.py`/`backfill_photos.py` silently stripped pins by omitting
`is_pinned` on upsert; and the `is_pinned` migration itself resets
pre-existing rows with no backfill, confirmed live against this repo's own
`.env` (fixed via the regex backstop union, closing exactly this window).
Full reasoning and code for each round lives in the branch's commit history.

Round 5 found three more real but non-live-risk gaps — no un-pin mechanism,
`scrape.py` holding its browser session open through the delisting cascade,
and the regex backstop missing `/homedetails/` address-slug URLs — all
parked in `docs/journal/backlog.md` rather than fixed, since none of them
can cause data loss against this repo's actual current state, unlike every
finding in rounds 1–4. Treating this as the natural stopping point for this
hardening cycle before merging.

---

## 2026-08-16 — reset local storage for a fresh scrape

With stale-listing removal merged, wiped `data/listings.db`, `data/photos/`,
`data/listings/` (JSON store), `data/listings.csv`, and `data/gallery.html`
to start clean — the prior 362-listing local dataset had accumulated well
past the ~117 currently live on the portal, and now that delisting keeps
the DB self-cleaning going forward, there's no reason to keep migrating old
data through the new logic instead of just re-scraping. Kept
`data/.auth/` (the Playwright login session) since re-authenticating is the
only non-trivial part to regenerate. `get_connection()` re-creates the DB
schema automatically on first connect, so no separate init step is needed
— the next `scrape.py` run rebuilds everything from scratch.

## 2026-08-16 — price-per-score metric implemented; layout/floor-plan folded into photo-scoring design

Implemented both items queued after the stale-listing-removal merge:

- **`score.py` price-per-score field.** Added `value_score(composite, price_numeric)`
  = composite points per $100k of price, printed as a `value=` column on every ranked
  report line. `None` (rendered `n/a`) when `price_numeric` is missing rather than
  defaulting to 0, since a "Contact agent" listing has nothing to divide by. Sorted by
  composite by default, unchanged; a new `--sort-by-value` flag sorts by this field
  instead, with `None` values always sorting last regardless of direction. Deliberately
  not blended into the composite itself — a separate lens for weighing score against
  price, not a scoring factor. No test file, matching `score.py`'s existing untested-
  orchestration precedent; smoke-tested against the (currently empty, post-reset) real
  DB.
- **Layout/floor-plan photos folded into the photo-scoring design and plan**, not yet
  implemented — `score_photos.py`/`src/vision.py` still don't exist; that work is still
  blocked on finishing the photo backfill (see Open, below). Added a `layout_plan`
  field to the vision response schema (`present: bool`, `clarity_score: 0-10 | null`)
  and a matching `VisualScoreResult.has_layout_plan`/`layout_plan_clarity_score`, stored
  in a widened `visual_scores` table. Confirmed direction from Ben: assess it (it's
  useful to know which listings include one, and how legible it is), but never let it
  enter `condition_photo_score`, `outdoor_photo_score`, or the composite — captured as
  information only, the same pattern `raw_response` already uses for anything Ben might
  want to look at directly without it being a scored signal.

## 2026-08-16 — anti-botting countermeasures for photo downloads, before running the full backfill

Ben raised a concern before kicking off the ~4,000-photo fresh backfill: could this
pattern of requests get the Compass account flagged or shut down? Audited the actual
request shape rather than guessing:

- Listing/collection data was already low-risk: it goes through a real authenticated
  Playwright session with persisted `storage_state` (no repeated logins), and the
  collection comes from one batched paginated API call
  (`fetch_collection_listings`), not one page load per listing.
- The one real gap: `download_photos()`'s `fetch_bytes` (in both `scrape.py` and
  `backfill_photos.py`) used a bare `requests.get()` &mdash; no cookies, the default
  `python-requests` User-Agent, and no delay between calls. For the backfill that's
  ~4,000 back-to-back unauthenticated-looking requests to Compass's media CDN from one
  IP in a tight loop &mdash; the one part of the pipeline that didn't look like a
  browser at all.

Fixed both scripts the same way:
- Photo downloads now go through the authenticated page's own request context
  (`page.request.get(url)`) instead of a standalone `requests` call &mdash; reuses the
  real session's cookies and browser HTTP/TLS fingerprint, so each photo fetch looks
  like a normal in-page image load. `src/photos.py`'s `download_photos()` gained an
  optional `sleep_fn` parameter (no-op by default, so existing tests still run
  instantly) that callers use to inject pacing.
- Added `PHOTO_JITTER_MIN/MAX_SECONDS` (0.15&ndash;0.5s) random delay between photo
  downloads in both scripts, called only after an actual network fetch (never after a
  skip-because-already-downloaded).
- `backfill_photos.py`'s download loop had to move *inside* the `with
  launch_authenticated_page(...)` block &mdash; it previously ran after the browser
  had already closed, which would have broken outright once photo fetches started
  depending on `page.request`.

Deliberately did not add concurrency/parallel downloads to compensate for the slower,
paced sequential shape &mdash; sequential-with-jitter is the safer pattern here, not
something to optimize away.

Also added a `--limit=N` flag to `scrape.py`, so the very first run after this change
can process a small batch (e.g. 10&ndash;20 listings) rather than committing straight
to the full ~117-listing/~4,000-photo backfill. Worth it specifically because
`download_photos()`'s per-photo `try/except` means a systemic problem with the new
`page.request`-based fetch (wrong API assumption, CDN rejecting the request shape)
would fail *silently* &mdash; the script exits 0 with every photo individually logged
as skipped, not a loud error. `--limit` only caps which collection listings get
upserted/have photos downloaded *this run*; `compute_changes`/delisting still evaluate
against the full fetched collection, so a small test batch is never misread as mass
delisting.

Also added `--new-listing` and `--force`, both `scrape.py`-only, both meant to pair
with `--limit`:

- **`--new-listing`** filters the collection down to not-yet-scraped listings *before*
  `--limit` slices it. Without this, `--limit` alone re-slices the same fixed-order
  prefix of the fetched collection every run (Compass's collection API returns a
  stable order), so once that prefix is scraped, a repeated `--limit=N` run does zero
  new work. `--new-listing --limit=N` is what makes a chunked, staged backfill (several
  runs over time, spreading the ~4,000-photo download out rather than one long burst)
  actually make progress each run. `upsert_listing` still runs against every fetched
  listing regardless of this filter &mdash; it's cheap, no network, and it's what keeps
  price-change detection correct even during a staged run that skips most photos.
- **`--force`** bypasses the "already scraped, skip" check so `_save_listing` runs
  again for a listing the JSON store already has an entry for. Exists because
  `_save_listing` writes that JSON marker *unconditionally* after `download_photos()`,
  even if some individual photos failed &mdash; `download_photos()` swallows per-photo
  errors and never raises. A transient network blip mid-listing can silently leave a
  listing "scraped" with an incomplete photo set that no future unlimited run will
  ever retry, since `is_scraped()` only checks presence, not completeness. `--force`
  is the recovery path, and it's cheap to use: `download_photos()`'s own per-photo
  `dest.exists()` check means a forced retry only fetches what's actually missing, not
  a full re-download.

## 2026-08-25 — rubric and assessment-prompt changes from the calibration findings

Acted on 5 of the items `docs/house-tour-calibration-findings.md` flagged, per Ben's
go-ahead on each:

**`src/scoring.py` (real, live rubric):**
- **Room count factor added.** New `score_room_count(beds, baths, ...)`, min-max
  normalized against the collection exactly like `score_sqft`. New
  `WEIGHT_ROOM_COUNT = 0.10`, carved out by dropping `WEIGHT_COMMUTE` 0.35→0.30 and
  `WEIGHT_PARKING` 0.10→0.05 (still sums to 1.0). `ScoreResult`/`CollectionStats`
  gained matching fields (defaulted, so existing tests keep constructing them
  unchanged); `scores` table gained a migrated `room_count_score` column.
- **`score_condition`'s no-keyword fallback softened 0.0 → 40.0** (new
  `CONDITION_NO_KEYWORD_SCORE`), matching `OUTDOOR_NO_KEYWORD_SCORE`'s
  already-existing "weak signal, not proof of absence" pattern — the two were
  inconsistent for no real reason.
- **Both keyword lists broadened**, then corrected once against real data:
  adding `"fenced"`/`"trees"` to `OUTDOOR_KEYWORDS` fixed 93rd Way's known false
  negative but immediately created a false *positive* on 77th Dr (Ben's most
  decisive real NO) via its description's "Fenced yard for the family pet" —
  confirmed live with `score.py`, not just reasoned about. Removed both; kept
  `"landscaped"/"landscaping"/"patio"/"deck"/"garden"/"fire pit"/"hot tub"`,
  which still fix 93rd Way without the boilerplate-triggering risk. Re-verified
  after the fix: 93rd Way's `outdoor_score` stayed 100.0, 77th Dr's dropped back
  to 40.0.

**`assess_six_houses.py` (still-prototype vision assessment):**
- **Staging detection, in both directions.** New `staging_flags` schema field:
  explicit instruction to read for a literal "Virtual Staged"/"Virtually Staged"
  watermark, *and* a separate look for unwatermarked staging tells (off
  furniture scale/shadows, unrealistic rendering). Verified live against
  Kipling Place (the confirmed-virtually-staged house, watermark visible in
  `data/photos/2126174613662059081/03.jpg`): correctly detected
  `watermarked_staging_detected: true` with the specific rooms named. Did not
  flip the overall verdict to NO on its own — it's a confidence signal per the
  prompt's design, not an automatic veto.
- **Aerial/drone photos now explicitly disregarded.** Confirmed real via a
  direct photo read: Kipling's `39.jpg` is a pure aerial shot of a nearby
  school/ballfields, not the house. Ben's call: not enough signal either way
  to be worth classifying/routing elsewhere — just excluded from scoring and
  observations entirely.
- **`garage.attached` promoted from stray prose to a real field** (own
  `_GARAGE` schema, informational only like `layout_plan` — "a formal note,"
  not scored). Verified live: Kipling's garage correctly read `attached: true`
  from exterior photos alone.

Not yet re-run: the existing 7 entries in `docs/claude-six-house-assessment.md`
still reflect the old prompt/schema. Left as-is pending Ben's call on whether to
regenerate them (each costs ~$0.15-0.25) or treat them as a frozen first-pass
snapshot the findings doc already analyzed.

## Open

- **House tour feedback and calibration findings — done.** All 7 homes Ben
  and Megan toured are written up in `docs/house-tour-feedback.md`, and
  compared against Claude's photo-only read (`assess_six_houses.py`) and the
  current live v1 algorithm in `docs/house-tour-calibration-findings.md`.
  Layout/vertical-circulation confirmed as the single most repeated real
  rejection reason (4 of 7 houses); staging, both real and virtual,
  demonstrated to actively fool a photo-only assessment on two separate
  houses; concrete, verified false-negatives found in both
  `RENOVATION_KEYWORDS` and `OUTDOOR_KEYWORDS` against real listing
  descriptions. Nothing acted on yet — pure calibration data, flagged for
  whenever the rubric next gets tuned.
- **Photo backfill incomplete.** Only 61 of 362 listings have any downloaded
  photos (2,133 photo files total) — though note the 362-listing dataset
  this count refers to predates the 2026-08-16 data reset; the DB is
  starting fresh from the 7 pinned tour houses plus whatever the next real
  `scrape.py` run adds. Needs to be resumed/finished before the
  photo-scoring plan can meaningfully run at collection scale.
- **Photo-scoring implementation** — plan written (including the
  `layout_plan` field), not yet started; queued behind the photo backfill.
  Should be read against `docs/house-tour-calibration-findings.md` before
  implementation starts — layout/circulation and staging-detection aren't in
  the current design at all, and the findings doc explains why in concrete,
  evidenced detail rather than in the abstract.
