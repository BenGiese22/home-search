# Structured Listing Fields — HOA Re-sourcing, Property Tax, and Grade-Split Sqft

Date: 2026-08-30
Original Author: Benjamin Giese

Supersedes, in part: `2026-08-30-hoa-scoring.md`. That plan's scoring design (`score_hoa`, the saturating curve, the three-state `hoa_annual` semantics, the weight rebalance) stands unchanged and is not re-litigated here. Its **Findings** section — "Compass's structured listing JSON does not expose HOA anywhere" — is **wrong**, and this plan replaces it.

## Findings (read this before anything else)

**The 2026-08-30 HOA plan's central factual claim is false, and the way it became false is the lesson of this document.** That investigation checked `tests/fixtures/canossa_dr_listing.json` (1.1 KB) and `tests/fixtures/collection_response_sample.json` (1.3 KB) — fixtures hand-trimmed down to *exactly the fields `parse_listing_object` already reads* — and concluded no structured HOA field exists. The real payloads are ~45 KB with 35 top-level keys and ~164 leaf fields. Checking trimmed fixtures for fields the trimming removed can only ever confirm the parser's existing blind spots. A live full-corpus probe (all 85 listings, zero failures; captures preserved at `data/raw-captures/`: `payloads/<listing_id>.json`, `key_fields_per_listing.json`, `leaf_stats.json`, `sqft_deep_dive.json`, plus `collection_raw_capture.json`, `probe2_condos.json`, `findings-hoa-semantics.md`) established:

### HOA is structured, free, and three-way corroborated

```json
"price": {"monthlySalesCharges": 0, "monthlySalesChargesInclTaxes": 367.25,
          "charges": [{"chargeAmount": 4407, "paymentFrequentType": 3, "chargeType": 0}]}
```

