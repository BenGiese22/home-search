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
- Score condition room by room, not as one blob. Kitchen, bathrooms, living space,
  basement, and garage each get their own visual assessment, weighted by how much they
  actually matter to the value and feel of a home &mdash; kitchen and bathrooms count
  most.
- One Claude request per listing, all of its photos (typically 15&ndash;25) batched
  together, with structured JSON output so results are typed, not text to parse.
- Run the whole pass through the Message Batches API: this is a one-time-ish job across
  ~114 listings, not a live workload, and the 50% discount applies cleanly.
- Make the scoring script safe to re-run on a schedule (e.g. a daily cron job): it
  should detect which listings already have a score and only spend API calls on
  listings that don't.
- Handle two distinct kinds of missing photos differently: a listing that omits a room
  category despite having plenty of other photos is a real signal and scores low; a
  listing that simply doesn't have enough photos yet (still being staged) is excluded
  from photo scoring rather than penalized for it.
- Fold "nearby bike trails/parks" into the existing outdoor keyword scan as a cheap v1
  addition &mdash; no new weighted category.
- Detect and assess any floor-plan/layout graphic among a listing's photos (some
  listings include one), but exclude it from the composite score entirely &mdash; it's
  informational, not a signal about the home's condition or outdoor appeal.

## Non-goals (v2)

- No new top-level weighted category and no reweighting of `WEIGHT_*`. Trails/parks
  joins the existing outdoor slot's keyword list; room-level condition detail stays
  inside the existing condition slot; `layout_plan` is captured but never scored at
  all &mdash; it doesn't join any existing slot either.
- No geocoded or Claude-API-prompt-based trails/parks proximity check yet (tracked in
  `docs/journal/backlog.md` for v3).
- No OS-level scheduling. The script is built to be safe to invoke repeatedly (see
  Goals), matching the change-detection feature's precedent, but setting up the actual
  cron job or launchd task is a manual step for Ben, not part of this spec.
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
- `parse_visual_response(response_json: dict, garage_expected: bool) -> VisualScoreResult`
  &mdash; converts the model's structured JSON into two 0&ndash;100 sub-scores.
  `garage_expected` comes from the listing's own `parking_spaces` field (structured
  data, not the model's guess): a listing with `parking_spaces == 0` has no garage to
  score, so garage is excluded from the average rather than penalized as omitted.

  Response shape per listing:

  ```json
  {
    "kitchen": {"status": "present", "score": 7},
    "bathrooms": {"status": "present", "score": 6},
    "living_space": {"status": "present", "score": 6},
    "basement": {"status": "not_applicable", "score": null},
    "garage": {"status": "present", "score": 5},
    "backyard": {"present": true, "tree_coverage": 8, "hosting_suitability": 6},
    "layout_plan": {"present": true, "clarity_score": 8}
  }
  ```

  `layout_plan` reports whether a floor-plan/layout graphic appears among the listing's
  photos and, if so, how legible/useful it is (`clarity_score`, 0&ndash;10 &mdash; a
  crisp labeled floor plan scores high, a blurry or partial one scores low).
  `present: false` means no such graphic was found; `clarity_score` is `null` in that
  case. Unlike every other category, `layout_plan` is stored but never enters
  `condition_photo_score`, `outdoor_photo_score`, or the composite &mdash; it's captured
  purely as information Ben can glance at (e.g. while reviewing `raw_response`), not a
  scored signal.

  Each condition room reports a `status`: `"present"` (photographed, `score` is a
  0&ndash;10 rating) or `"omitted"` (the room plausibly exists but no photo shows it
  &mdash; scored at the fixed `MISSING_CATEGORY_SCORE = 20`, not dropped, since an
  omission is itself a signal). `basement` additionally allows `"not_applicable"`:
  some homes genuinely don't have one, and that status excludes it from the average
  rather than penalizing it. `garage`'s applicability is decided by `garage_expected`,
  not the model: when `garage_expected` is false, `garage` is excluded from the average
  regardless of what the model reports.

  `condition_photo_score` is a weighted average over whichever condition categories
  apply to this listing, renormalized so the applicable weights sum to 1:

  | Category | Weight |
  |---|---|
  | Kitchen | 0.35 |
  | Bathrooms | 0.30 |
  | Living space | 0.20 |
  | Basement | 0.10 |
  | Garage | 0.05 |

  (Named constants, same as the v1 rubric &mdash; a starting point, expected to be
  retuned once real listings have been scored and reviewed.)

  `outdoor_photo_score` is unchanged from the original design: the average of
  `backyard.tree_coverage` and `backyard.hosting_suitability`, each &times;10, with
  `present: false` treated as `MISSING_CATEGORY_SCORE` per attribute.

