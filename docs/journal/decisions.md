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

## 2026-08-25 to 2026-08-27 — photo-scoring merged and run against the full live collection; three real bugs found in production

`bgiese/photo-scoring` merged to `main`. Before running it for real, estimated
cost against Ben's account balance ($4, then topped up to $23.59) — batch
pricing (50% discount) against ~144 listings' photo sets came in well under
that. Ran the full pipeline for real: `scrape.py` (fresh collection fetch +
photo backfill) then `score_photos.py` (the paid vision-scoring batch job)
against the live 144-listing collection, not a sample.

None of these three bugs were caught by code review, since none involve a
live API call during implementation — all three only surfaced through real
usage against the actual Batches API:

- **`ANTHROPIC_API_KEY` not resolved.** Bare `anthropic.Anthropic()` doesn't
  see `.env` (loaded via `dotenv_values()`, which never touches `os.environ`).
  Fixed by loading and passing the key explicitly, matching the pattern
  `assess_six_houses.py` already used.
- **`anthropic.RequestTooLargeError` (413) on submission.** One batch of 142
  listings' photos exceeded the API's 256MB per-request limit. Fixed with
  `_chunk_by_size()` — splits the submission into size-bounded sub-batches,
  each independently checkpointed to a JSON list
  (`.photo_scoring_batch_state.json`) so a crash mid-submission never
  double-submits or double-bills.
- **Round-robin processing bug.** The original loop processed batches strictly
  in submission order, so it sat blocked on whichever batch was submitted
  first even after other batches had already finished — caught via real
  numbers (80 `visual_scores` rows stuck at a stale count while Anthropic
  reported 78 succeeded). Fixed by extracting `_process_batch_results()` and
  switching to a poll-all-pending-each-cycle loop.
- **Truncated JSON responses.** `MAX_TOKENS = 1536` was too small for ~30% of
  listings' longer per-room notes, causing `stop_reason == "max_tokens"` and
  `json.loads` failures on the truncated output. Diagnosed by fetching a
  specific batch result directly and checking `stop_reason`. Fixed by raising
  to 4096, then clearing and re-running just the 44 affected rows.

Also handled, mid-run: Ben shut down the OS and resumed later, explicitly
confirmed safe first (Anthropic batches run server-side, independent of local
machine state; the checkpoint file survives a restart). Final result: 142 of
144 listings scored successfully, zero remaining truncation failures.

Once complete, `score.py` gained `write_ranked_csv()` — writes the full
ranked report (with clickable listing URLs) to `data/ranked_report.csv`,
so Ben can navigate the ranked list without going back through the terminal.

## 2026-08-27 — status/property-type filtering: an over-broad first attempt, corrected to Active + Coming Soon only

Ben flagged two data-quality issues in the ranked list: one listing was
expired with no real data left, another he suspected was a duplex. Live
investigation (DB queries, the raw collection API response, and direct
browser checks via claude-in-chrome) disproved the duplex claim for that
specific listing (Single Family, confirmed three independent ways) but fully
confirmed the expired one — Compass's own collection API already returns
`localizedStatus` ("Active", "Pending", "Closed", "Expired", "Withdrawn",
"Coming Soon", "Active / Backup", ...) and a coarse `property_type`, neither
of which `src/listing_parser.py` was extracting.

Added both fields to `Listing`, wired extraction into `listing_parser.py` and
storage into `db.py`, and reused the existing, already-hardened delisting
infrastructure (`compute_changes`/`run_delisting`/circuit breaker from the
2026-08-16 stale-listing-removal work) to treat a non-Active status as
"absent from the collection" — rather than writing new deletion logic.

First version of `is_active_status()` only accepted blank or exactly
`"Active"`. Running it live delisted 67 of 139 listings (48%) — an
implausible rate that got investigated rather than trusted: the breakdown was
`{Pending: 28, Closed: 23, Expired: 9, Withdrawn: 5, Active/Backup: 1, Coming
Soon: 1}`. Ben then explicitly clarified intent with real example URLs: for
the purpose of a home-*purchase* dataset, only "Active" and "Coming Soon"
matter — Pending/Closed/Expired/Withdrawn should all be excluded, since
they're no longer purchasable, but should keep being re-checked (not deleted
from tracking logic entirely, just excluded from this dataset — they
reappear automatically if a status ever reverts to Active).

