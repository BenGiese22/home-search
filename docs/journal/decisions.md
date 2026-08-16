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

---

## Open

- **Six-house tour feedback not yet written down.** Ben and Megan toured
  six homes before this project started — Canossa Dr. was a "yes," five
  others were "no" — but the detailed reasoning behind each verdict has
  never been captured in writing. This is exactly the calibration data the
  photo-scoring rubric (and eventually the stats rubric's weights) should
  be checked against. Ben has said he'll write it up separately when he has
  time, rather than dictating it in conversation.
- **Photo backfill incomplete.** Only 61 of 362 listings have any downloaded
  photos (2,133 photo files total). Needs to be resumed/finished before the
  photo-scoring plan can meaningfully run — most listings would currently
  fail the plan's 5-photo floor purely because photos were never fetched,
  not because anything's wrong with them.
- **Stale-listing removal** — design finalized (see entry above), not yet
  implemented.
- **Photo-scoring implementation** — plan written, not yet started; queued
  behind the photo backfill and the stale-listing removal work.
