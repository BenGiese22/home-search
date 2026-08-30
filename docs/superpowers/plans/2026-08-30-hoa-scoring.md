# HOA Fee Awareness — Findings, Design, and Implementation Plan

Date: 2026-08-30
Original Author: Benjamin Giese

## Findings (read this before anything else)

**Compass's structured listing JSON does not expose HOA anywhere.** Every source of listing data this scraper touches was checked:

- `tests/fixtures/canossa_dr_listing.json` and `tests/fixtures/collection_response_sample.json` — the two known shapes (`scrape_listing`'s single-page embed and `fetch_collection_listings`'s paginated API), both derived from real Compass responses. Neither contains `hoa`, `association`, `dues`, `monthlyFee`, or any other fee-shaped key, in `detailedInfo` or anywhere else.
- `src/listing_parser.py` (`parse_listing_object`) — confirms the extracted field surface: address/price/beds/baths/sqft/lot/parking/year/description/amenities/photos/property_type/localized_status. No fee field is read because none exists in the object.
- `data/listings/*.json` (all 85 already-scraped real listings) — grepping every stored `description` for "hoa" case-insensitively finds **4 listings, and all 4 only say the home has no HOA** ("With no HOA, friendly neighbors...", "...neighborhood with no HOA...", "...the rare advantage of no HOA...", "...HOA-free Northmoor neighborhood!"). All 4 are matched by the `_NO_HOA_RE` patterns below. **Zero listings in the current real dataset state a dollar figure or cadence for an HOA fee.** This matches the existing note in `docs/improvement-ideas.md`: *"HOA fees — Compass sometimes buries this in free-text description; Zillow/Realtor.com often have it as a structured field."*

**Conclusion:** there is no structured HOA field to "capture during the scrape" today. The only usable signal Compass gives us is prose in `description`, and in every real example seen so far that prose only asserts absence, never a number. Three ways forward:

1. **(Recommended, this plan) Best-effort description-text parsing.** Detect explicit "no HOA" phrasing (→ confirmed $0) and, forward-looking, a `$amount` + cadence pattern near an "HOA"/"association" mention (→ normalized annual figure), falling back to "unknown" when neither matches. Costs nothing extra to scrape (no new page/API calls), works today for the "no HOA" case we've actually observed, and is ready the day a listing's description does include a dollar figure. Flag (or paste) the first live Compass listing seen with an actual `$` HOA figure in the text so it can become a permanent regression fixture — the amount-parsing path is currently untested against real prose.
2. **(Follow-up, out of scope) Verify the raw Compass payload more thoroughly.** The trimmed fixtures may not reflect every key a *live* page embeds — worth a one-time check of a live listing's raw JSON (e.g. temporarily logging `candidates[0]` in `scrape_listing`) for a `financialDetails`/`hoaDues`-shaped key before assuming description-parsing is the ceiling. If one exists, slot it in as a higher-confidence source ahead of text parsing.
3. **(Not now) External data source (Zillow/Realtor/ATTOM etc.)** — already captured as a bigger, riskier idea in `docs/improvement-ideas.md`.

This plan implements option 1, structured so option 2 can slot in later as an additional extraction path without changing the model, scoring, or storage design.

## Normalization Design

**Cadence values to handle** (none appear in the current corpus yet; these are the standard industry forms, parsed defensively): monthly (`/mo`, `per month`, `monthly`), quarterly (`per quarter`, `quarterly`), semi-annual (`semi-annual`, `semiannual`), annual (`/yr`, `per year`, `annually`, `yearly`, `per annum`).

**New pure module `src/hoa.py`** (mirrors the `src/commute.py` pattern — pure text/data logic, no I/O):

