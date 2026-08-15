# home-search: v2 "photo scoring" design

## Context

v1 baseline scoring (spec: `2026-08-13-baseline-scoring-design.md`) is mid-implementation
via subagent-driven development, on branch `bgiese/baseline-scoring`. It scores listings on
commute, sqft, condition, outdoor/hosting, and parking &mdash; but condition and
outdoor/hosting rely on weak text-keyword scans (`RENOVATION_KEYWORDS`,
`OUTDOOR_KEYWORDS`), explicitly flagged in that spec as placeholders for photo-based
scoring once it exists.

Listing photos are already being downloaded to `data/photos/<listing_id>/NN.jpg` via
the existing `download_photos()` pipeline. This spec covers **v2: photo scoring** &mdash;
looking at those photos with a vision-capable Claude model and using the result to
replace the two placeholders, without touching the rest of the v1 composite.

## Goals

- Score each listing's condition and outdoor/hosting appeal from its actual photos,
  replacing the keyword-only placeholders in the same weight slots (condition 20%,
  outdoor 15%) &mdash; no change to `WEIGHT_*` constants or the other three factors.
- One Claude request per listing, all of its photos (typically 15&ndash;25) batched
  together, with structured JSON output so results are typed, not text to parse.
- Run the whole pass through the Message Batches API: this is a one-time-ish job across
  ~114 listings, not a live workload, and the 50% discount applies cleanly.
- Handle two distinct kinds of missing photos differently: a listing that omits a room
  category despite having plenty of other photos is a real signal and scores low; a
  listing that simply doesn't have enough photos yet (still being staged) is excluded
  from photo scoring rather than penalized for it.
- Fold "nearby bike trails/parks" into the existing outdoor keyword scan as a cheap v1
  addition &mdash; no new weighted category.

## Non-goals (v2)

- No new weighted category and no reweighting. Trails/parks joins the existing outdoor
  slot's keyword list.
- No geocoded or Claude-API-prompt-based trails/parks proximity check yet (tracked in
  `docs/journal/backlog.md` for v3).
- No live or interactive scoring. This is a manually-run batch script, not part of
  `scrape.py`'s per-run flow.
- No use of the six-house tour feedback yet. Ben is writing it up separately; it will
  calibrate rubric wording once it exists, not before.

## Architecture

```
listings + downloaded photos (data/photos/<listing_id>/*.jpg)
      +
photo scoring (new) → visual_scores table
      ↓
scoring.py (v1) — score_condition()/score_outdoor() read visual_scores when present,
                   fall back to the v1 keyword computation otherwise
```

### `src/vision.py` (new)

Pure functions, no network calls &mdash; same split as `src/commute.py`: the real
Anthropic API call is a thin wrapper in the orchestration script; the logic worth
testing is what happens with its result.

- `has_enough_photos(photo_count: int) -> bool` &mdash; checks against
  `MIN_PHOTOS_FOR_VISION_SCORING = 5`. A listing below this floor is excluded from
  vision scoring entirely rather than scored low; a handful of photos usually means the
  listing is still being populated, not that it's a bad property.
- `parse_visual_response(response_json: dict) -> VisualScoreResult` &mdash; converts the
  model's structured JSON into two 0&ndash;100 sub-scores. Response shape per listing:

  ```json
  {
    "kitchen": {"present": true, "renovation_level": 7},
    "interior_condition": {"present": true, "score": 6},
    "backyard": {"present": true, "tree_coverage": 8, "hosting_suitability": 6}
  }
  ```

  `condition_photo_score` averages `kitchen.renovation_level` and
  `interior_condition.score` (each &times;10); `outdoor_photo_score` averages
  `backyard.tree_coverage` and `backyard.hosting_suitability` (each &times;10). Any
  `present: false` sub-attribute scores low (20/100) rather than being dropped from the
  average &mdash; an omitted room in an otherwise well-photographed listing is a
  concerning signal, per Ben's read on it, not a neutral gap.

### `visual_scores` table (new, in `src/db.py`)

`listing_id, condition_photo_score, outdoor_photo_score, photo_score_unavailable,
raw_response, computed_at`. `photo_score_unavailable` covers both exclusion paths
(too few photos, and a failed/errored API call for an eligible listing) with one flag,
since `scoring.py`'s fallback behavior is identical either way: use the v1 keyword
score. `raw_response` keeps the model's JSON for debugging, nullable when unavailable.

### `score_photos.py` (new, top-level orchestration script)

Mirrors `compute_commutes.py`'s shape:

1. For every listing with `photo_count >= MIN_PHOTOS_FOR_VISION_SCORING` and no existing
   `visual_scores` row, build one Batch API request (`custom_id = listing_id`): all of
   that listing's photos as image content blocks, a fixed instruction, and
   `output_config.format` pinned to the schema above.
2. For listings below the photo floor, write a `visual_scores` row directly with
   `photo_score_unavailable = true` and null scores &mdash; no API call.
3. Submit the batch, poll until it ends, and upsert each result. A per-item API error
   or malformed response also gets `photo_score_unavailable = true` rather than aborting
   the batch.

### Integration into `scoring.py` (small follow-up edit to the v1 module)

`score_condition()` and `score_outdoor()` gain an optional visual-score argument. When
present, it replaces the keyword-hit component (condition keeps `year_built` as its
secondary signal; outdoor is fully replaced). When absent &mdash; no photos downloaded
yet, or `photo_score_unavailable` &mdash; both functions fall back to their existing v1
keyword-only computation unchanged.

## Config

Requires `ANTHROPIC_API_KEY` (or an `ant auth login` profile) &mdash; the first time this
project calls the Claude API directly, separate from Ben's claude.ai chat subscription.
Setting up billing at console.anthropic.com is a manual step for Ben before
`score_photos.py` can run for real; the rest of this design (schema, `src/vision.py`'s
pure functions) builds and tests without it, same as `src/commute.py` did.

## Error handling

- Zero photos: same path as below-floor (0 &lt; 5) &mdash; `photo_score_unavailable`.
- API error, refusal, or malformed response for one listing: log, mark that listing's
  row `photo_score_unavailable`, continue the batch.

## Testing

`has_enough_photos()` and `parse_visual_response()` get unit tests against fixture JSON
&mdash; boundary cases at the 5-photo floor, `present: false` handling, missing keys.
No live API calls in tests, matching `src/commute.py`'s precedent. `score_photos.py`'s
Batch API submission and polling is untested orchestration, matching
`compute_commutes.py` and `score.py`.

## Cost

Ballpark: ~114 listings &times; ~20 photos each through Claude Sonnet 5 with the Batch
API discount lands around $5&ndash;10 total. Worth confirming with `count_tokens` against
a handful of real listings once the schema and photo sizing are final, rather than
committing to the estimate.

## Open questions for v3

- Real geocoded or Claude-API-prompt-based trails/parks proximity, replacing the v2
  keyword scan (`docs/journal/backlog.md`).
- Recalibrate rubric wording once the six-house tour feedback is written up.
- Revisit `MIN_PHOTOS_FOR_VISION_SCORING = 5` once the real photo-count distribution
  across the 114 listings is visible.
- Downsample photos before upload if real token cost runs higher than the estimate.