- `price.charges[].chargeType`: **0 = annual property tax, 2 = annual HOA fee** (1 never observed in any sample). `paymentFrequentType` is always 3 (annual) and describes the cadence of the *aggregated* total, not per-association cadence.
- `price.monthlySalesCharges` = `sum(charges[chargeType==2].chargeAmount) / 12` — verified exact on 4 positive-control condos, including one with **two** HOAs (sub + master) summing to $3,066/yr = $255.50/mo, which also equals `detailedInfo.listingDetails["Association Fee Total Annual"]`.
- `detailedInfo.listingDetails["Association"]` / `regionalKeyDetails["Association"]` is an explicit `Yes`/`No` — ground truth for HOA *presence*. Across all 85 probed payloads it agrees perfectly with `charges`: 77 `No` with no chargeType:2 entry, 8 `Yes` with a positive chargeType:2 amount. Genuine no-HOA listings **omit** the chargeType:2 entry entirely (they still carry the chargeType:0 tax entry) and show `keyDetails["HOA Fees"] == "-"`.
- **8 of our 85 listings have a real HOA.** `src/hoa.py`'s description regex found **none of them** — because 78/85 descriptions are empty (the collection API omits `description`; only 7 listings have prose, from which the regex correctly resolved 4 no-HOA phrases). Where the regex *did* resolve, it matched the structured value exactly (0 mismatches), so it is accurate but blind: demote it, don't delete it.
- Concrete damage in the current DB: 3123 West 105th Court ($781.56/yr), 8052 Fenton Court ($864/yr), 10191 Zenobia Circle ($1,000/yr) all sit at `hoa_annual = NULL`, `hoa_score = 50.0`.
- Residual risks (guard, don't assume): no example was found of `Association: Yes` with a genuinely $0 fee, nor of the `Association` field being absent from a detail payload. Neither may be assumed impossible.

### Two payload shapes, and they differ

This matters for the precedence chain. **The collection API payload** (`fetch_collection_listings`, how ~83/85 listings enter, verified against `collection_raw_capture.json`) carries `price.charges[]`, `price.monthlySalesCharges`, `detailedInfo.assessorDetails`, and the grade-split sqft — but **not** `description`, `keyDetails`, `listingDetails`, or `regionalKeyDetails`. **The detail-page embed** (`scrape_listing`, the 2 pinned `LISTING_URLS`) carries everything, including the `Association` Yes/No ground truth. Every extraction rung below must degrade gracefully when its field is absent — for collection listings the chain effectively starts at `charges`.

### Property tax is structured and near-universal — but has two disagreeing sources

- `charges[chargeType==0].chargeAmount`: numeric, present **85/85**, and equals the figure Compass displays (`keyDetails["Taxes"]`, e.g. `"$4,407 / year"`) in every case checked.
- `detailedInfo.assessorDetails.assessorInfo.propertyTax.{tax, monthlyTax, taxYear}`: present **84/85** (the miss has no assessor record at all), formatted strings (`"$4,407"`, `"$367"`).
- The two **disagree on 15/85 (18%)**, sometimes by ~3x (e.g. 7471 Wilson Court: MLS-quoted $2,066 vs assessor $753). The pattern is consistent with owner-specific exemptions (senior/veteran) in the assessor figure — a number that will not survive transfer to a new buyer. The MLS-quoted `charges` figure is the primary source; assessor is fallback only.

### Square footage: `squareFeet` silently includes unfinished basement space

- `size.aboveGradeTotalAreaSquareFeet`: **85/85**. `size.belowGradeTotalAreaSquareFeet`: **74/85** — and it is missing on *exactly* the 11 listings where `regionalKeyDetails["Basement"] == "No"`. **Absence means "no basement," not missing data.** A `0` with a basement present means an unfinished basement (e.g. 945 Garnet: `squareFeet` 2641, above 1739, below 0 → ~902 sqft unfinished).
- `squareFeet == above + below` on 40/85; `squareFeet > above + below` on 45/85; **`above + below` is never greater than `squareFeet`** (shortfall 2–1560 sqft, median ~187). `price.perSquareFoot == lastKnown/squareFeet` on 85/85. Interpretation: `squareFeet` is the MLS total-footprint figure driving displayed $/sqft; `above + below` is finished living area. They are different quantities, not a broken sum.
- Consequence for `score_sqft` (min-max normalized across the corpus): a listing with a large unfinished basement currently outranks a fully-finished listing of equal living area. On 45/85 listings the comparison is not like-for-like.

## Design

### 1. HOA re-sourcing — precedence chain

New pure module **`src/structured_fields.py`** (mirrors the `src/hoa.py`/`src/commute.py` pattern: pure data logic, no I/O), housing `extract_hoa_annual(obj) -> float | None` plus the tax and sqft extractors below. `src/hoa.py` stays but is **demoted to fallback**; its header comment ("Compass's structured listing JSON has no HOA field at all") is factually wrong and must be rewritten to record what happened and why the module survives as a fallback.

Precedence, evaluated top-down; each rung is skipped when its field is absent from the payload shape at hand:

1. **`Association` ground truth** (`detailedInfo.listingDetails["Association"]` or `regionalKeyDetails["Association"]`; detail payloads only).
   - `"No"` → **0.0** (confirmed no HOA), regardless of anything below.
   - `"Yes"` → an HOA exists; the *amount* must come from rungs 2–3. If no positive amount is found anywhere → **`None`** (unknown fee), **never 0.0** — this is the guard for the unobserved "Association Yes, $0 fee" edge, and it correctly sets `has_incomplete_data`.
   - Absent (every collection payload) → fall through.
2. **`charges[chargeType==2]`** — authoritative annual amount.
   - Sum over all chargeType:2 entries (multi-HOA case) → that positive sum.
   - `charges` present and non-empty (i.e. the feed populated it — the chargeType:0 tax entry is there) but no chargeType:2 entry → **0.0**, confirmed no HOA (the exact structural signature of `Association: No`, corroborated 77/77) — *unless* rung 1 said `Yes`, in which case `None`.
3. **`monthlySalesCharges * 12`** — derived cross-check, and amount-of-last-resort.
   - When both rung 2 and `monthlySalesCharges` are present, they must reconcile within $1 after `/12` rounding; on disagreement **`charges` wins** (it is the source; `monthlySalesCharges` is derived from it).
   - `charges` missing/empty but `monthlySalesCharges > 0` → use `monthlySalesCharges * 12`.
   - `charges` missing/empty **and** `monthlySalesCharges` missing-or-0 → this `0` is a sum-over-nothing ambiguity, **not** confirmed absence → fall through.
4. **`parse_hoa_from_description`** — regex fallback, only when every structured signal was absent/ambiguous. Covers the 7 prose listings and any degenerate future feed. A structured **0.0** from rungs 1–2 beats a positive regex hit (structured is authoritative; where both ever resolved, they agreed).
   - All rungs exhausted → `None`.

`detailedInfo.keyDetails["HOA Fees"]` (`"-"` vs `"$X / year"`) is a display-layer duplicate of the same data — usable as a manual cross-check, never a rung.

The three-state semantics (`None` unknown / `0.0` confirmed none / positive annual fee) and the tuned `score_hoa` curve are **unchanged**: `WEIGHT_HOA=0.045`, `HOA_MAX_PENALTY=50.0`, `HOA_HALF_PENALTY_AT=3850.0`, `HOA_PENALTY_STEEPNESS=2.6`, `HOA_NO_FEE_BONUS=2.5`. The curve was tuned correctly; only its input was starved.

### 2. Property tax — capture now, display-only (recommendation)

New field `Listing.tax_annual: float | None = None` (`None` = unknown; same nullable pattern as `hoa_annual`, though tax has no meaningful confirmed-zero state — a `0.0` would just be suspicious data). Extraction in `structured_fields.extract_tax_annual(obj)`:

1. `charges[chargeType==0].chargeAmount` (sum, though only one entry has ever been observed) — numeric, 85/85, matches the Compass-displayed figure.
2. Fallback: parse `detailedInfo.assessorDetails.assessorInfo.propertyTax.tax` (`"$4,407"` → `4407.0`) via a small `parse_dollars(text) -> float | None` helper.
3. Neither → `None`.

**Recommendation: do NOT score it yet.** Reasons, in order of weight:

- **Double-counting.** HOA already carries the "recurring cost of ownership" signal at 0.045, and price already has its own dedicated lens (`value_score`, points per $100k — deliberately kept *out* of the composite per `docs/journal/decisions.md` 2026-08-16). Property tax correlates strongly with price, so a tax sub-score would mostly re-penalize expensive homes twice.
- **The number is owner-contaminated.** 15/85 assessor-vs-MLS disagreements, up to ~3x, consistent with exemptions that vanish at transfer. Scoring would reward listings whose *current owner* is tax-exempt.
- **Modest spread.** Corpus taxes run ~$750–$4,650/yr; after removing the exemption artifacts the honest spread is narrower still, and a well-calibrated sub-score would move composites less than its calibration risk warrants.

Surface it in `listings.csv` and the gallery (e.g. `Taxes $4,407/yr`) so it informs human review immediately. If it ever does enter the rubric, the right shape is a *combined carrying-cost* factor — fold HOA and tax into one monthly figure (`monthlySalesChargesInclTaxes` is Compass's ready-made version, present 85/85) and score *that* on one curve, rather than stacking a second independent cost factor — recorded here as the future direction, not this plan.

### 3. Grade-split sqft — store both; score on finished area (recommendation)

New fields, both nullable to preserve source semantics (a deliberate second exception to the 0-sentinel convention, same justification as `hoa_annual`):

- `Listing.sqft_above_grade: int | None = None` — `None` = field absent (unknown; 0/85 today).
- `Listing.sqft_below_grade: int | None = None` — `None` = field absent = **no basement** (verified against `Basement: No`, 11/11); `0` = basement present but zero finished below-grade area. The distinction is real data worth keeping for display and any future basement signal.

**Recommendation: switch `score_sqft`'s input to finished area** = `sqft_above_grade + (sqft_below_grade or 0)`, falling back to `squareFeet` when `sqft_above_grade` is missing (defensive only; 85/85 present today). Implemented as a small helper (`finished_sqft(listing) -> int` in `src/scoring.py`); `score_sqft(sqft, min, max)` itself is unchanged — `score.py` feeds it and `compute_collection_stats` the finished values instead. `has_incomplete_data`'s `not listing.sqft` clause stays keyed to `squareFeet`, since that is the last-resort fallback.

Why: the sub-score exists to compare *living space* like-for-like, and it is currently wrong on 45/85 listings, by up to 1,560 sqft. The cost is real and accepted: the scored figure no longer matches the `squareFeet`/$-per-sqft shown on Compass and in our own CSV/gallery — acceptable because `score_sqft` is an internal comparator, not a displayed price metric; `sqft` remains stored and displayed everywhere as today, with finished area shown alongside it (gallery: `2641 sqft (1739 finished)`). This *will* reshuffle rankings — that's the point — so the backfill step includes a before/after `ranked_report.csv` diff for review, same as the weight-rebalance check in the HOA plan.

### 4. `has_incomplete_data`

No code change. The `listing.hoa_annual is None` clause was correct in intent and stays; what changes is its firing rate. Today it makes the flag near-useless (82/85 flagged, almost entirely from structurally-unknowable HOA). After the re-scrape, `hoa_annual` should be non-NULL on 85/85 (77 confirmed-zero + 8 positive, per the probe), and the flag reverts to meaning what it says: a listing actually missing a scoring input (commute, sqft, year_built, beds — or a genuine `Association: Yes` with undisclosed fee). Verify the post-backfill count drops from 82 to the commute/field-gap residue (expected: low single digits) and record the number.

## Schema / Storage Changes

- **`src/models.py`** — add `tax_annual: float | None = None`, `sqft_above_grade: int | None = None`, `sqft_below_grade: int | None = None`. Defaulted, so `Listing(**data)` still loads every pre-existing `data/listings/*.json` file.
- **`src/db.py` `_SCHEMA`** — `listings` gains `tax_annual REAL`, `sqft_above_grade INTEGER`, `sqft_below_grade INTEGER` (all nullable, no DEFAULT — NULL is meaningful for all three, same reasoning as `hoa_annual`'s existing comment). No `scores` changes (no new sub-score).
- **`init_db()`** — three matching `ALTER TABLE listings ADD COLUMN` guards, following the existing pattern.
- **`upsert_listing()`** — three new columns in the INSERT column list and value tuple.
- **`src/turso_sync.py` — no code changes** (`ensure_schema`/`_migrate_missing_columns` re-parses `_SCHEMA` generically), but extend its hardcoded migration *test* with the new columns, as was done for `hoa_annual`.
- **`src/store.py` — no code changes** for the new fields (asdict/`Listing(**data)` round-trip).
- **`score.py`** — `_row_to_listing()` passes the three new columns through; sqft stats and `score_listing` input switch to `finished_sqft`.
- **`src/csv_writer.py`** — add `tax_annual`, `sqft_above_grade`, `sqft_below_grade` to `FIELDNAMES` (blank when `None`, preserving the null-vs-zero distinction exactly as `hoa_annual` does).
- **`src/gallery.py`** — add a taxes line (`Taxes $4,407/yr`, omitted when unknown) and extend the facts line with finished area when the split is present.
- **Raw payload retention (recommended, severable)** — the root cause of this whole re-scrape is that `parse_collection_response`/`scrape_listing` discard the raw object after parsing. Add an optional `on_raw: Callable[[str, dict], None]` hook to `parse_collection_response`/`fetch_collection_listings` (and call it in `scrape_listing`'s path), which `scrape.py` wires to a `save_raw_payload(data/raw, listing_id, obj)` in `src/store.py`. `data/` is gitignored; ~45 KB x 85 ≈ 4 MB. The next unparsed-field discovery then costs a re-parse, not a re-fetch.

### Backfill — a re-scrape IS required this time

Unlike `backfill_hoa.py`, the new inputs (`price.charges`, `monthlySalesCharges`, `assessorDetails`, grade-split sqft) **do not exist in `data/listings/*.json`** — those files are `asdict(Listing)` of already-parsed fields only; the raw payload was discarded at scrape time. There is nothing on disk to re-parse. (`data/raw-captures/payloads/*.json` holds all 85 raw payloads from the investigation — usable to *verify* the re-scrape and to build fixtures, never as the backfill source.)

Procedure, in order (same double-sync-avoidance logic as the HOA plan's migration ordering):

1. Ship all code below; the first `get_connection()` migrates the local DB.
2. `python scrape.py --force --skip-photos` — one collection API pass (~1–2 paginated requests) re-parses all ~83 collection listings with the new extractor and rewrites both DB rows and store JSON (`--force` is what makes the JSON store rewrite happen for already-scraped listings; `--skip-photos` because photos are already on disk); the 2 pinned `LISTING_URLS` re-scrape their detail pages, exercising the `Association` rung live.
3. Verify against the probe: exactly 8 listings with `hoa_annual > 0` (including 3123 W 105th Ct ≈ 781.56, 8052 Fenton 864, 10191 Zenobia 1000), 77 at 0.0, 0 NULL; `tax_annual` non-NULL on 84–85; `sqft_above_grade` on 85.
4. `python score.py`, diff `ranked_report.csv` before/after (expect movement from both the 8 HOA penalties and the finished-sqft switch), review, then `publish.py`.

`backfill_hoa.py` is not modified — it is a completed one-off, now historical; its "no re-scrape needed" docstring documents the state of knowledge at the time.

## Test Plan (TDD order — tests before implementation)

### 0. Fixture replacement — the root-cause fix

The existing fixtures are hand-trimmed to the parser's own field list, which is precisely how a 164-leaf payload got certified HOA-free. Replace/augment with **real captured payloads**, copied from `data/raw-captures/`:

- `tests/fixtures/collection_page_full.json` — a real paginated-response envelope with 2–3 *complete* `listingData` objects: one HOA-positive (10191 Zenobia, chargeType:2 = 1000), one no-HOA with a populated basement split, one with `belowGradeTotalAreaSquareFeet` absent (no basement). Source: `collection_raw_capture.json`.
- `tests/fixtures/detail_condo_association_yes.json` — the 3751 W 136th Ave two-HOA positive control (Association Yes, Fee Total Annual $3,066). Source: `probe2_condos.json`.
- `tests/fixtures/detail_sfh_association_no.json` — an SFH detail payload with `Association: No` and no chargeType:2 entry.

Trimming rule, stated in a `tests/fixtures/README.md`: payloads are kept **whole**, minus only named bulky/PII keys (`media` truncated to 2 entries, `fullContacts`/`history` removed) — removal by denylist of named irrelevancies, **never** by allowlist of currently-parsed fields. Keep `canossa_dr_listing.json`/`collection_response_sample.json` for the existing tests that reference them.

### 1. `tests/test_structured_fields.py` (new)

HOA chain:
- `test_association_no_returns_confirmed_zero`
- `test_association_yes_with_charge_returns_annual_amount`
- `test_association_yes_without_any_amount_returns_none_not_zero` — the residual-risk guard; the one place "Yes + $0" could silently become a no-HOA bonus.
- `test_multiple_hoa_charges_sum` — two chargeType:2 entries → their sum (real case: $2,796 + $270).
- `test_tax_charge_present_but_no_hoa_charge_returns_zero` — the collection no-HOA signature.
- `test_empty_charges_and_zero_monthly_sales_charges_returns_none` — sum-over-nothing zero is ambiguity, not absence.
- `test_monthly_sales_charges_used_when_charges_missing` — `65.13` → `781.56`.
- `test_charges_wins_over_disagreeing_monthly_sales_charges`
- `test_description_fallback_only_when_structured_absent`
- `test_structured_zero_beats_positive_description_regex`
- `test_extracts_hoa_from_real_collection_fixture` / `test_extracts_hoa_from_real_association_yes_fixture` / `test_zero_from_real_association_no_fixture` — the regression tests the trimmed fixtures made impossible.

Tax:
- `test_tax_annual_from_charge_type_zero`
- `test_tax_annual_falls_back_to_assessor_formatted_string` — `"$4,407"` → `4407.0`.
- `test_tax_annual_none_when_no_source`
- `test_parse_dollars_handles_commas_cents_and_garbage`

Sqft:
- `test_grade_sqft_extracted_from_real_fixture`
- `test_below_grade_absent_returns_none_not_zero`
- `test_below_grade_zero_preserved_as_zero`

### 2. `tests/test_models.py`
- `test_listing_new_structured_fields_default_to_none_for_backward_compatible_construction`

### 3. `tests/test_listing_parser.py`
- `test_parse_listing_object_from_full_collection_fixture_extracts_hoa_tax_and_grade_sqft`
- `test_parse_listing_object_prefers_structured_hoa_over_description`
- Update `test_parse_listing_object_missing_optional_fields_defaults_safely` for the three new `None`s.
- The two existing description-based HOA tests still pass unchanged (fallback path).

### 4. `tests/test_db.py`
- `test_upsert_listing_roundtrips_tax_and_grade_sqft_nulls_and_values` (None stays None, not 0)
- `test_init_db_migrates_existing_listings_table_missing_structured_columns`

### 5. `tests/test_turso_sync.py`
- Extend `test_ensure_schema_migrates_a_mirror_created_before_a_column_existed` with the three new columns (test-only change, per precedent).

### 6. `tests/test_scoring.py`
- `test_finished_sqft_sums_above_and_below`
- `test_finished_sqft_treats_absent_below_grade_as_zero`
- `test_finished_sqft_falls_back_to_total_sqft_when_above_grade_missing`
- `test_score_listing_scores_sqft_on_finished_area_not_total` — the regression for the unfinished-basement inflation.
- All existing `score_hoa` tests pass **untouched** — the curve is not changing, and their surviving unchanged is itself the assertion.

### 7. `tests/test_csv_writer.py` / `tests/test_gallery.py`
- CSV: new columns present, blank for `None`, values round-trip.
- Gallery: taxes line rendered for a known value, omitted when unknown; finished area shown when split present.

### 8. Raw payload retention (if taken)
- `tests/test_store.py::test_save_raw_payload_writes_json_keyed_by_listing_id`; `tests/test_scraper.py::test_parse_collection_response_invokes_on_raw_per_listing`.

## Staged Steps (dependency order, one commit each)

1. **Real-payload fixtures** + `tests/fixtures/README.md` + the fixture-based additions to `tests/test_listing_parser.py` that only assert *current* behavior against the full payloads (proves fixtures faithful before anything changes). Files: `tests/fixtures/*`, `tests/test_listing_parser.py`.
2. **`src/structured_fields.py` — HOA chain** + `tests/test_structured_fields.py`; rewrite `src/hoa.py`'s now-false header comment and demote its docstring to fallback status. Depends on 1.
3. **`src/structured_fields.py` — tax + grade sqft extractors** + tests. Depends on 1.
4. **`src/models.py`** — three new fields + `tests/test_models.py`.
5. **`src/listing_parser.py`** — wire `extract_hoa_annual`/`extract_tax_annual`/grade-sqft into `parse_listing_object` + parser tests. Depends on 2–4.
6. **`src/db.py`** — schema columns, `init_db` guards, `upsert_listing` + `tests/test_db.py`; extend `tests/test_turso_sync.py`. Depends on 4.
7. **`src/scoring.py`** — `finished_sqft` helper, `score_listing` sqft input switch + `tests/test_scoring.py`. Depends on 4.
8. **`score.py`** — `_row_to_listing` new columns; sqft stats/input use `finished_sqft`. Depends on 6–7. No test file (untested-orchestration precedent).
9. **`src/csv_writer.py` + `src/gallery.py`** + tests — surface tax and finished sqft. Depends on 4; independent of 5–8.
10. **Raw payload retention** (recommended, severable) — `src/store.py::save_raw_payload`, `on_raw` hook in `src/scraper.py`, wiring in `scrape.py` + tests. Independent of 5–9.
11. **Backfill run** — `python scrape.py --force --skip-photos`; verify 8/77/0 HOA split and the three named damage cases; `python score.py` with before/after `ranked_report.csv` diff; `publish.py`. No new script needed.

## Open Questions / Risks

- **`chargeType == 1` has never been observed.** If it appears (special assessment? co-op fee?), the chain ignores it silently. Acceptable for now; the raw-payload retention step is what makes it discoverable later.
- **`Association: Yes` with a $0/undisclosed fee** remains theoretical. The chain returns `None` (unknown) for it by design; if one ever appears, confirm the neutral-scoring outcome is what's wanted.
- **`paymentFrequentType != 3` never observed.** The extractor should treat any non-3 value as "do not trust this aggregation" → fall through, rather than guessing a multiplier — same conservative posture as the regex's no-cadence rule.
- **Tax-source disagreement (15/85, up to ~3x)** is *why* tax stays display-only, but the displayed MLS figure could itself be stale for a re-assessed year. If tax ever scores, resolve the exemption question first, and prefer a combined carrying-cost factor over a second independent cost weight (double-counting risk flagged above).
- **The finished-sqft switch reshuffles ~half the rankings.** Intended, but the before/after diff in step 11 is a review gate, not a formality — if a known-good listing craters, the fallback question is whether unfinished basement space deserves partial credit (a future `UNFINISHED_SPACE_WEIGHT < 1` blend), not a revert to total footprint.
- **The re-scrape depends on Compass access working today** (auth state, API shape). If the collection API has drifted since the captures, the fixtures from step 1 date the last-known-good shape.
- **Deliberately out of scope, noted for later:** `garageSpaces` (differs from `totalParkingSpaces` on 33%), `detailedInfo.schools` (structured, GreatSchools-rated, unused), `detailedInfo.outdoorSpace` (a structured list that could replace the prose keyword matching `score_outdoor` currently runs against 78 empty descriptions), `mlsStatus`, `lastPropertyClosePrice`, `monthlySalesChargesInclTaxes` as a display line. `seniorCommunityYN`/`waterfrontYN`/`latestTransaction` are constant-False/unusable across the corpus.