### `visual_scores` table (new, in `src/db.py`)

`listing_id, condition_photo_score, outdoor_photo_score, has_layout_plan,
layout_plan_clarity_score, photo_score_unavailable, raw_response, computed_at`.
`photo_score_unavailable` covers both exclusion paths (too few photos, and a
failed/errored API call for an eligible listing) with one flag, since `scoring.py`'s
fallback behavior is identical either way: use the v1 keyword score. `raw_response`
keeps the model's full room-by-room JSON for debugging, nullable when unavailable.
`has_layout_plan`/`layout_plan_clarity_score` are stored alongside the scored columns
for the same idempotency/caching reasons but are never read by `scoring.py` &mdash;
no accessor treats them as part of a listing's score, only as data available for
Ben to look at directly (e.g. a query against the DB) if he wants to see which
listings included a floor plan.

### `score_photos.py` (new, top-level orchestration script)

Mirrors `compute_commutes.py`'s shape, and its skip-if-already-scored check is what
makes the whole script idempotent and cron-safe:

1. For every listing with `photo_count >= MIN_PHOTOS_FOR_VISION_SCORING` and **no
   existing `visual_scores` row**, build one Batch API request (`custom_id =
   listing_id`): all of that listing's photos as image content blocks, a fixed
   instruction, and `output_config.format` pinned to the schema above. A listing
   already in `visual_scores` &mdash; from a prior run &mdash; is skipped without an API
   call, the same pattern `get_listing_ids_missing_commute` already uses for commutes.
2. For listings below the photo floor, write a `visual_scores` row directly with
   `photo_score_unavailable = true` and null scores &mdash; no API call.
3. Submit the batch, poll until it ends, and upsert each result. A per-item API error
   or malformed response also gets `photo_score_unavailable = true` rather than
   aborting the batch.

Run daily (or whenever) via cron, this script only ever pays for listings it hasn't
scored yet &mdash; a full run with nothing new to score submits an empty batch and exits,
the same shape as `compute_commutes.py`'s "already covers every listing" case.

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
&mdash; boundary cases at the 5-photo floor, each room `status` value (`present`,
`omitted`, `not_applicable`), `garage_expected = false` excluding garage regardless of
what the model reports, and the renormalized weighted average when one or more
categories are inapplicable. Also covered: `layout_plan.present = true/false` parses
into `has_layout_plan`/`layout_plan_clarity_score` correctly, and neither value shifts
`condition_photo_score` or `outdoor_photo_score` &mdash; the regression case that
matters most, since a silent leak into the composite would be easy to miss. No live API
calls in tests, matching `src/commute.py`'s precedent. `score_photos.py`'s Batch API
submission and polling is untested orchestration, matching `compute_commutes.py` and
`score.py`.

## Cost

Ballpark: ~114 listings &times; ~20 photos each through Claude Sonnet 5 with the Batch
API discount lands around $5&ndash;10 total. The richer room-by-room schema adds a
handful of output tokens per listing over the original single-field design &mdash;
negligible next to the image-token cost, which is unchanged. Worth confirming with
`count_tokens` against a handful of real listings once the schema and photo sizing are
final, rather than committing to the estimate.

## Open questions for v3

- Real geocoded or Claude-API-prompt-based trails/parks proximity, replacing the v2
  keyword scan (`docs/journal/backlog.md`).
- Recalibrate rubric wording and room weights once the six-house tour feedback is
  written up and once real listings have been scored and reviewed.
- Revisit `MIN_PHOTOS_FOR_VISION_SCORING = 5` once the real photo-count distribution
  across the 114 listings is visible.
- Downsample photos before upload if real token cost runs higher than the estimate.