Corrected `is_active_status()` to accept blank, any `"Active"`-prefixed
status (covers "Active" and "Active / Backup"), or exactly "Coming Soon" —
meaning only 2 of the original 67 delistings (the Coming Soon and
Active/Backup listings) were actually wrong under the clarified rule.
Restored both (3777 Shefield Drive, 8240 Garland Drive) via a fresh
`scrape.py` run. Wired into both `scrape.py` and `check.py`, exempting
pinned listings the same way delisting already does.

Deliberately held off on an actual duplex/multi-family filter — no confirmed
real example exists in the data yet (see above), so there's nothing concrete
to build a check against.

## 2026-08-28 — concurrent-session git collision, resolved via cross-session coordination

A second Claude Code session (`short-list-f2`, working on a separate
`~/code/short-list` repo) was using this same `~/code/home-search` checkout
to build a cloud-publish pipeline (`src/turso_sync.py`, `src/blob_upload.py`,
`publish.py` on branch `bgiese/publish-to-cloud`) for a planned short-list
viewer app. Two sessions sharing one non-worktree checkout means either
session's `git checkout` moves the same `HEAD` out from under the other —
`short-list-f2`'s `git checkout -b bgiese/publish-to-cloud main` did exactly
that, so this session's next commit (the status/property-type fix above,
`2bdb69c`) landed on their branch instead of `main`, sandwiched between their
first two commits and their third.

Caught it, stopped immediately (no further git mutations), and coordinated
directly with the other session via cross-session messaging rather than
guessing or unilaterally rewriting shared branch history. `short-list-f2`
confirmed ownership of its 3 (later 4) commits and consented to the fix:
cherry-picked `2bdb69c` onto `main` as `7ff5ef6`, full test suite green (197
passed), `bgiese/publish-to-cloud` left completely untouched. `main` also
picked up a small doc commit (`docs/journal/2026-08-27-photo-scoring-batch-ids.md`,
committed but missed in an earlier "commit everything" pass).

Two things noted for later, not acted on:
- `bgiese/publish-to-cloud`'s history still technically carries a duplicate of
  `2bdb69c` (harmless — git will de-dupe identical content if that branch is
  ever merged). Dropping it would mean rebasing that branch; low-risk since
  it's local/unpushed, but left as Ben's call rather than either session
  doing it unasked.
- `publish.py`/Turso/Blob sync is complete and reviewed on `short-list-f2`'s
  side, but only import-checked, never run end-to-end — no Turso or Blob
  credentials exist yet. The first real run (provisioning cloud resources,
  deploying a public site) was deliberately left to Ben rather than either
  session doing it autonomously.

## 2026-08-28 — full second pipeline pass surfaces a real leak: inactive listings could never actually stay excluded

Ran the complete pipeline end-to-end (`scrape.py` → `check.py` →
`score_photos.py` → `score.py`) as a second real pass, expecting only a
handful of new listings. `score.py` reported 148 listings scored against
`scrape.py`'s own JSON-store count of 83 — a mismatch worth chasing rather
than reporting the run as clean.

Root cause: both `scrape.py` and `check.py` had an "upsert every fetched
listing, no matter what" loop that ran *before* the Active/Coming-Soon
filter from the 2026-08-27 entry above was applied. `compute_changes()`'s
delisting logic can only remove a listing that goes from present to
absent — it has no way to catch one that shows up already inactive and was
never tracked locally before. Confirmed live: 65 non-active, non-pinned rows
were sitting in the DB, several of them the *exact* IDs `scrape.py` had just
hard-deleted moments earlier in the same run — `check.py`'s identical
upsert-everything loop, running right after, silently wrote them straight
back in since its own "before" snapshot no longer knew about them.

