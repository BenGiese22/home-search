# Backlog

Raw, ungroomed ideas that haven't been formalized into a spec yet. Items
graduate out of here (and get deleted from this file) once they're written
up properly under `docs/superpowers/specs/`.

## 2026-08-24 — vertical circulation/split-level flow is invisible to photo-only scoring

`assess_six_houses.py`'s Claude-vs-Ben comparison: on House 2 (4552 W 111th Ave)
and House 3 (8221 W 93rd Way), Claude's photo-only assessment said YES on both;
Ben and Megan's actual in-person verdict was NO on both, for the same reason each
time — a disjointed multi-half-floor split-level layout ("spiral staircase maze
of 4 half floors" / "immediately throw you into a split level stairway"), not
condition. The planned `layout_plan` rubric field (design spec, `src/vision.py`)
only detects whether a floor-plan graphic photo exists — it doesn't and can't
judge whether a home's actual circulation feels confusing. Two of three real "no"
verdicts so far hinged entirely on this, so it's a real blind spot worth knowing
about, not something to solve now.

**Update 2026-08-25:** confirmed at the full 7-house dataset, not just the
first 3 — see `docs/house-tour-calibration-findings.md` for the complete
comparison (human verdict vs. Claude's photo-only read vs. the current v1
algorithm) plus several more findings from the same exercise: staging (real
and virtual) actively fooling a photo-only read, bed/bath count as an unused
signal distinct from sqft, and concrete verified false-negatives in both
keyword lists against real listing descriptions.

## 2026-08-15 — nearby bike trails / parks as a scoring factor

Ben flagged proximity to bike trails and/or parks as worth assessing
somewhere in the listing scoring — currently not covered by any factor in
the v1 baseline-scoring rubric.

Plan for now: fold it into the photo-scoring v1 design as a cheap
text-keyword scan over the listing description/amenities (same lightweight
approach as the existing outdoor/hosting placeholder — phrases like
"trail," "park," "greenbelt," etc.).

Ben wants something more robust eventually, and named two candidate
directions for a v2 pass, undecided between them:

- **Geocoded proximity check** — look up actual nearby parks/trails via a
  real geo data source and compute distance, the same way the commute
  factor already does real routing via OSRM/Nominatim instead of guessing.
  Would need a parks/trails dataset or API to be identified (not yet
  researched).
- **Claude-API-prompt-based check** — ask a Claude model directly about
  what's nearby a given address, similar to the *rejected* original
  approach for commute distance. Worth remembering that the commute
  feature specifically moved *away* from LLM-prompted distance guessing
  because it was measurably less accurate than real routing data for a
  less-documented destination — the same caution would apply here before
  trusting an LLM's geographic knowledge over ground truth.

## 2026-08-15 — baseline-scoring calibration follow-ups (from the final whole-branch review)

The final review of `bgiese/baseline-scoring` found these worth doing, but
explicitly not merge-blocking — parked rather than fixed, since the one
required fix (the incomplete-data flag) was scoped tightly on purpose. Real
data numbers below are all against the live 362-listing collection.

- **Neutral-50 commute imputation functions as a penalty, not neutral.**
  Spec's stated intent was "one missing field shouldn't tank a composite
  score," but on real data the 319 successfully-routed listings score
  62.7–99.9 (median 90.0) on the commute sub-score — nothing routed scores
  below 50. So the 43 geocode-failed listings (now visibly flagged via
  `has_incomplete_data`) are still silently ranked beneath every routed
  listing on the heaviest-weighted factor (35%). Consider imputing the
  routed-collection's median instead of a flat 50.
- **No retry path for failed geocodes.** `get_listing_ids_missing_commute`
  keys on row *absence*, but a failed geocode still writes a row — so the
  43 current failures are cached permanently; the only recovery today is a
  manual `DELETE FROM commute WHERE geocode_failed=1`. Spot-checked 3 of the
  43 live and they're genuine Nominatim misses, not transient errors, so a
  naive retry wouldn't help today — but there's no supported path to
  re-attempt later as OSM coverage improves. Needs either a `--retry-failed`
  flag on `compute_commutes.py` or excluding `geocode_failed=1` rows from
  "already covered."
- **`score_condition` floors a keyword-miss at 0; `score_outdoor` floors the
  same shape of problem at 40** with an explicit "absence isn't proof of
  absence" rationale that logically applies equally to condition. Real
  impact is large: only 6 of 362 listings match any renovation keyword, so
  356 of 362 condition scores are compressed into `[0, 20]` — a factor
  nominally weighted 20% contributes at most ~4 composite points for 98% of
  the collection. Not a weight-retuning fix; needs a deliberate decision
  since it changes real rankings.
- **`YEAR_BUILT_MIN`/`MAX` (1955–2005) is hardcoded** while sqft and commute
  both normalize against real `CollectionStats` computed from the live data.
  Real collection spans 1932–2026, so 32/362 listings saturate at the 0 or
  100 end. Consider folding year-built range into `CollectionStats` like the
  other two.
- **`score_parking(0) == 0.0` contradicts this project's own stated global
  rule** ("missing input stat → neutral 50, never 0") — though it matches
  the design spec's own parking table, which intentionally says 0. The two
  project documents disagree with each other; worth reconciling explicitly
  rather than leaving the contradiction standing. Affects one real listing
  (8177 Ames Way — likely an unreported garage, not a parking-less home).
- **`geocode_failed` is a misleading name** — `compute_commute` also sets it
  `True` when *routing* fails with a perfectly good geocode, so a row can
  have valid lat/lon with `geocode_failed=1`. Didn't occur on real data (all
  43 failures have NULL lat), but `commute_failed` would be an honest
  rename.
