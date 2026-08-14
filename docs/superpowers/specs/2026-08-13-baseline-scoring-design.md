# home-search: v1 "baseline scoring" design

## Context

v0 shipped: scrape, store, browse. The Compass collection now matches 114
listings under a tightened saved search ($480K&ndash;$655K, 3+ beds, Arvada /
Broomfield / Westminster / Lafayette). Data lives in SQLite (`data/listings.db`),
queryable and kept fresh on every fetch (`scrape.py`, `check.py`).

This spec covers **v1: baseline scoring** &mdash; rank listings using
structural stats and cheap derived signals, before any photo/vision AI.

Ben and Megan toured six homes before this project started (Canossa Dr. was
a yes; five others were no). Their detailed reasoning isn't written down yet
and isn't needed for this pass &mdash; it's the calibration data for tuning
weights and for photo-based scoring later, not for v1.

## Goals

- A composite score (0&ndash;100) per listing, plus visible sub-scores, so a
  ranking is inspectable rather than a black box.
- Two new hard cutoffs: baths &ge; 2, lot &ge; 6,000 sqft. (Price, beds, and
  geography are already enforced by the Compass collection itself.)
- Real commute times &mdash; computed from an actual road network, not
  guessed &mdash; to Denver and to Megan's workplace (Medtronic, Lafayette).
- A cheap, explicitly weak keyword signal for backyard/mature-trees/hosting
  suitability, standing in until photo scoring exists.
- Weights that live as named constants, not embedded logic &mdash; this
  rubric is a starting point, expected to be retuned once real rankings are
  reviewed and the six-house feedback is collected.

## Non-goals (v1)

- No photo or vision AI.
- No use of the six-house tour feedback (not yet collected).
- No live-traffic-aware routing &mdash; OSRM routes the static road network,
  not current conditions.
- No automated editing of the Compass collection's own filters. Adding
  baths/lot filters there is a manual step for Ben; this spec's hard cutoffs
  are a local stand-in until he does.

## Architecture

```
listings (existing — source of truth for stats)
      +
commute (new — cached geocode + OSRM route per listing)
      ↓
scoring (new — cheap, rebuildable) → scores table
```

Two new modules, mirroring the existing store/derive split (JSON is truth,
SQLite is derived; here, `listings` is truth, `commute` and `scores` are
derived):

### `src/commute.py`

- Geocodes each address with Nominatim and routes it with OSRM &mdash; both
  free, keyless, public endpoints. Nominatim's usage policy caps requests at
  1/second and asks for a real User-Agent; the code respects both.
- Two fixed destinations: Denver Union Station (the same anchor used when
  OSRM's accuracy was spot-checked against a model-generated estimate
  earlier), and Medtronic's Lafayette location. If the specific business
  doesn't geocode, it falls back to Lafayette's city center and records
  that fallback happened.
- Writes to a `commute` table: `listing_id`, `lat`, `lon`,
  `denver_miles`, `denver_minutes`, `medtronic_miles`, `medtronic_minutes`,
  `geocode_failed`, `computed_at`.
- Only computes listings missing from the table &mdash; a rerun costs
  nothing for listings already geocoded.
- A geocode or routing failure logs a warning, marks `geocode_failed`, and
  moves to the next listing. It doesn't abort the batch, matching how the
  rest of this project already treats a single bad listing.

### `src/scoring.py`

Pure functions: `listings` row + `commute` row in, sub-scores and a
composite out. No network calls, so `rebuild_scores()` can run as often as
the rubric changes, the same way `write_csv`/`write_gallery` already
regenerate from the JSON store on every run.

**Rubric (v1 weights):**

| Factor | Weight | Scoring |
|---|---|---|
| Commute | 35% | 80% Medtronic leg / 20% Denver leg (see below) |
| Sqft | 20% | Min&ndash;max normalized across the current collection |
| Condition | 20% | Renovation keywords dominate; `year_built` is a smaller secondary signal |
| Outdoor/hosting | 15% | Keyword placeholder &mdash; explicitly weak, replaced by photo scoring later |
| Parking | 10% | Step function |

**Commute curve.** Megan's stated thresholds (ideal under 20 minutes, firm
ceiling around 30) shape the Medtronic leg: 100 points at &le;20 minutes,
a linear slide to 40 points at 30 minutes, then a steep drop &mdash; roughly
zero by 40 minutes. That's a strong penalty past 30, not a hard cliff at it.
The Denver leg has no stated threshold, so it's min&ndash;max normalized
across the collection's actual range: closer is better, nothing more
specific.

**Condition.** A curated keyword list ("Renovated," "Updated Kitchen,"
"Remodeled," "New Roof," etc.) scanned against `description` and
`amenities`. A renovation hit carries most of the score; `year_built`,
normalized across the collection's 1955&ndash;2005 range, fills the rest.
This matches the stated preference directly: an older home that's already
updated should score close to a newer one, and a dated one at a real
discount isn't penalized as heavily as raw age alone would suggest.

**Outdoor/hosting.** A second keyword list ("mature trees," "private yard,"
"backyard," "open floor plan," "entertaining," "outdoor living") against the
same two fields. Labeled provisional in code and in the scoring output &mdash;
this is a real preference that description text can only weakly proxy, and
it's the first thing to replace once photo scoring exists.

**Parking.** 2+ spaces scores 100; 1 space scores 90 (Ben: "1 car is fine");
0 or missing scores 0 &mdash; an edge case the current filtered collection
doesn't actually contain (observed range is 1&ndash;6).

### Hard cutoffs

`baths >= 2` and `lot_sqft >= 6000` are recorded as a `passes_filters` flag
on each scored listing rather than deleting rows &mdash; every scraped
listing stays in the data; the flag just marks which currently qualify.
Price, beds, and geography aren't re-checked locally; the Compass collection
already enforces them.

## Config

No new `.env` variables. Nominatim and OSRM need no credentials; their
rate limits are handled in code, not configuration.

## Error handling

- Geocode or route failure: log, mark `geocode_failed`, move on.
- Missing input stat (e.g. `year_built` is 0 or absent): treat that
  sub-score as neutral (50) rather than zero, and flag the listing as
  having incomplete data &mdash; one missing field shouldn't tank a
  composite score.

## Testing

Unit tests for every `score_*` function against fixture listings with known
inputs, covering the boundary cases that matter: 20/30-minute commute
thresholds, 0/1/2 parking spaces, missing `year_built`. Same house style as
`tests/test_db.py` and `tests/test_diff.py` &mdash; module-level fixtures,
`tmp_path`, no test classes.

Geocoding and routing aren't tested live, the same way `scrape.py` and
`check.py`'s Playwright orchestration isn't: the network calls are thin
wrappers; the logic worth testing is what happens with their results.

## Open questions for v2 (not blocking this spec)

- Retune weights once the six-house feedback is written down and once real
  rankings have been eyeballed against gut sense.
- Replace the outdoor/hosting placeholder with photo-based scoring.
- Revisit OSRM's static routing if it proves materially off for real
  peak-hour commute decisions.
- Once Ben adds the baths/lot filters in the Compass portal itself, the
  local `passes_filters` cutoff becomes redundant but stays harmless as a
  safety net.