```python
import re

_NO_HOA_RE = re.compile(
    r"\bno\s+hoa\b|\bhoa[-\s]?free\b|\bwithout\s+(?:an?\s+)?hoa\b", re.IGNORECASE
)
_MENTION_RE = re.compile(r"\bhoa\b|\bassociation\s+fee\b", re.IGNORECASE)
_AMOUNT_RE = re.compile(r"\$\s?([\d,]+(?:\.\d+)?)")

_CADENCE_PATTERNS: list[tuple[re.Pattern, float]] = [
    (re.compile(r"per\s*month|/\s*mo\b|monthly", re.IGNORECASE), 12.0),
    (re.compile(r"per\s*quarter|quarterly", re.IGNORECASE), 4.0),
    (re.compile(r"semi[-\s]?annual(?:ly)?", re.IGNORECASE), 2.0),
    (re.compile(r"per\s*year|/\s*yr\b|annually|per\s*annum|yearly", re.IGNORECASE), 1.0),
]
_PROXIMITY_WINDOW = 60  # chars either side of an HOA/association mention


def parse_hoa_from_description(description: str) -> float | None:
    """Best-effort annual HOA fee mined from free-text listing prose --
    Compass's structured JSON (both the single-listing embed and the
    collection API) has no HOA field at all (confirmed against every
    fixture and every already-scraped real listing in data/listings/,
    2026-08-30), so description text is the only source available today.

    Returns:
      - a positive annual dollar figure, when a $amount is found within
        _PROXIMITY_WINDOW characters of an "HOA"/"association fee" mention
        AND an unambiguous cadence keyword is found in that same window
      - 0.0 when the description explicitly states there's no HOA
      - None when neither pattern matches -- covers both "never mentions
        HOA" and "mentions HOA but with no parseable amount/cadence"
        (e.g. "HOA required, ask agent"). None is a distinct, deliberately
        conservative "unknown", never guessed at -- a wrong 12x cadence
        guess would badly distort scoring, so an ambiguous match is
        treated the same as no match at all rather than assumed monthly.

    Amount-plus-cadence is checked before the no-HOA phrase, so an amount
    mentioned near an incidental "no HOA" aside still wins (the more
    specific, informative signal).
    """
    for match in _MENTION_RE.finditer(description):
        window = description[
            max(0, match.start() - _PROXIMITY_WINDOW) : match.end() + _PROXIMITY_WINDOW
        ]
        amount_match = _AMOUNT_RE.search(window)
        if not amount_match:
            continue
        cadence = next(
            (multiplier for pattern, multiplier in _CADENCE_PATTERNS if pattern.search(window)),
            None,
        )
        if cadence is None:
            continue
        return float(amount_match.group(1).replace(",", "")) * cadence

    if _NO_HOA_RE.search(description):
        return 0.0

    return None
```

**Three-state representation** — the key design decision, and a deliberate break from this codebase's usual convention: `Listing.hoa_annual: float | None = None`.

- `None` — unknown / not disclosed.
- `0.0` — confirmed no HOA.
- `> 0.0` — known annual fee in dollars.