Fixed by computing `present_listings` (active + pinned) before the upsert
loop in both scripts and upserting only those — an inactive, non-pinned
listing now never enters the DB, whether it's brand new or was just removed.
Cleaned up the 65 leaked rows via the existing `apply_delisting()` cascade
(same reviewed removal path as regular delisting, not a raw SQL delete).
Verified the fix under real conditions, not just unit tests: re-ran
`check.py` then `scrape.py --new-listing` against the live collection — 4
genuinely new active listings picked up correctly, 2 genuinely delisted,
zero leaked rows either time. Full pipeline finished clean at 85 listings,
matching across `scrape.py`'s JSON store, the DB, and `score.py`'s count for
the first time.

## Open

- **`bgiese/publish-to-cloud` cleanup** — rebase to drop the duplicate
  `2bdb69c` commit, or leave as-is and let git de-dupe on eventual merge.
  Ben's call; not urgent, branch is local and unpushed.
- **Turso/Blob publish pipeline untested end-to-end.** `publish.py` needs real
  Turso and Vercel Blob credentials before its first live run — deliberately
  not run by either Claude session since it means provisioning cloud
  resources and deploying a public site.
- **Duplex/multi-family filter still not built** — deferred until a real
  duplex example turns up in the data; the one flagged so far turned out to
  be Single Family on investigation.
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
- **Photo backfill and photo-scoring — done.** See the 2026-08-25 to
  2026-08-27 entry above: full collection scraped and scored, 142 of 144
  listings successfully vision-scored. Note `docs/house-tour-calibration-findings.md`'s
  layout/circulation and staging-detection findings were never folded into
  the shipped rubric — still an open gap in what gets scored, just not an
  incomplete-run problem anymore.

## 2026-09-03 — Phase 3 gates: two pass cleanly, one is blocked, one needs two weeks

Ran the gates from issue #24 against a real Vercel Sandbox in `iad1`
(egress `54.162.144.92`, an AWS datacenter IP) and, as a control, from the
desktop's residential IP (`24.9.167.51`). Same script both places, so the
only variable is the egress IP. Scripts kept in `ops/spikes/`.

**Total cost: 36.4s active CPU, 229.8s wall, ≈ $0.007.** The 2026-08-30
spike cost $0.03–0.05; this was cheaper because nothing needed reinstalling
after the measurements were taken.

### Gate 2 — Nominatim/OSRM from a cloud IP: PASS

Twelve geocodes at the documented 1 req/sec, with the pipeline's real
User-Agent (`home-search/1.0 (bengiese22@gmail.com)`): **12/12 resolved,
all HTTP 200, zero 429 or 403.** OSRM routing returned `Ok`, 11.5 mi /
18.1 min.

The stronger evidence is the control: the datacenter run returned
coordinates **identical to the residential run, digit for digit**, and the
one address that failed to geocode failed from *both* IPs — it is an
address Nominatim genuinely does not know, not throttling. That also
explains part of the 12 `geocode_failed` rows currently in Turso. No
evidence of the per-IP penalty the gate was written to look for.

### Gate 3 — Compass photo CDN from a cloud IP: PASS

Both sampled photos returned HTTP 200, `image/jpeg`, valid JPEG magic
bytes, and **byte-identical sizes from both IPs** (359,448 and 328,369).

Worth recording because it changes an assumption: the plan expected photo
downloads to need the authenticated Playwright page context, which is why
it thought they would "probably" pass. They do not — `www.compass.com/m/...`
serves **unauthenticated**. That makes this gate a much weaker risk than it
looked, and it means a cloud runner does not need a Compass session merely
to fetch images.

### Gate 4 — full supervised run against a throwaway Turso DB: BLOCKED

Not attempted. Creating a throwaway database needs a Turso **platform** API
token; `.env` holds only `TURSO_DATABASE_URL` and `TURSO_AUTH_TOKEN`, which
authenticate against the existing database and cannot create another. The
gate deliberately says "throwaway", so running the pipeline against the
real database from an unproven environment would defeat its purpose. Needs
a `turso auth` login or a platform token before it can run.