- **`get_scores()` (src/db.py) is unused** — `score.py` re-sorts in Python
  instead of using it. Either wire it in or remove it.
- **`score.py` does 362×2 individual queries** (`get_commute` +
  `get_amenities` per listing) rather than a joined batch query — an N+1
  pattern, irrelevant at 362 rows, worth revisiting only if the collection
  grows substantially.

## 2026-08-16 — stale-listing removal: known limitations accepted at merge

Two findings from code review on `bgiese/stale-listing-removal` were
deliberately left unfixed rather than deferred by accident — recording the
reasoning so it isn't re-litigated from scratch later.

- **`apply_delisting()` deletes the DB row before the on-disk photos and
  JSON file.** If the process is killed between those calls (Ctrl-C, OOM,
  crash), the DB row is gone but the photo directory and JSON file are
  orphaned on disk forever — nothing will ever revisit them, since the
  listing_id can't reappear in a future `before` snapshot once its row is
  gone. Accepted as a low-probability edge case for a personal,
  manually-invoked tool rather than solved now. If it becomes a real
  problem, worth either reordering (delete disk state first, so a crash
  leaves an orphaned *DB row* instead of orphaned files — a DB row is at
  least discoverable via a query) or a durable pending-deletion log.
- **Each delisted listing gets its own DB transaction** (`delete_listing`'s
  own `with conn:`) rather than the whole batch being one transaction.
  Considered and *not* changed: a single all-or-nothing transaction across
  every delisted listing would mean a mid-run crash loses ALL progress
  instead of just the remainder, which is a worse resilience story than
  what exists today — the current per-listing commits mean an interrupted
  run has still durably cleaned up whatever it got through, consistent
  with this codebase's existing partial-progress-preserved philosophy
  everywhere else (scrape.py, check.py, compute_commutes.py). Performance
  is a non-issue at this data scale either way.

## 2026-08-16 — stale-listing removal: round-5 review findings, not live risks

A fifth review round on `bgiese/stale-listing-removal` found three more real
gaps, but — unlike every finding in rounds 1-4 — none of these are live
data-loss risks against this repo's actual current state. Deliberately
parked rather than fixed, to avoid expanding scope further and risking the
kind of complexity-induced bug the last four rounds kept catching in each
other's fixes.

- **No un-pin mechanism.** Once `is_pinned=1` is set for a listing_id,
  nothing ever sets it back to 0 — a listing removed from `LISTING_URLS`
  stays permanently exempt from delisting. This is an over-protection gap
  (something lingers that should eventually be cleaned up), not an
  under-protection one (nothing gets wrongly deleted), so it works against
  "match the portal count in near-real-time" but never causes data loss. A
  real fix was sketched (compare `get_pinned_listing_ids()` at the start of
  a `scrape.py` run against listing_ids still derivable from the *current*
  `config.listing_urls`, un-pinning anything that dropped out) but needs
  its own safety guard — an accidentally-empty `LISTING_URLS` must not be
  read as "unpin everything," the same class of "empty result treated as
  ground truth" risk the delisting circuit breaker exists to prevent.
  Worth building carefully, as its own reviewed change, not bolted on here.
- **`scrape.py` holds the authenticated Playwright session open through the
  entire delisting cascade** (DB deletes, `shutil.rmtree` calls, file
  unlinks) even though the browser page isn't used again after the
  collection fetch — `check.py` already closes its session first, so this
  is an unintentional asymmetry. Pure resource-efficiency nit, no
  correctness impact; low priority.
- **`derive_pinned_ids_from_urls()`'s regex-based backstop only covers
  numeric-ID URLs** (`/listing/<id>/view`), not the
  `/homedetails/<address-slug>/` format (which this project's own tests
  already document as returning no ID). Both of this repo's actual current
  `LISTING_URLS` entries are the numeric-ID format, so the backstop fully
  covers today's real data — this is a real gap only for a URL format not
  currently in use here. If an address-slug URL is ever added to
  `LISTING_URLS`, the authoritative `is_pinned` DB flag still protects it
  correctly after the next `scrape.py` run; only the narrow
  post-migration/pre-rerun window would be uncovered for that specific
  listing.

## Per-tab delisting denominator

`should_apply_delisting` measures delistings as a fraction of everything
tracked. With favorites (~26) and matches (~149) merged, a favorites tab
returning anomalously few but non-zero results — 2 of 11 — is under the
global threshold and would delist the rest of the bucket. The zero-result
gate in `collection_fetch_is_trustworthy` catches only the empty case.

A real fix needs last run's per-tab id sets to give each tab its own
denominator: either a `source_tabs` column on `listings` (schema change,
Turso mirror included) or a small JSON sidecar in `data/`. Not built —
the empty-tab case is the one actually observed in the wild.