Every other numeric `Listing` field (`sqft`, `year_built`, `beds`, ...) uses `0` as its own "missing" sentinel (see `score_sqft`'s `if not sqft: return NEUTRAL_SCORE`). That convention is exactly wrong for HOA, because `0` is a real, meaningful, first-class value ("confirmed no fee") that must never collide with "we don't know." `None` for missing plus `0.0` for confirmed-zero keeps the three states unambiguous without a second boolean flag. Call this out explicitly in code review — it is an intentional exception, not an oversight.

## Schema / Storage Changes

- **`src/models.py`** — add `hoa_annual: float | None = None` to `Listing`, defaulted (same reasoning as the existing `property_type`/`localized_status` defaults) so `Listing(**data)` still works for every JSON file in `data/listings/` saved before this field existed.
- **`src/db.py` `_SCHEMA`** — add `hoa_annual REAL` (nullable, no `NOT NULL`, no `DEFAULT`, matching the existing `price_numeric REAL` precedent) to `listings`, and `hoa_score REAL NOT NULL DEFAULT 0` to `scores` (matching `room_count_score`'s migration precedent — the whole `scores` row is rewritten every `score.py` run anyway, so the default is only transiently visible).
- **`init_db()`** — add the two matching `ALTER TABLE ... ADD COLUMN` guards, following the pattern already used for `property_type`/`is_pinned`/`room_count_score`.
- **`upsert_listing()`** — add `hoa_annual` to the `INSERT OR REPLACE INTO listings (...)` column list and value tuple.
- **`upsert_score()`** — add `hoa_score` to the `scores` insert.
- **Turso (`src/turso_sync.py`) — no code changes required.** `ensure_schema()` re-parses `src.db._SCHEMA` generically (`_migrate_missing_columns`) and diffs it against each mirrored table's actual columns via `PRAGMA table_info`, so both new columns are picked up automatically on the next `publish.py` run. This is the same mechanism that exists to avoid the drift behind the `visual_scores` orphaned-row incident — ride it rather than adding new migration code.
- **`src/store.py` (JSON per-listing files) — no code changes required.** `save_listing`/`load_all_listings` round-trip via `dataclasses.asdict`/`Listing(**data)`; the new defaulted field flows through, as `property_type` did.
- **`score.py`** — `_row_to_listing()` needs `hoa_annual=row["hoa_annual"]`.
- **`src/csv_writer.py`** — add `"hoa_annual"` to `FIELDNAMES` and populate it (blank string when `None`, else the raw float) in `write_csv()`.
- **`src/gallery.py`** — add a formatted line to each listing's `<section>`: `"No HOA"` / `"HOA $X,XXX/yr"` / nothing when unknown.
- **Backfill for existing rows** — new one-off top-level script `backfill_hoa.py` (mirrors the `backfill_db.py`/`backfill_photos.py` convention: untested orchestration, no test file). It re-parses each already-downloaded listing's **existing** `description` (no re-scrape — the text is already on disk in `data/listings/*.json`), rewrites the JSON via `save_listing()` when a value is found, and re-upserts into SQLite via `upsert_listing()`, preserving pin status exactly like `backfill_db.py`. Given the 4 known "no HOA" listings, one run should populate `hoa_annual = 0.0` for those 4 and `None` for the other 81, with zero network calls.

**Migration ordering** (the thing to be careful about, given the Turso batching/orphan history): (1) ship the code with the new nullable column + `ALTER TABLE` guards, so the next `get_connection()` call anywhere migrates the local DB before anything writes to it; (2) run `backfill_hoa.py` once to populate `hoa_annual` from stored descriptions; (3) run `score.py` once to recompute every `scores` row including `hoa_score`; (4) only then run `publish.py`, so Turso's first sync of the new columns carries real values instead of a wave of `NULL`s a second sync would overwrite. None of this is required for correctness — everything is idempotent — but this order avoids a needless double-sync of every listing row.

## Scoring Design

### Current idiom (from `src/scoring.py`)

Every existing factor is a `score_*(...) -> float` sub-score on a fixed **0–100** scale, where missing input degrades to `NEUTRAL_SCORE = 50.0` rather than `0.0` (documented for `score_commute`, `score_sqft`, `score_condition`'s year component, `score_room_count`). Sub-scores blend into `composite` via named `WEIGHT_*` constants summing to exactly `1.0`:

```
WEIGHT_COMMUTE=0.30, WEIGHT_SQFT=0.20, WEIGHT_CONDITION=0.20,
WEIGHT_OUTDOOR=0.15, WEIGHT_ROOM_COUNT=0.10, WEIGHT_PARKING=0.05
```

HOA fits this idiom cleanly: "no HOA very slight positive," "unknown neutral," "low HOA slight negative," "high HOA much more negative" is exactly a 0–100 sub-score centered slightly below 50 for positive fees, slightly above 50 for confirmed zero, and pinned at `NEUTRAL_SCORE` for unknown.

### The formula

```python
# src/scoring.py -- new constants, alongside the existing WEIGHT_* block
WEIGHT_HOA = 0.045

# All six existing weights scaled by (1 - WEIGHT_HOA) = 0.955, preserving
# their relative importance to each other while making room for HOA --
# not carved out of any one factor arbitrarily.
WEIGHT_COMMUTE = 0.2865     # was 0.30
WEIGHT_SQFT = 0.191         # was 0.20
WEIGHT_CONDITION = 0.191    # was 0.20
WEIGHT_OUTDOOR = 0.14325    # was 0.15
WEIGHT_ROOM_COUNT = 0.0955  # was 0.10
WEIGHT_PARKING = 0.04775    # was 0.05
# sums to exactly 1.0 in float arithmetic (verified, not assumed)

HOA_NO_FEE_BONUS = 2.5        # confirmed-$0 scores this far above neutral
HOA_MAX_PENALTY = 50.0        # asymptote: approached, never reached
HOA_HALF_PENALTY_AT = 3850.0  # annual fee costing half of HOA_MAX_PENALTY
HOA_PENALTY_STEEPNESS = 2.6   # how sharply the curve turns around that point


def score_hoa(annual_hoa: float | None) -> float:
    """Unknown (None) is neutral -- no bonus, no penalty, exactly like every
    other missing-data case in this module. Confirmed no HOA (0.0) earns a
    small bonus.

    A positive fee is penalized on a saturating curve rather than a plain
    power curve: shallow at the low end (a cheap HOA barely dents a home
    that's good on the things that matter), steepest through the middle
    where the fee starts being real money, then flattening as it approaches
    HOA_MAX_PENALTY without ever quite reaching it.

    Two readable knobs. HOA_HALF_PENALTY_AT is the annual fee that costs
    exactly half of HOA_MAX_PENALTY -- lowering it slides the whole curve
    left so every fee costs more. HOA_PENALTY_STEEPNESS controls how
    sharply the curve turns around that point: raising it makes cheap fees
    cheaper and expensive ones more expensive, widening the gap between
    them rather than shifting everything at once.

    Because the curve only approaches its asymptote, there is no hard cap
    and therefore no flat region -- a $9,600 and a $20,000 HOA still rank
    differently, which a hard ceiling could not do. The sub-score stays
    above 0 for any finite fee, so HOA remains a factor rather than a veto.
    """
    if annual_hoa is None:
        return NEUTRAL_SCORE
    if annual_hoa <= 0:
        return NEUTRAL_SCORE + HOA_NO_FEE_BONUS
    fee = annual_hoa ** HOA_PENALTY_STEEPNESS
    midpoint = HOA_HALF_PENALTY_AT ** HOA_PENALTY_STEEPNESS
    return _clamp(NEUTRAL_SCORE - HOA_MAX_PENALTY * fee / (fee + midpoint))
```

### Worked table

Composite impact is this sub-score's contribution at `WEIGHT_HOA = 0.045`, measured against a neutral/unknown HOA.

| annual HOA | meaning | `score_hoa` | vs. neutral (50) | composite impact |
|---|---|---|---|---|
| unknown (`None`) | not disclosed | **50.0** | neutral | 0.00 |
| $0 | confirmed no HOA | **52.5** | +2.5 | +0.11 |
| $600 | very low | **49.6** | −0.4 | −0.02 |
| $1,200 | low (spec example) | **47.7** | −2.3 | −0.10 |
| $2,400 | moderate | **38.7** | −11.3 | −0.51 |
| $3,600 | elevated | **27.2** | −22.8 | −1.03 |
| $6,000 | high (spec example) | **12.0** | −38.0 | −1.71 |
| $9,600 | condo-tier | **4.3** | −45.7 | −2.06 |
| $12,000 | extreme | **2.5** | −47.5 | −2.14 |
| $20,000 | absurd | **0.7** | −49.3 | −2.22 |

$6,000 costs **16.5x** the $1,200 penalty for a fee only 5x larger — the widest separation of any pass, and the whole point of the final tuning. A $1,200 HOA is now nearly free (−0.10 composite, comfortably inside the noise between two comparable homes), which is precisely the "low enough cost and the home is of good value" case. By $6,000 it costs 1.71 composite points — enough to move a listing well down a ranking, not enough to sink one that wins on commute, size, and condition. And the curve keeps discriminating past $9,600 instead of going flat.

### Calibration notes (final)

Pass 1 was too harsh, pass 2 overcorrected, pass 3 fixed the shape, and passes 4–5 tuned the bite. Two things did the real work: changing the curve's shape, then separating *where* it turns from *how sharply* it turns.

| | pass 1 (hot) | pass 2 (soft) | pass 3 | pass 4 | **final** |
|---|---|---|---|---|---|
| shape | power, floors at 0 | power + hard cap | saturating | saturating | **saturating, steeper** |
| `WEIGHT_HOA` | 0.05 | 0.04 | 0.045 | 0.045 | **0.045** |
| `HOA_HALF_PENALTY_AT` | — | — | 5000 | 4200 | **3850** |
| `HOA_PENALTY_STEEPNESS` | — | — | 2.0 | 2.0 | **2.6** |
| $1,200 composite | −0.25 | −0.15 | −0.12 | −0.26 | **−0.10** |
| $6,000 composite | −1.88 | −1.03 | −1.33 | −1.51 | **−1.71** |
| $6k / $1.2k ratio | 7.5x | 6.9x | 10.8x | 6.7x | **16.5x** |
| flat region | none | everything ≥ $6,813 | none | none | **none** |

The last two steps are worth separating. Pass 4 slid the curve left (`HOA_HALF_PENALTY_AT` 5000 → 4200), which did make $6,000 bite harder — but it dragged the low end along with it, pushing $1,200 to −0.26, *harsher than the pass-1 calibration that was rejected as too drastic*. Sliding a curve makes everything cost more, including the cases that are supposed to stay cheap.

The final pass got the same additional bite at $6,000 by raising `HOA_PENALTY_STEEPNESS` from 2.0 to 2.6 and pulling `HOA_HALF_PENALTY_AT` back to 3850. A steeper transition widens the gap between cheap and expensive instead of shifting both: $6,000 lands at −1.71 (harder than pass 4) while $1,200 improves to −0.10 (softer than any previous pass). That is the stated intent stated precisely — an expensive HOA should hurt, a cheap one should barely register — and it is why the two knobs are separate constants.

On the shape change: pass 2 capped the penalty to keep an outlier fee from cratering the score, but bought that with a flat region starting at $6,813 — right inside the band where condo and townhome HOAs live, so a $6,800 and a $20,000 fee scored identically. The saturating curve gets the same protection for free: it approaches `HOA_MAX_PENALTY` asymptotically, so the sub-score never hits 0 and never goes flat. The hard cap and its edge case disappeared together.

It also has a better low end. At `HOA_PENALTY_STEEPNESS = 2.6` the curve is very flat below ~$1,500, so a genuinely cheap HOA is a rounding error rather than a penalty — closer to the stated intent than any earlier pass managed.

**When retuning, pick the knob that matches the complaint.** If *everything* feels off, move `HOA_HALF_PENALTY_AT` — lower to make every fee hurt more, raise to soften across the board. If only the expensive end is wrong, move `HOA_PENALTY_STEEPNESS` — raising it punishes big fees harder while making small ones cheaper, lowering it compresses the two toward each other. Pass 4 is the cautionary example: reaching for the slide knob when the real complaint was about the expensive end made $1,200 harsher than the calibration already rejected as too drastic.

### Wiring into `score_listing`/`ScoreResult`

- `ScoreResult` gains `hoa_score: float`.
- `score_listing()` computes `hoa_score = score_hoa(listing.hoa_annual)` and adds `WEIGHT_HOA * hoa_score` to `composite`.
- `has_incomplete_data` gains `or listing.hoa_annual is None` — an unknown HOA is flagged the same way a missing `sqft`/`year_built`/commute already is, even though it doesn't move the score off neutral. Confirmed `0.0` is real, complete data and does **not** set the flag.

## Test Plan (TDD order — tests before implementation)

### 1. `tests/test_hoa.py` (new)

- `test_no_hoa_phrase_returns_zero` — parametrize over `"With no HOA, ..."`, `"...HOA-free neighborhood!"`, `"...without an HOA..."`.
- `test_no_mention_at_all_returns_none`.
- `test_monthly_amount_normalizes_to_annual` — `"$150/month HOA"` → `1800.0`.
- `test_quarterly_amount_normalizes_to_annual` — `"HOA fee of $300 per quarter"` → `1200.0`.
- `test_semi_annual_amount_normalizes_to_annual` — `"$600 semi-annual HOA due"` → `1200.0`.
- `test_annual_amount_stays_as_is` — `"HOA: $1,200/year"` → `1200.0`.
- `test_comma_formatted_amount_parses_correctly`.
- `test_amount_without_cadence_returns_none` — `"HOA $150"` with no cadence marker; documents the conservative no-guessing choice.
- `test_case_insensitive_matching` — `"Hoa fee $100/MO"`.
- `test_unrelated_dollar_amount_outside_window_is_not_mistaken_for_hoa`.
- `test_amount_and_no_hoa_phrase_both_present_amount_wins`.

### 2. `tests/test_listing_parser.py`

- Update `test_parse_listing_object_missing_optional_fields_defaults_safely` to also assert `listing.hoa_annual is None`.
- `test_parse_listing_object_sets_hoa_annual_none_from_real_fixture` — the Canossa fixture mentions nothing HOA-related.
- `test_parse_listing_object_sets_hoa_annual_zero_from_no_hoa_description`.
- `test_parse_listing_object_sets_hoa_annual_from_dollar_amount_in_description` — `"...HOA $150/month..."` → `1800.0`.

### 3. `tests/test_models.py`

- `test_listing_hoa_annual_defaults_to_none_for_backward_compatible_construction` — mirrors the existing `property_type`/`localized_status` default test.

### 4. `tests/test_scoring.py`

- Add `hoa_annual=0.0` to the module-level `LISTING` fixture.
- `test_score_hoa_unknown_is_neutral` — `score_hoa(None) == 50.0`.
- `test_score_hoa_no_fee_is_slightly_above_neutral` — `score_hoa(0.0) == 52.5`.
- `test_score_hoa_low_fee_is_slightly_below_neutral` — `score_hoa(1200) == pytest.approx(47.7, abs=0.1)`.
- `test_score_hoa_high_fee_penalty_is_much_larger_than_low_fee_penalty` — `(50 - score_hoa(6000)) > 5 * (50 - score_hoa(1200))` (actual ratio 16.5x, so the 5x assertion has wide headroom for retuning).
- `test_score_hoa_curve_accelerates_rather_than_scaling_linearly` — marginal penalty over `[1200, 2400]` exceeds that over `[0, 1200]`.
- `test_score_hoa_never_reaches_zero_for_any_finite_fee` — `score_hoa(50000) > 0` and `score_hoa(1_000_000) > 0`; asserts the asymptote holds so HOA stays a factor, never a veto.
- `test_score_hoa_keeps_discriminating_above_the_expensive_band` — `score_hoa(9600) > score_hoa(20000)`; this is the regression test for the hard-cap flat region the saturating curve was chosen to avoid, and would fail under the previous capped design.
- `test_score_hoa_half_penalty_constant_behaves_as_documented` — `score_hoa(HOA_HALF_PENALTY_AT) == pytest.approx(NEUTRAL_SCORE - HOA_MAX_PENALTY / 2)`; pins that knob's stated meaning (it holds for any `HOA_PENALTY_STEEPNESS`) so retuning stays interpretable.
- `test_score_hoa_cheap_fee_stays_negligible_while_expensive_fee_bites` — `(50 - score_hoa(1200)) < 3` and `(50 - score_hoa(6000)) > 30`; encodes the actual product intent as a test, so a future retune that makes cheap HOAs meaningfully costly fails loudly rather than drifting.
- `test_score_hoa_monotonically_decreases_as_fee_increases` — walk `[0, 600, 1200, 2400, 3600, 6000, 9600, 12000, 20000]` and assert *strictly* decreasing (the saturating curve has no flat region, so this can be strict).
- Update `test_score_listing_combines_sub_scores_with_named_weights`'s `expected` to add `+ WEIGHT_HOA * result.hoa_score`.
- `test_score_listing_flags_incomplete_data_when_hoa_unknown`.
- `test_score_listing_has_incomplete_data_false_when_hoa_confirmed_zero` — the most important regression test here: it is the one place the deliberate "0 ≠ missing" decision could silently regress to the codebase's usual convention.

### 5. `tests/test_db.py`

- Add `hoa_annual` to `SAMPLE`/relevant fixtures as needed.
- `test_upsert_listing_roundtrips_hoa_annual_unknown` — `None` in, `None` out (not `0`).
- `test_upsert_listing_roundtrips_hoa_annual_known_value` — `1200.0` round-trips exactly.
- `test_init_db_migrates_existing_listings_table_missing_hoa_annual_column` — mirrors the existing migration-guard tests for `property_type`/`is_pinned`.
- `test_upsert_score_then_get_scores_includes_hoa_score`.

### 6. `tests/test_turso_sync.py`

- Extend `test_ensure_schema_migrates_a_mirror_created_before_a_column_existed` with `hoa_annual`/`hoa_score` — that test hardcodes `property_type`/`localized_status` rather than being fully generic, so keep it 1:1 with the real schema even though `_migrate_missing_columns` needs no code changes.

### 7. `tests/test_csv_writer.py`

- Extend `test_write_csv_round_trips_fields` asserting the `hoa_annual` column is present and blank for `None`; add a second case for a known value.

### 8. `tests/test_gallery.py`

- Assert the rendered HTML shows "No HOA" for `0.0`, an "$X,XXX/yr" figure for a known positive value, and omits the line for `None`.

### 9. `backfill_hoa.py`

- No test file (matches `backfill_db.py`/`backfill_photos.py` precedent). Verify manually: run against `data/listings/`, print a summary count of `no-HOA / known-amount / unknown` before committing, and confirm the 4 known "no HOA" listings land at `0.0`.

## Staged Steps (dependency order, one commit each)

1. **`src/hoa.py` + `tests/test_hoa.py`** — parsing/normalization, independent of everything else. Failing tests first, then `parse_hoa_from_description`.
2. **`src/models.py` + `tests/test_models.py`** — add `Listing.hoa_annual: float | None = None`.
3. **`src/listing_parser.py` + `tests/test_listing_parser.py`** — wire `parse_hoa_from_description(obj.get("description", ""))` into `parse_listing_object`. Depends on 1–2.
4. **`src/db.py` (`listings`) + `tests/test_db.py`** — `_SCHEMA` column, `init_db` guard, `upsert_listing`. Depends on 2.
5. **`src/scoring.py` + `tests/test_scoring.py`** — `score_hoa`, rebalanced `WEIGHT_*`, `ScoreResult.hoa_score`, `score_listing` wiring, `has_incomplete_data`. Depends on 2, not 4.
6. **`src/db.py` (`scores`) + `tests/test_db.py`** — `_SCHEMA` column, guard, `upsert_score`. Depends on 5.
7. **`tests/test_turso_sync.py`** — extend the migration test for both new columns (no `src/turso_sync.py` changes). Depends on 4 and 6.
8. **`score.py`** — `_row_to_listing` passes `hoa_annual` through. Depends on 4–6. No test file (untested orchestration, existing precedent).
9. **`src/csv_writer.py` + `tests/test_csv_writer.py`** — add the column. Depends on 2.
10. **`src/gallery.py` + `tests/test_gallery.py`** — display line. Depends on 2; independent of 3–9.
11. **`backfill_hoa.py`** — one-off backfill. Depends on 1, 3, 4. Manual smoke test against real `data/listings/`, then run for real, then `score.py`, then `publish.py`.

Steps 9–10 have no ordering dependency on 5–8 and can run in parallel if split across sessions.

## Open Questions / Risks

- **No real example of a dollar-figure HOA mention exists anywhere in this repo's data.** The amount+cadence path is built defensively against realistic phrasing, but it is untested against a real Compass description. Capture the first live example seen so it can become a fixture — there is real risk the actual phrasing does not match the patterns guessed here.
- **Ambiguous-cadence amounts are silently dropped to "unknown" rather than guessed.** Deliberate and documented, but it means `"HOA $150"` with no cadence marker gets no score effect, even though a human would probably read it as monthly. Worth revisiting once real examples surface.
- **Weight rebalancing method is one reasonable choice, not the only one.** Scaling all six existing weights by `0.955` avoids picking an arbitrary donor, but it does shave the currently-dominant commute weight (`0.30 → 0.2865`). Taking the full `0.045` from `WEIGHT_PARKING` alone would leave commute/sqft/condition untouched but cut parking to `0.005`, effectively retiring it — worse than spreading the cost. The resulting constants are unlovely (`0.14325`, `0.04775`); they sum to exactly `1.0` in float arithmetic (verified), but if any existing test asserts that sum it should use `pytest.approx` to stay robust. Shifts are small enough (≤0.0135 per weight) that rank changes among existing listings should be rare — worth confirming with a before/after `score.py` run.
- **The four HOA constants are calibrated against the two example points ($1,200 slight, $6,000 substantially larger)**, not against real house-tour feedback the way `RENOVATION_KEYWORDS`/`YEAR_BUILT_MIN`/`_medtronic_leg_score` eventually were. Same status as every other v1 constant in `src/scoring.py` — expect retuning once real rankings including HOA are reviewed. Given that no listing in the corpus has a known fee yet, the whole penalty branch is currently unexercised by real data, so err toward the gentle side until it is.
- **`HOA_MAX_PENALTY = 50` is an asymptote, so no fee is ever scored as fully disqualifying.** A $1M/yr HOA still scores just above 0 rather than at it. That is the intended trade — HOA stays one signal among seven — but it does mean the composite alone will never rule a listing out on fees. If a hard "never show me anything above $X/yr" rule is ever wanted, that belongs in filtering, not in the score.
- **`backfill_hoa.py` rewrites files in `data/listings/`.** `save_listing()` uses a temp-file-then-`os.replace` pattern, so it is safe against partial writes. `data/` is gitignored (confirmed 2026-08-30 — `git ls-files data` returns nothing), so the mechanical `hoa_annual` key added to all 85 files produces no git diff; the files are still worth backing up before the first run since the script rewrites them in place.

## Critical Files for Implementation

- `src/hoa.py` (new — parsing/normalization, the crux of the findings gap)
- `src/models.py` (`Listing.hoa_annual`, the None-vs-0-vs-value design)
- `src/scoring.py` (`score_hoa`, weight rebalance, `score_listing`/`ScoreResult` wiring)
- `src/db.py` (`listings.hoa_annual` + `scores.hoa_score` schema/migration/upsert)
- `src/listing_parser.py` (wires parsing into every scraped/collection-fetched listing)