### Gate 1 — reCAPTCHA durability: cannot be cleared in one session

Two weeks of scheduled runs by construction. One same-day data point: from
the datacenter IP, Chromium loaded `https://www.compass.com/login/` with
HTTP 200 and no interactive challenge, consistent with 2026-08-30. That
confirms the earlier result still holds; it says nothing about durability,
which is the entire question.

### Incidental findings that change the plan

- **Chromium install takes 25s, not the 2–3 min the plan assumed** — a 6x
  improvement, on `vercel/sandbox/universal` with Python 3.14.4. This
  materially weakens the argument for snapshot management.
- **Sandbox persistence is now the default** ("no manual snapshot management
  needed"). Phase 3 step 3's snapshot-expiry strategy, and its cold-install
  fallback, are probably obsolete — a stopped sandbox snapshots itself and
  `--keep-last-snapshots` handles retention. Re-read
  `/docs/sandbox/concepts/persistent-sandboxes` before building it.
- **There is a Python Sandbox SDK** (`vercel.sandbox` in the `vercel`
  package). The plan assumed the JS SDK because "Workflow DevKit is
  TypeScript-only"; that reasoning does not apply to Sandbox, so the runner
  could be Python.
- **Vercel CLI 59.5.0 cannot talk to Sandbox** — `/v3/sandboxes` returns
  `{"error":{"code":"forbidden","invalidToken":true}}` while `vercel whoami`
  and `projects ls` work fine on the same token. 59.11.2 works. Use
  `npx vercel@latest` rather than debugging the token.
- **`turso.io` has no A record.** A reachability check against the apex
  domain fails everywhere, not just in a sandbox; the real DB host
  (`aws-us-east-1.turso.io`) resolves fine from `iad1`. Noted because it
  looked briefly like an egress block and is an easy trap to fall into
  twice.

### Recommendation: partial go

The two gates that could actually fail did not, and both failed *softer*
than expected — Nominatim shows no per-IP penalty at this volume, and the
photo CDN needs no session at all. Neither is a reason to hold Phase 3.

What still gates it is unchanged and unhurried: **the two-week reCAPTCHA
canary is the real decision**, and gate 4 needs a throwaway database.
Nothing here argues for building the cron and the runner before the canary
has run, because the canary is the cheap way to learn the one thing that
would make the whole phase pointless.

## 2026-09-03 — Favorites were never being scraped

Ben asked whether the `/favorites` tab was being picked up alongside
`/matches`. It was not, and had never been.

Compass serves every tab of a collection — `/matches`, `/favorites`,
`/notInterested` — from one collection ID. The tab is chosen by a
`listingsFilter` integer inside the API query, not by the URL path.
`fetch_collection_listings` hardcoded `listingsFilter: 0`, and
`extract_collection_id` kept only the ID and threw the path away. So
`COMPASS_COLLECTION_URL` could be pointed at `/favorites` and would keep
returning matches, silently. That is the part worth remembering: the config
looked like it worked.

Verified live by intercepting the SPA's own requests:

| tab | listingsFilter | reviewStage | count |
|---|---|---|---|
| matches | 0 | 0 | 149 |
| favorites | 1 | 2 | 26 |
| (unnamed) | 2 | 3 | 22 |
| notInterested | 3 | 1 | 277 |

26 favorites were missing; 11 still active, 10 of those absent from the DB
entirely and never scored or published.

### Decisions

**The URL is validated, not obeyed.** Tabs come from `COLLECTION_TABS` in
code, defaulting to `favorites,matches`, overridable with
`COMPASS_COLLECTION_TABS`. The URL's tab segment is checked and a wrong one
raises. Deriving the filter from the path was rejected: the requirement is a
*set* of tabs against one ID, and an untouched `/matches` `.env` has to start
fetching favorites — which already contradicts literal URL semantics. Naming
`notInterested` anywhere raises rather than falling back to matches; silently
fetching something else is the exact bug being removed.

**Favorites outranks matches on dedup.** Only Ben or Megan move a listing
into favorites, and only they move it back out; matches is just "the saved
search matched it". Config orders tabs by `TAB_PRECEDENCE` before fetching,
so first-wins dedup keeps the favorites copy however the tabs were typed.

**Delisting trusts a fetch only when every tab succeeded.** `any()` is
provably unsafe: favorites failing while matches succeeds makes all 10
favorites-only listings look delisted, and 10/159 = 6% sails under
`MAX_DELISTED_FRACTION` — wiping the bucket, then re-adding them next run
with photos re-downloaded and scores lost. The reverse case only survives by
the accident that 139/159 trips the fraction. A tab returning zero listings
counts as failed for the same reason: an empty 200 OK is invisible to a
global fraction when the empty tab is the small one.

**`check.py` shipped in the same change.** It runs the same delisting
cascade off its own fetch. Teaching only `scrape.py` about favorites would
have left `check.py` deleting all 10 on its next run — 6%, under the
breaker. `backfill_photos.py` fetches too and moved over with it.

### Residual gap

A tab returning *anomalously few but non-zero* results (2 of 11 favorites) is
still under-detected by the global fraction. A real per-tab denominator needs
the source tab persisted per listing, which is a schema change against Turso.
Recorded in `backlog.md` rather than built.

## 2026-09-03 — A favorite that goes Pending is no longer deleted

Follow-up to the favorites work above, and to issue #50.

`is_active_status` excludes Pending, which Ben confirmed on 2026-08-27 after
seeing a mixed batch of statuses. That call still stands for listings in
general. It was made before favorites were fetched at all, though, and a
favorite is a different kind of object: only Ben or Megan put a listing
there, and only they take it out. Until now it was the strongest interest
signal in the system and the least protected — pins were exempt from status,
favorites were not — so a favorite going under contract was hard-deleted
along with its photos and its paid vision scoring.

**Decision: Pending favorites are exempt. Closed, Expired and Withdrawn
favorites are not.** Pending deals fall through and return to Active, so
deleting one throws away work that will be needed again shortly. A Closed or
Expired favorite is genuinely gone, and exempting those too would accumulate
dead listings forever. Ben's call, given the options.

Scoped deliberately: `is_active_status` is untouched, and the exemption lives
in a new `select_present_listings` alongside a narrow `is_pending_status`.
Folding Pending into `is_active_status` would have silently reverted the
2026-08-27 decision for every listing.

`select_present_listings` is one function used by both `scrape.py` and
`check.py` rather than the same comprehension written twice. Both delist off
this decision, and a listing `check.py` considers absent is hard-deleted —
that exact drift is what made favorites deletable by `check.py` before they
were ever fetched.

`CollectionFetch` now carries `tab_ids`, so the fetch layer knows which
listings are favorites. When the favorites tab fails, `favorite_ids` is empty
rather than wrong — and that run is already untrustworthy for delisting via
`collection_fetch_is_trustworthy`, so a failed tab cannot turn a favorite
back into a deletable match.

Live effect at the time of the change: 5 Pending favorites that had already
been dropped come back (6085 West 82nd Drive, 12306 Deerfield Way, 3331 West
10th Ave Place, 950 Laurel Street, 9862 Independence Street), and nothing is
delisted. Two others that looked at risk (2765 Canossa Drive, 10538 Kipling
Place) turned out to be pinned already.

## 2026-09-04 — The sandbox runner reports by leaving files on disk

Phase 3 gives the pipeline a second execution home: a Vercel Sandbox started
by a cron in short-list. The obvious design has the runner tell Vercel when
it is finished. It cannot, and working out why shaped most of the rest.

Stopping a sandbox, or calling anything else on the Vercel API, needs a
Vercel credential **inside** the VM. Both available kinds are wrong. An OIDC
token has a 2-hour TTL and is reused for up to 90 minutes before refresh,
against a pipeline whose photo-scoring stage legitimately polls a vision
batch for hours — it expires mid-run, unrefreshably, because the refresh
happens in the function that is long gone. A personal access token is
team-wide, which is the worst blast radius available for a VM driving
Chromium against a third-party site.

**So the runner holds no Vercel credential at all**, and communicates the
only way it can: `data/.run/started` and `data/.run/done` on its own disk,
read from outside by the launcher and the reaper. `started` without `done`
is the entire in-progress signal. That makes the write ordering load-bearing
in a way a status callback would not have been — the previous run's `done`
must be cleared *before* the new `started` appears, or both readers see a
run as finished the moment it begins — and it is why every marker is renamed
into place rather than written, since the VM can be killed mid-write.

The consequence is that **only the reaper can stop a sandbox**, and it runs
every ten minutes. That is not tidiness. Vercel bills provisioned memory for
the whole session rather than for CPU used, so a 20-minute run left idling
to its 3-hour limit wastes about $0.23, and four of those a day exceeds the
entire Pro credit. The reaper collects the refreshed Compass session before
stopping — once stopped the filesystem is a snapshot, and those cookies are
only reachable by resuming it — but a failure to collect never prevents the
stop. A stale session costs a cold login; an unstopped sandbox costs money.

### Zero listings is a failure, not an empty collection

`ops/canary.py` runs nightly, read-only, half an hour before anything else.
It exists because the way this pipeline breaks is silent: a rotated session,
a changed selector and a WAF block against a datacenter IP all produce a
clean fetch of *nothing*, and downstream an empty tab is exactly what makes
the delisting cascade consider deleting everything it covered. So the canary
fails on a zero count rather than treating it as an empty collection, and
does it a day before the pipeline would have.

It records the egress IP because that is the one variable the sandbox
introduces — every scrape until now came from a residential address — and it
records whether the session was warm, without failing on a cold login. Warm
first is the design; a cold login that works still answers the question.

### Notification is for the two things nothing else would surface

`src/notify.py` had existed since #55 with nothing calling it. It now fires
on a failed stage (named, because which stage is the whole diagnostic) and
on losing the cross-home lease mid-run. The second is the one condition that
can cost real money and leaves no non-zero exit code behind: two homes
writing the same database means a concurrent scrape re-downloading photos
Compass already rate-limits.

Successes stay silent. A nightly notification that everything is fine is one
people learn to swipe away, and by the time one matters they no longer read
it. The canary is the liveness proof; ntfy is for exceptions.

## 2026-09-05 — The cloud cutover, and what it cost to trust it

The pipeline runs itself now. Three crons in short-list drive a Vercel
Sandbox: the pipeline every six hours, a read-only canary nightly, and a
reaper every ten minutes that is the only thing able to stop a sandbox.
Verified end to end in production — launcher 202, bootstrap, a warm Compass
session seeded from private Blob, scrape, 37 photos downloaded and uploaded
to Blob from a datacenter IP, commutes, scoring, `verify`, revalidate,
`exit_code: 0`, then `collect-and-stop`.

Two facts about Compass came out of it. A cold login from a Vercel IP
succeeded, which was the most likely way the whole phase could have failed
outright. And the collection returns identical counts to a residential
address — 27 favorites, 153 matches — so Compass serves a datacenter IP the
same data. Neither settles the durability question; that still needs the
scheduled logins to run for two weeks.

The desktop path was deliberately abandoned rather than kept as a fallback.
The systemd timer was never installed, which is why every run for weeks was
triggered by hand and why the corpus had drifted: five Active listings had
photos but no commutes and no vision scores, and were ranked on a fabricated
`commute_score = 50`. Fixing them moved one composite from 57.0 to 68.3, so
the ordering the viewer showed was wrong rather than merely incomplete.

### What the cutover actually taught

Nine defects, none caught by the test suite. Six came from trusting a type
signature or a remembered API instead of reading what the SDK does. The
important thing is not the count but the shape: **HTTP 200, valid JSON, no
exception, and the wrong answer.** A reaper that resumed and re-stopped an
idle sandbox 144 times a day looked healthy on every surface Vercel offers.

That is written up separately in
`2026-09-05-observability-and-silent-wrongness.md`, along with what Vercel
can and cannot detect and the eight platform gaps worth not re-researching.
The short version is that the fix was never a dashboard: it was to stop
returning success when the system is wrong. `verify.py` now asserts four
invariants as the last pipeline stage and fails the run on violation, the
reaper states its invariant and throws rather than assuming it, and both
cron routes emit their decision as a queryable metric so a wrong pattern is
a shape on a chart rather than something a person has to notice.

### One correction to an earlier entry

The local execution home is a **desktop**, not a laptop. The cross-home lock
is a lease partly because a runner can vanish without releasing it — but the
reason is a machine powered off or booted into its other OS, not a closed
lid. The conclusion survives; the stated reason was not the true one.

## 2026-09-05 — Commute: a real rush-hour number, and the terms we are breaking

The commute figure was free-flow OSRM: no traffic, consistently ~18 minutes
where the hand-checked answer is ~25. Issue #72 asked for something better.

The finding that reframed it: the factor was not merely wrong, it was
**inert**. `_medtronic_leg_score` uses absolute thresholds (≤20 → 100), and
against free-flow durations 70 of 92 listings scored an identical 100. The
heaviest-weighted input in the rubric, at 28.65%, was a near-constant. The
thresholds were never wrong — `decisions.md` (2026-08-14) records they were
curved around Megan's stated 20-minute-ideal and 30-minute-ceiling, which
describe a *lived* commute. They had been fed the wrong quantity for a year.

Measured against four real listings at 08:00/08:15/08:30 arrival, Mapbox and
TomTom agree within a minute on every one:

```
                    free-flow   Mapbox   TomTom   ratio
145 Caria Drive           5.2      6.3      6.2    1.22
5012 West 77th Drive     14.8     17.7     17.4    1.20
7263 Deframe Court       23.4     23.8     24.5    1.02
12665 West 67th Place    25.5     26.3     27.3    1.03
```

That killed the cheapest option. A uniform correction factor looked viable
on published Travel Time Index data, but the measured spread is 1.02–1.22
and runs *opposite* to intuition: short arterial routes congest, long ones
on CO-121/CO-72/Northwest Parkway barely do. No single multiplier works, and
correcting it by hand would mean maintaining a road classification the
providers already model.

It also settled a disagreement between two research passes about whether
Mapbox's `arrive_by` is genuinely traffic-aware. It is — durations differ
from an untimed request and shift with arrival time.

**Provider: Mapbox, knowingly against its terms.** Every commercial vendor
prohibits storing results and automated bulk querying; Google additionally
cannot answer arrive-by for driving at all. The reasoning, the exact clauses
from Mapbox and TomTom, and the mitigations are in
`docs/routing-provider-terms.md`. The short version: the prohibition is
universal so it is not a differentiator, Mapbox enforces by quota rather
than audit, and the real risk is a revoked key degrading the corpus silently
— which is a design problem, not a legal one.

### Two defects found while measuring

**The card showed the wrong destination.** Every list card rendered
`denver_minutes` labelled "min commute" while `commute_score` and "Sort:
Commute" used the Lafayette leg. Fixed in short-list#17.

**Two properties were in the corpus twice.** A relist arrives as a new
Compass listing id, and nothing recognised it — so one house was scored,
ranked, and vision-scored twice. Both stored URLs 301-redirect to a single
canonical page, which revealed the underlying structure: Compass keeps a
stable **property id** (`_pid`) and disposable **listing ids** (`_lid`). The
`_pid` is the authoritative dedupe key; address+city is the free proxy we
use, since reading `_pid` costs a request per listing. `scrape.py` now
supersedes automatically, `verify.py` asserts uniqueness, and
`ops/dedupe_relists.py` cleared the two that predated both.

That episode also produced a rule worth keeping: **only the layer that
reclaims blobs may delete a listing.** `delete_listing` prunes the
`hosted_photos` rows, and `blob_url` is the only record an image was
uploaded — so deleting rows first destroys the handle rather than tidying
up. That is how 1,813 orphans accumulated once. It is now enforced by a test
rather than remembered, and the first real use of the guard caught me
reaching for the wrong function.
