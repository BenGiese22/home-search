# Commute rebuild — traffic-aware routing, Denver leg out of the score

Date: 2026-09-05
Status: **EXECUTED 2026-09-05.** Kept for the reasoning, the measurements and
the tasks that were deliberately not done — not as a description of the code,
which has moved on. What shipped:

| task | PR |
| --- | --- |
| T1 pre-flight | #80 |
| T2 snapshot | #81 |
| T3 `forwards` | #79 |
| T4/T5/T6 provider, schema, stage | #82 |
| T7/T8 scoring and invariants | #82 |
| T9 token allowlist | short-list#18 |
| T11 distribution report | #83 |
| T12, T13 | #82 |

T0 was settled by Ben before execution (Mapbox, knowingly — see
`docs/routing-provider-terms.md`). **T14 and T15 were not built**: T14's job
stopped being needed once `commute_source` made the migration self-triggering
(#75 closed on that reasoning), and T15 is a label change with no correctness
consequence.

Three things the plan assumed that measurement changed:

- **The corpus is 101, not 103.** #77 landed first and superseded two relists.
- **Mapbox's `arrive_by` horizon is not ~7 days.** It accepted +53, and
  returned an identical duration at every horizon — so it reads a historical
  profile rather than a forecast, which is what makes the migration
  reproducible.
- **The pre-flight passed 101/101**, so the Census fallback was deleted
  rather than kept behind Mapbox.
Issues: #72 (the ask), #29 (neutral-50 imputation), #32 (`geocode_failed`
misnamed), #75 (recompute without a shell), #69 (closed by #74 — context).
Related: short-list#17 (card now shows `medtronic_minutes`).
Baseline at planning time: `main` = `2b7e523` (merge of #74), **825 tests**.
The working tree is shared with a concurrent session: it began the day on
`bgiese/recompute-commutes-job` (no commits over `main`; one uncommitted
change to `pipeline.py` — §2.8, Task T3) and by the end of planning was on
`bgiese/unique-addresses` at `478daab` (#76 relist supersession, **838
tests**), with the `pipeline.py` diff still uncommitted and carried across
the switch. Tasks below name which of those they build on.

## 0. Summary

The commute factor carries 28.65 % of the composite and is near-inert: it is
fed free-flow OSRM durations, 70 of 92 of which fall in the flat `<= 20 min →
100` region of a curve that was drawn around a *lived rush-hour* commute.
This plan swaps the input, not the curve:

- **`medtronic_minutes` becomes a traffic-aware, arrive-by-08:15 duration**
  from Mapbox `mapbox/driving` with `arrive_by` — one request per listing, no
  max-of-three (Ben, 2026-09-05; the measured 08:00/08:15/08:30 spread is
  0.4 min).
- **The Denver leg stops contributing to the score** and becomes display
  only. It is still computed and stored, by the same provider and the same
  call shape, so the corpus never holds two kinds of number.
- **Both destinations become pinned street-address coordinates** — Megan's
  office `250 Medtronic Dr, Lafayette, CO 80026` (the scored leg) and Ben's
  occasional coworking space `3201 Walnut St #107, Denver, CO 80205` (display
  only) — instead of geocoding a POI name at every run start. The Medtronic
  fix is a **precision and reproducibility win, not a correctness win**: the
  POI geocode was 0.19 mi off.
- **Every commute row records how it was measured** (`commute_source`), the
  selector treats a row measured any other way as stale, `score.py` refuses
  to score a stale row, and `verify.py` asserts the corpus is single-source.
  A mixed corpus becomes impossible in `scores` and detectable in `commute`,
  and **the first pipeline run after deploy migrates the corpus by itself** —
  no `--force`, no separate job.
- **Mapbox's geocoder replaces Nominatim + Census** (Ben, 2026-09-05),
  *conditional* on a pre-flight showing it resolves all 103 addresses (T1).
- **`WEIGHT_COMMUTE`, the 20/30/40-minute thresholds, and the neutral-50
  imputation are untouched.** Recalibration and #29 are follow-ups decided
  from the recomputed distribution (T11), not guessed here.
- **#32 is absorbed** (`geocode_failed` means only that; routing failures get
  their own field). **#75's job is not built**; the `pipeline.py` flag
  forwarding it needed is landed anyway because Ben asked for it (T3).
- **The duplicate `5012 West 77th Drive` is already explained and being
  fixed elsewhere** (#76, commit `478daab` on `bgiese/unique-addresses`:
  two relisted properties, fetched-id-wins supersession, and a `verify.py`
  address-uniqueness invariant). This plan does not duplicate that work;
  T12 shrinks to the two interactions it has with the commute rebuild.

**One thing in this plan is not settled and must be decided before any code
merges (T0): Mapbox's Product Terms forbid storing results from Navigation
API requests and from Temporary Geocoding.** This pipeline stores both in
Turso. §2.0 quotes the clauses. It is the same clause on which the #72
research disqualified Google.

Fourteen tasks plus two optional; three are Ben's. Six can start in parallel
once T0 clears.

## 1. What changes, end to end

### 1.1 The commutes stage after this plan

```
compute_commutes.py                       (one process per pipeline run)
  │ MAPBOX_ACCESS_TOKEN required — fail fast, name the variable, never the value
  │ arrive_by = next_arrival(now_in_denver)            §2.1
  │ ids = get_listing_ids_missing_commute(conn, source=COMMUTE_SOURCE)
  │       = no row | medtronic_minutes NULL | geocode_failed | commute_source != current
  ▼
  for each listing (sequential, ≥0.2 s between requests, 429 → honour reset, retry ≤3)
     geocode (Mapbox v6 forward, structured)        ── fail → row: geocode_failed=1
     route origin → MEDTRONIC  (Directions, arrive_by)  ┐ fail → row: minutes NULL,
     route origin → DENVER_COWORKING (same)             ┘        route_error='…'
     upsert_commute(... commute_source, arrive_by, route_error)   ALWAYS a row
  exit: 0 normally; non-zero on 401/403 or when every attempted listing failed
score.py
  commute rows with commute_source != COMMUTE_SOURCE are treated as absent
  commute_score = _medtronic_leg_score(medtronic_minutes)      (Denver gone)
verify.py
  + every listing has a commute row
  + all routed rows share one commute_source, and it is the current one
  + (after T12) no two listings share (address, zip_code)
```

### 1.2 Schema delta (`commute` table)

| column | today | after | notes |
| --- | --- | --- | --- |
| `medtronic_minutes` / `_miles` | free-flow OSRM | **traffic-aware, arrive-by 08:15** | same column, overwritten; viewer needs no change |
| `denver_minutes` / `_miles` | free-flow to Union Station | traffic-aware to **3201 Walnut St** | column names kept; destination documented in code |
| `geocode_failed INTEGER NOT NULL` | set for geocode *or* route failure | **geocode failure only** (#32) | existing column, meaning narrowed |
| `commute_source TEXT` | — | **new, nullable**; `NULL` = pre-migration free-flow OSRM | the invariant key |
| `arrive_by TEXT` | — | new, nullable; the local timestamp requested, e.g. `2026-09-09T08:15` | makes a row self-describing; catches a wrong-weekday bug |
| `route_error TEXT` | — | new, nullable; short reason when a leg did not route | the honest half of #32 |
| `lat`, `lon`, `computed_at` | | unchanged | |

All new columns are nullable with no `DEFAULT` — see §2.3 for why that is
mandatory here.

## 2. Decisions and rationale

Each states what was **verified** (read in code or docs on 2026-09-05) and
what is **assumed**.

### 2.0 Decision gate: Mapbox's terms and this pipeline's storage

**Verified**, from *Mapbox Product Terms (2026-07)*, fetched 2026-09-05:

> **2.10.1 Navigation APIs.** Customer shall not export, download, cache or
> store results from any request to a Navigation API.

> **2.7.2 Temporary Geocodes.** Customer shall not export, store, or cache
> Temporary Geocodes. […]
> **2.7.3 Permanent Geocodes.** Customer may permanently store Permanent
> Geocodes […]

> **3.51** "Navigation APIs" means Mapbox's navigation service APIs as
> described in Mapbox documentation. **3.71** "Temporary Geocode" means a
> Geocode other than a Permanent Geocode.

The Directions API is documented under `docs.mapbox.com/api/navigation/`.
Writing `medtronic_minutes` to Turso is storing a Navigation API result;
writing `lat`/`lon` from the free geocoder is storing a Temporary Geocode.
Permanent Geocoding has no free tier ($5 / 1K, card on file — pricing page,
2026-09-05). The #72 research ruled Google out partly because its ToS forbids
storing durations, "which is exactly what this pipeline does". Mapbox's terms
say the same thing; the research did not check them, and the decision
"Provider: Mapbox" was made without this fact.

This plan does not make the call. It isolates the provider behind one module
(T4) so the decision changes one file, one env var and one allowlist entry,
and it asks Ben to choose in **T0**:

| Path | What it means | Cost |
| --- | --- | --- |
| **M — Mapbox, knowingly** | Accept the terms risk for a two-user, ~400-request/month personal tool; record the acceptance in `decisions.md` so it is a decision, not an oversight | none in money; a documented terms violation |
| **T — TomTom** | Same code shape (`arriveAt`, coordinates in, seconds out; geocoder on the same key). Rejected in #72 for quota (20K/mo vs ~400/mo actual use — not a constraint) and for 429s on three unpaced requests (a 0.25 s pacer, which T6 has anyway, makes 206 calls ≈ 1 min) | **its storage terms are unverified** — the terms pages render client-side and could not be fetched today; T0 reads them |
| **N — keep OSRM** | Do the Denver removal and the invariants only | the factor stays inert; not recommended |

If T0 picks **T**, every task below stands; T4's adapter targets TomTom, the
env var is `TOMTOM_API_KEY`, and T1's pre-flight runs against TomTom's
geocoder. Nothing else in the plan is provider-specific. If TomTom's terms
also forbid storage, M with eyes open is the honest remaining choice.

### 2.1 One request per listing, arrive by 08:15 on a pinned mid-week weekday

**Settled by Ben:** one call. The plan's own assessment agrees and adds why:
the 0.4-minute max-of-three delta is ~2 % of a 20-minute commute, inside any
provider's noise, cannot reproduce Google's 18–26 range (that range is a
percentile spread, not an arrival-time spread), and would have tripled the
partial-failure surface (one of three calls fails — then what?). Quota was
never the argument: 309 calls against 100K/month is nothing.

**The `arrive_by` rule** (`next_arrival(now)` in `src/commute.py`, pure,
tested):

1. Take `now` in `America/Denver` (stdlib `zoneinfo`; add `tzdata>=2024.1`
   to `requirements.txt` so the sandbox image's Python 3.14 has zone data
   regardless of the OS package — cheap insurance, verified locally that
   `ZoneInfo("America/Denver")` loads on the desktop).
2. Candidate = the **next Wednesday at 08:15 local that is at least 2 hours
   ahead**. A cron firing Wednesday 03:00 asks about that same morning; one
   firing Wednesday 15:00 asks about the following Wednesday.
3. Emit it as `YYYY-MM-DDThh:mm` with no offset. **Verified** in the
   Directions docs: that format is interpreted in the *destination's* local
   time zone for `arrive_by`, so no offset arithmetic and no DST edge.

Why a pinned weekday rather than "next weekday": every listing in the corpus
is then measured against the same historical profile, so a listing computed
on a Friday-fired run and one computed on a Monday-fired run are comparable.
Wednesday is the conventional representative day. **Assumed:** Mapbox accepts
`arrive_by` up to ~7 days out (T1 confirms; if it does not, the rule falls
back to "next weekday ≥ 2 h ahead" and the plan notes the loss of
comparability). The docs do not state a horizon or a must-be-future rule; the
rule guarantees a future time regardless.

The requested timestamp is stored on the row (`arrive_by`), so a run that
asked about a Saturday is visible in the data rather than inferred.

### 2.2 Geocoding: Mapbox replaces the chain, after a pre-flight; destinations are pinned

**Settled by Ben:** Mapbox's geocoder (v6 forward, structured input —
**verified** the endpoint accepts `address_number`, `street`, `place`,
`region`, `postcode` and returns `coordinates.accuracy` of `rooftop | parcel
| point | interpolated | approximate`; default limit 1,000 req/min) becomes
the only geocoder. Nominatim and the Census fallback go.

Two things the plan carries for that decision:

- **Pre-flight before deletion (T1).** Census resolved 11 addresses Nominatim
  could not (#74), and today returned *no match* for `250 Medtronic Dr` — a
  street address it should know. So neither keyless geocoder is trustworthy,
  and Mapbox has not yet been shown to resolve all 103 either. T1 geocodes
  every address in `listings` plus both destinations, reports accuracy per
  row and the distance from the stored `lat`/`lon`, and **the chain is
  deleted only if it is 103/103 at `rooftop|parcel|point`**. If Mapbox
  misses any, Census stays as a fallback behind Mapbox rather than shipping a
  regression, and T4 keeps `geocode_census` + `geocode_with_fallback`.
- **Destinations are constants, not runtime geocodes.** `MEDTRONIC_LAFAYETTE`
  and `DENVER_COWORKING` become `(lat, lon)` tuples in `compute_commutes.py`
  with the street address, the source and the date in the comment. The
  coordinates come from T1's Mapbox result for each address (Ben's Nominatim
  results — Medtronic `(39.9637923, -105.0878507)`, Walnut St
  `(39.7654983, -104.9786422)` — are the cross-check). Pinning removes two
  Nominatim calls per run and a silent-degradation path that is live today:
  `resolve_destination` falls back from `"Medtronic, Lafayette, CO"` to the
  *city centroid* with only a print statement, and there are Medtronic sites
  in Louisville and Boulder for a POI search to drift to.
- **`resolve_destination` and its `fallback_address` go.** With pinned
  constants there is nothing to resolve at run start. Deleted with its four
  tests (T4 lists them).
- **Naming.** `DENVER_UNION_STATION` is wrong in value and name; it becomes
  `DENVER_COWORKING`. The `denver_*` columns keep their names — renaming
  columns in SQLite/libsql is a table rebuild, and short-list's detail page
  reads them (`app/listing/[id]/page.tsx:135`, `lib/queries.ts:123`). The
  destination change is documented at the constant and in the journal. Ben
  has no commute of his own; the code and comments say the scored leg is
  Megan's.

The 0.19-mile Medtronic correction means existing durations were
approximately right. Say so in the journal entry; do not present the
destination fix as fixing wrong numbers.

### 2.3 Schema: overwrite in place, record provenance, make staleness a first-class state

**Overwrite `medtronic_minutes`, do not add a parallel traffic column.** A
second column would need a second provider to keep the free-flow number
fresh (or it rots), a short-list change to read the new column, and it
answers a question nobody asked — Ben's manual method produces one number.
Reversibility comes from the T2 snapshot and from `--force` under reverted
code (OSRM is free), not from keeping stale numbers online.

**`commute_source TEXT`, nullable, `NULL` = pre-migration free-flow OSRM.**
Written explicitly by every `upsert_commute` from `COMMUTE_SOURCE` (a
constant in `src/commute.py`, e.g. `"mapbox-arrive-0815/v1"`). Bumping the
constant is how a future method or destination change invalidates the corpus.
*Rejected:* `NOT NULL DEFAULT 'osrm'` — true for every existing row, but a
default is a trap for the next `INSERT` that forgets the column.

**Staleness is used, not just recorded:**

- `get_listing_ids_missing_commute(conn, source=COMMUTE_SOURCE, ...)` adds
  `OR c.commute_source IS NOT ?`. The first cron run after deploy therefore
  recomputes all 103 with no operator action. `--force` survives for the
  "same source, measure again" case.
- `score.py` passes a commute row to `score_listing` only when its
  `commute_source` equals the current constant; otherwise the listing scores
  as if it had no commute (neutral, `has_incomplete_data = 1`, visibly
  flagged on the card). **This is what makes constraint 1 hold by
  construction**: the Denver-removal code cannot score a free-flow number
  because it never sees one. It also means T7 merging *before* T6 would
  neutral-score the whole corpus — §4.1 orders them.
- `verify.py` asserts single-source (T8). Between "T6 deployed" and "first
  run finished" the invariant is legitimately violated; since `verify` runs
  after `commutes` in the same run, the window is inside one run and closes
  with it.

**Nullable columns are mandatory** — constraint 4 **verified**:
`_parse_columns` in `src/turso_db.py` strips only `PRIMARY KEY` and
`REFERENCES` from a column definition and hands the rest to `ALTER TABLE …
ADD COLUMN`; SQLite rejects `ADD COLUMN x TEXT NOT NULL` with no default
("Cannot add a NOT NULL column with default value NULL"). `ensure_schema`
runs at every `stage_connection()`, so that failure would take down every
stage at once. T5 adds a regression test that builds *today's* `commute`
table and runs `ensure_schema` over the new `_SCHEMA`.

**Migration reversibility:** revert the PRs and the old code ignores the new
columns (it `SELECT *`s and reads by name). Rows hold Mapbox minutes, which
the old 80/20 formula would score as minutes — fine as a transitional state;
to restore free-flow exactly, `compute_commutes.py --force` under the old
code re-fetches from OSRM in ~2 minutes, or T2's snapshot is re-imported.

### 2.4 The Denver leg: display only, same provider, same call

Ben's decision is "stops contributing to the score but keeps being computed
and stored for display". Three ways to keep computing it:

| Option | Verdict |
| --- | --- |
| Keep OSRM for Denver only | Keeps a second provider, the public demo server (no SLA, usage policy) and a free-flow number next to a traffic-aware one on the same page. No |
| Drop computation, leave column | Old rows keep Union Station numbers, new rows are NULL — an inconsistent detail page. No |
| **Mapbox, same `arrive_by`, second Directions call** | One code path, one kind of number, 2 calls per listing (206 for the full recompute; ~2 per new listing after). **Yes** |

The Denver leg's destination changes to the coworking space at the same time
(§2.2). Because every row is recomputed by the source-invalidation, the old
Union Station numbers are gone after the first run.

The detail page keeps showing both legs (short-list#17 changed only the
card). No short-list display change is required by this plan; an optional
label tweak is T15.

### 2.5 Scoring: commute = Medtronic leg; nothing else moves

- `score_commute(medtronic_minutes)` → `_medtronic_leg_score` or
  `NEUTRAL_SCORE`. `MEDTRONIC_LEG_WEIGHT`, `DENVER_LEG_WEIGHT`,
  `_denver_leg_score` deleted.
- `CollectionStats` loses `denver_minutes_min/max` and becomes
  `@dataclass(kw_only=True)`. **Constraint 5 verified:** the dataclass is
  constructed positionally at 12 sites in `tests/test_scoring.py` (lines 53,
  184, 199, 203, 220, 231, 241, 250, 259, 268, 299, 408); dropping the two
  middle fields would bind `10.0, 30.0` to `room_count_min/max` with no
  error. `kw_only=True` turns every one of those into a `TypeError` the
  implementer must fix by hand. `compute_collection_stats` likewise loses its
  middle positional parameter and goes keyword-only; `score.py:87` calls it
  positionally today.
- `score_listing` loses `denver_minutes`; make the parameters after
  `listing` keyword-only for the same reason.
- `has_incomplete_data` no longer includes `denver_minutes is None` — a
  display-only number cannot make the *score* incomplete.
- **Thresholds, `WEIGHT_COMMUTE`, `NEUTRAL_SCORE`: unchanged.** Settled by
  Ben, and the measured evidence says the source swap is ~2× the ranking
  effect of any threshold change. T11 produces the distribution the
  recalibration decision needs.
- **#29 (neutral-50 as a penalty): follow-up, not this change.** Three
  reasons. Today zero listings are imputed (103/103 routed after #74), so
  the change has no current effect and no before/after diff to review. The
  principled value — corpus median — depends on the *new* distribution,
  which does not exist until T10 has run. And it is a second ranking change;
  reviewing it in the same diff as the source swap muddles both. T11's report
  prints the two numbers #29 needs (median commute score; count of listings
  scoring on the fallback) so the follow-up is a decision, not a guess.
- The 2026-08-13 baseline-scoring spec's "80 % Medtronic / 20 % Denver" is
  superseded on this point; the journal entry (T13) says so rather than
  editing the spec.

### 2.6 Failure handling in `compute_commutes.py` (constraint 3, verified)

Today: `except Exception: print(...); continue` with no upsert. Under
`--force` an existing complete row whose recompute raises is left as it was,
looks complete, and is never re-selected. Under a keyed, rate-limited
provider that is a routine event, not a hypothetical. The source column
already fixes the "never re-selected" half (a stale-source row is selected
next run). T6 fixes the rest:

- **Always write a row.** Geocode failure → `geocode_failed = 1`, minutes
  NULL. Route failure or exception → `geocode_failed = 0`, minutes NULL,
  `route_error` = a short reason (`NoRoute`, `HTTP 429 after 3 retries`,
  `Timeout`). `commute_source` is written on every row so the selector
  re-picks it by `medtronic_minutes IS NULL`, not by source.
- **Retry 429 with the reset header**, up to 3 times, sleeping until the
  reset (cap 60 s). Pace at ≥ 0.2 s between requests regardless.
- **Abort the stage on 401/403** on the first request: the token is wrong
  and 103 identical skips tell nobody anything. Non-zero exit → `pipeline.py`
  stops and notifies with the stage name.
- **Exit non-zero when every attempted listing failed** (systemic outage);
  exit 0 with a printed summary when some failed (those rows carry the reason,
  score neutral and flagged, and are retried next run). Rationale: one
  listing's transient failure should not withhold fresh scores from the other
  102, and `verify` still catches a listing with *no* row.
- Print a run summary: attempted, geocoded, routed, failed-by-reason, and the
  `arrive_by` used.

### 2.7 Rate limits, runtime, and why Matrix does not help

- Full recompute: 103 geocodes + 206 Directions calls. At ≥ 0.2 s pacing and
  ~200 ms round-trips, **≈ 2–3 minutes** inside the sandbox; nothing near
  the 3-hour session. **Verified:** Geocoding v6 default 1,000 req/min.
  **Assumed:** Directions default 300 req/min (could not fetch the limits
  section today; T1 observes the rate headers and the 429 handling in T6
  makes the exact figure immaterial).
- Steady state: ~2–4 requests per new listing, well under 100 a month.
- **Matrix API: no. Verified** from its docs today: it accepts `depart_at`
  and **has no `arrive_by`**; 25 coordinates per request; 60 req/min. A
  common `depart_at` for the whole corpus asks a different question from
  "arrive by 08:15" — the 5-minute and the 26-minute routes would be measured
  against different arrival times, and the measured evidence is that short
  routes congest *more*. Also 103 listings × 2 destinations needs five Matrix
  requests anyway. Directions is simpler and the volume is trivial.

### 2.8 How the recompute is triggered

**The migration triggers itself** (§2.3): source invalidation makes the first
cron run after deploy recompute every row. That is the answer to #72's
"can we re-run all the commute times through this new flow?" — yes, and
without anyone doing anything.

**The uncommitted `forwards` work is adopted (Ben).** Read as the working-tree
diff against `HEAD` (the branch has no commits, so `git diff main...branch`
is empty — the brief's command shows nothing; the change is `git diff HEAD --
pipeline.py`). It adds `Stage.forwards: dict[str, str]`, `_forwarded()`, a
`forwarded=()` parameter on `run_pipeline`, collection in `main()`, and
`Stage("commutes", …, forwards={"--force-commutes": "--force"})`. **Verified
by reading:** both the dry-run branch and the real loop append
`_forwarded(stage, forwarded)`, `main()` collects triggers from every stage's
`forwards` and passes them to both call sites. **It has zero tests** —
`grep forward tests/test_pipeline.py` finds only the scrape-flags test. T3
lands it with tests for: dry-run argv, real argv, only the commutes stage
receives it, unknown flags are ignored, `main()` collects it.

**#75's `job=recompute-commutes` is not built now (T14, optional).** With
self-triggering migration its only use is a same-source re-measure, which has
no current cause. If it is ever built, two traps for whoever does: the job's
argv must be `["pipeline.py", "--force-commutes"]` **without `--max-age`**
(the freshness guard would skip it right after a scheduled run), and it must
be allowlisted in *both* `ops/sandbox/run.py:JOBS` and short-list's
`lib/pipeline/run-handler.ts:JOBS` (`new Set(['pipeline', 'canary'])`) or the
route returns 400.

### 2.9 #32 absorbed

`geocode_failed` narrows to its literal meaning; routing outcomes go to
`route_error` (§1.2). Consumers **verified**: only
`get_listing_ids_missing_commute` and `compute_commutes.py`'s status line
read the flag. `check.py` and `score.py` do not (the issue's acceptance list
says they do — stale). The issue also names `src/turso_sync.py`; that module
is `src/turso_db.py` now and, as the issue predicted, needs no change beyond
what `_SCHEMA` drives.

### 2.10 Verified vs assumed — consolidated

Verified today: everything quoted in §2.0; Directions `arrive_by` is
`mapbox/driving` only, accepts `YYYY-MM-DDThh:mm` in the destination's zone,
returns `duration` from historical data; Matrix has no `arrive_by`; Geocoding
v6 structured input and accuracy field; pricing free tiers (Directions 100K,
Geocoding temporary 100K, permanent none); `_parse_columns` behaviour;
`CollectionStats` positional sites; `compute_commutes.py`'s swallow-and-
continue; `run.py`/`run-handler.ts` job allowlists; `buildRunnerEnv` is an
allowlist with `REQUIRED_VARS` that throws for *every* job when one is
missing; short-list reads `medtronic_minutes` on the card and both legs on
the detail page and never reads `lat`/`lon`; the `forwards` diff has no
tests; `zoneinfo` loads `America/Denver` on the desktop.

Assumed (each has a task that confirms it): Mapbox geocodes 103/103 at
address-level accuracy (T1); `arrive_by` accepts ~7 days ahead (T1);
Directions rate limit ≈ 300/min (T1 observes headers); TomTom's storage terms
(T0 reads them); production `LISTING_URLS` does not name either stale relist
id (T12 — the local `.env` does not; checked by grepping for the four ids
only).

## 3. Contradictions found while verifying

1. **The settled provider stores results its terms say may not be stored**
   (§2.0). Not a reason to stall the code — the provider is one module — but
   a reason for T0 to exist and to be first.
2. **The brief's `git diff main...bgiese/recompute-commutes-job` is empty.**
   The work is uncommitted in the working tree; `git status` at session start
   reported `main`, `git branch` then showed the recompute branch, and by the
   end of planning a concurrent session had checked out
   `bgiese/unique-addresses` in the same tree. The `pipeline.py` diff
   survived the switch. Adopted as `git diff HEAD -- pipeline.py`; T3 must
   commit it on a branch cut from `origin/main`, not on whichever branch
   happens to be checked out.
3. **#32 lists `check.py` and `score.py` as consumers of `geocode_failed`.**
   Neither reads it. Only the selector and the stage's log line do.
4. **The Medtronic destination fix is not a correctness fix.** 0.19 mi.
5. **Census is not the reliable fallback #74 implied**: it returned no match
   for `250 Medtronic Dr`. Moot for destinations (pinned), decisive for why
   T1 must run before the chain is deleted.
6. **`gh issue view N` printed nothing for #29/#32/#75/#69 in this
   environment**; `gh api repos/…/issues/N` worked. Immaterial to the plan,
   recorded so the executing agents do not conclude the issues are empty.
7. **The brief's constraint 3 is half-solved by the source column** (stale
   rows become re-selectable); the other half — a row that says *why* — is
   T6.
8. **The duplicate address was resolved while this plan was being written.**
   #76 (filed from the same coordinate-pulling query) shows *two* properties
   duplicated — `12651 James Circle` and `5012 West 77th Drive` — as relists
   under new Compass ids, both `5012` rows pinned. Commit `478daab` adds
   detection (`duplicate_address_groups`, `find_relisted`), fetched-id-wins
   supersession through `apply_delisting` (blobs reclaimed), and
   `check_addresses_are_unique` in `verify.py`, with 13 tests. The corpus
   will be **101** listings once that runs, not 103; T2 and T11 account for
   it.

## 4. Task breakdown

Conventions: every task is one PR on a `bgiese/commute-<slug>` branch cut
from `origin/main`, conventional commits, TDD (tests first, watch them fail).
`./venv/bin/python -m pytest -q` must stay green in home-search (825 on
`main` at `2b7e523`); `npm test` and `npx tsc --noEmit` in short-list.
Sizes: S ≤ 2 h, M ≤ half a day, L ≤ a day. "Repo" says which checkout the
task touches; nothing touches both. **Nothing in any task runs
`score_photos.py`, runs the pipeline against production, or writes to Turso
except through the normal stages in T10.** Spike and report scripts open the
connection with `src.turso_db.connect()` (no `ensure_schema`) and issue only
`SELECT`s.

### 4.1 Dependency graph

```
T0 Ben: terms decision ──────────────────────────────────────────────┐ gates everything below
T1 pre-flight geocode + arrive_by horizon (needs a token)            │
T2 snapshot commute/scores/ranked_report (read-only)                 │
T3 land `forwards` with tests                        (independent)   │
T5 schema + db selector ──┐                                          │
T4 src/commute.py rebuild ┴──> T6 compute_commutes.py ──> T7 scoring (Denver out, stale-ignore)
                                                          └──> T8 verify invariants
T9 short-list env allowlist  (Ben adds the env var FIRST, then merge)
T10 Ben: rollout — merge order T5,T4,T6 then T7,T8; watch the first run
T11 distribution report (needs T2 and T10)
T12 #76 interactions only: env check + row-count note (independent; #76 itself is not this plan's)
T13 docs / journal / issues (needs T10, T11)
T14 optional: recompute-commutes job          T15 optional: short-list labels
```

**Start immediately after T0 clears, in parallel:** T1, T2, T3, T5, T9
(env var first). T4 needs T1's verdict on the fallback. T6 needs T4 + T5.
T7 and T8 need T5. **Merge order is a hard constraint:** T6 must reach
`main` before T7 (see §2.3 — T7 first would neutral-score the corpus).
T5 → T4 → T6 may be one PR if the implementer prefers; T7 + T8 may be a
second. T9 must be *deployed to production* before T6 merges, or the next
cron's commutes stage fails at the token check (loudly, with a notification
— not silently, but a wasted run).

### 4.2 Tasks

#### T0 — Decide the provider against the storage terms (Ben, manual, S)

- Read §2.0 and the two Mapbox clauses (the PDF is linked from
  `mapbox.com/legal/service-terms`). Read TomTom's terms for the equivalent
  clause (`docs.tomtom.com/legal/terms-and-conditions`, and the Routing API
  product terms if separate). Decide M, T or N.
- Acceptance: a dated entry in `docs/journal/decisions.md` stating the
  choice and the clause it was made against. If M: the sentence "we store
  Directions results and Temporary Geocodes in Turso knowing §2.10.1 and
  §2.7.2" appears in it. Everything below assumes M and names the delta for
  T where it exists.

#### T1 — Pre-flight: does Mapbox geocode the whole corpus, and how far out does `arrive_by` go? (home-search, S–M)

- Files: new `ops/spikes/mapbox_preflight.py` (spike; never imported by the
  pipeline; documented in `ops/spikes/README.md`).
- Behaviour: `SELECT listing_id, address, city, state, zip_code, c.lat, c.lon
  FROM listings l LEFT JOIN commute c …` (read-only). For every row plus the
  two destinations, call Geocoding v6 forward with structured parameters;
  print `listing_id, accuracy, match_code.confidence, distance_m from stored
  lat/lon`. Then one Directions `arrive_by` request for a fixed pair at +1,
  +3, +7 and +10 days (Wednesday 08:15 each) and print `duration` and the
  HTTP status, plus the `X-Rate-Limit-*` headers from one response.
- Acceptance (recorded as a table in the PR description and in T13's
  journal entry): count at `rooftop|parcel|point`; list of anything
  `interpolated|approximate|none`; max distance from stored coords; the
  largest horizon Mapbox accepted. **Decision rule for T4:** 103/103 at
  address-level → delete the chain; otherwise keep `geocode_census` as the
  fallback behind Mapbox. The destination coordinates for T4's constants are
  copied from this output. Quota: ~110 requests.

#### T2 — Snapshot the pre-migration tables (home-search, S)

- Files: new `ops/snapshot_tables.py` — `SELECT * FROM commute`, `SELECT *
  FROM scores` to `data/snapshots/<date>/commute.csv`, `scores.csv`
  (`data/` is gitignored). Uses `connect()`, not `stage_connection()`, so it
  cannot alter the schema.
- Acceptance: both CSVs exist with one row per listing (103 today; 101
  after #76's supersession has run — either is fine, T11 joins on
  `listing_id`); the current `data/ranked_report.csv` is copied alongside.
  **Ben runs it before T6 merges** — after the first Mapbox run the old
  numbers are gone and T11 has no "before".

#### T3 — Land `forwards` on `pipeline.py` with tests (home-search, S)

- Files: `pipeline.py` (the existing uncommitted diff, committed as-is unless
  a test finds a fault), `tests/test_pipeline.py`.
- Tests: `run_pipeline(build_plan(), runner, forwarded=["--force-commutes"])`
  → `compute_commutes.py` argv contains `--force`, no other stage's argv
  does; dry-run prints `compute_commutes.py --force` (capture stdout) and
  runs nothing; a trigger no stage declares is ignored; `main()`-level: a
  test that drives the same collection expression `main()` uses (factor it
  into `_collect_forwarded(argv)` so it is testable without running
  `main()`, which takes the lock and opens Turso); `--only=commutes
  --force-commutes` still forwards.
- Acceptance: tests green; `python pipeline.py --dry-run --force-commutes`
  emits `compute_commutes.py --force` and nothing else changes (Ben already
  observed this by hand; the test pins it).

#### T4 — Rebuild `src/commute.py` around a provider adapter (home-search, M)

- Files: `src/commute.py`, new `src/routing_mapbox.py` (the only
  provider-specific module), `tests/test_commute.py`, new
  `tests/test_routing_mapbox.py`, `requirements.txt` (+ `tzdata>=2024.1`).
- `src/routing_mapbox.py` (pure; `http_get` injected as today):
  - `geocode_address(address_parts, token, http_get) -> Geocode | None`
    where `Geocode = (lat, lon, accuracy)`; structured v6 request; treats
    `accuracy` outside `rooftop|parcel|point` as a miss (return None) so a
    city-centroid match can never become a commute.
  - `route(origin, destination, arrive_by: str, token, http_get) ->
    tuple[miles, minutes] | None`; URL exactly the working shape from the
    brief (`overview=false`, `arrive_by=YYYY-MM-DDThh:mm`, lon,lat order);
    `None` on `code != "Ok"` / no routes / malformed.
  - The token is appended by the adapter and **never** appears in a log line
    or exception message (test: an error raised for a malformed response
    does not contain the token).
  - *If T0 picks T:* same two function signatures over TomTom's
    `calculateRoute` (`arriveAt`, `computeTravelTimeFor=all`, read
    `travelTimeInSeconds`) and its Search geocoder. Nothing outside this
    file changes.
- `src/commute.py`:
  - `COMMUTE_SOURCE = "mapbox-arrive-0815/v1"` with a comment saying
    bumping it invalidates every row.
  - `next_arrival(now: datetime) -> str` per §2.1; tests for each weekday
    at 03:00 and 15:00, DST-transition week, the 2-hour buffer at 06:14 vs
    06:16 on a Wednesday.
  - `CommuteResult` gains `commute_source: str`, `arrive_by: str | None`,
    `route_error: str | None`; becomes `@dataclass(kw_only=True)`. **Verified**
    the three test files that construct it (`tests/test_commute.py`,
    `tests/test_db.py:235,267,783`, `tests/test_score_batching.py:80`) —
    two are already keyword; `test_commute.py`'s two positional
    constructions must be rewritten.
  - `compute_commute(address_parts, denver, medtronic, arrive_by,
    geocode_fn, route_fn) -> CommuteResult`: geocode miss →
    `geocode_failed=True`, no route calls; a leg that returns `None` →
    minutes NULL for that leg and `route_error` naming the leg; an exception
    from `route_fn` propagates (T6 decides what to do with it, and writes the
    row).
  - **Delete** `geocode` (Nominatim), `resolve_destination`, and — if T1
    passed — `geocode_census` and `geocode_with_fallback`. Tests deleted with
    them: `test_geocode_returns_lat_lon_from_first_result`,
    `test_geocode_returns_none_when_no_results`,
    `test_geocode_returns_none_on_malformed_result`, the four
    `test_resolve_destination_*`, the three `test_census_*`, and the three
    fallback tests (`test_fallback_is_not_called_when_the_primary_succeeds`,
    `test_fallback_runs_when_the_primary_returns_nothing`,
    `test_both_failing_reports_the_fallback_was_tried`) — 13 tests. If T1
    failed, the Census and fallback functions and their six tests stay, with
    Mapbox as the primary.
  - `route_miles_minutes` (OSRM) and its five tests go; the unit conversion
    moves into the adapter with equivalent tests.
- Acceptance: `pytest tests/test_commute.py tests/test_routing_mapbox.py`
  green; no module outside `src/routing_mapbox.py` contains the string
  `api.mapbox.com`; `grep -r nominatim src/ compute_commutes.py` is empty
  (the spike under `ops/spikes/` may keep it).

#### T5 — Schema columns and the source-aware selector (home-search, S–M)

- Files: `src/db.py`, `tests/test_db.py`, `tests/test_turso_db.py`.
- `_SCHEMA.commute` gains `commute_source TEXT`, `arrive_by TEXT`,
  `route_error TEXT` (nullable, no defaults). `init_db` gains the three
  `ALTER TABLE` guards in the existing pattern for the local sqlite path.
- `upsert_commute` writes the three fields from `CommuteResult`.
- `get_listing_ids_missing_commute(conn, retry_failed=True, force=False,
  source=COMMUTE_SOURCE)` adds `OR c.commute_source IS NOT ?` in the
  `retry_failed` branch **and** in the `retry_failed=False` branch (a stale
  row is not "covered" under either mode). `force` unchanged.
- Tests: a row with `commute_source=NULL` is selected; a row with a
  different source is selected; a current-source routed row is not; **the
  migration test** — create the `commute` table with today's column list in
  sqlite, call `ensure_schema`, assert the three columns exist and a
  subsequent `upsert_commute` succeeds; a guard test that every column
  `_parse_columns` reports for `commute` beyond today's nine is either
  nullable or carries a `DEFAULT`.
- Acceptance: tests green; `tests/test_score_batching.py`'s statement-count
  pins still hold (no new round-trips).

#### T6 — Rewrite `compute_commutes.py` (home-search, M)

- Files: `compute_commutes.py`, new `tests/test_compute_commutes.py`
  (factor the loop into `run(conn, ids, geocode_fn, route_fn, arrive_by,
  now, sleep)` so it is testable with fakes; `main()` stays thin).
- Behaviour per §2.6: token from `load_env()["MAPBOX_ACCESS_TOKEN"]`, missing
  → exit 2 naming the variable; destinations are constants
  `MEDTRONIC_LAFAYETTE = (lat, lon)` / `DENVER_COWORKING = (lat, lon)` with
  address + source + date comments; `arrive_by = next_arrival(now)` printed
  once; pacing `sleep(0.2)` between requests; 429 → sleep to reset (cap
  60 s), ≤ 3 retries, then `route_error`; 401/403 → exit 3 immediately; every
  listing ends in an `upsert_commute`; exit 4 when attempted > 0 and routed
  == 0; summary line. `--force` and `--only-new` keep their meaning.
- Tests: fakes for `http_get`; a raising `route_fn` still yields a row with
  `route_error`; 429-then-200 succeeds and slept once; 401 aborts before the
  second listing; all-failed exits 4; one-failed exits 0; the token never
  appears in captured stdout.
- Acceptance: tests green; `python compute_commutes.py` against a **local
  sqlite copy** of T2's snapshot (a fixture, not production) recomputes 103
  rows with `commute_source` set — this is the one place a real token is
  used before T10, and it costs ~310 requests.

#### T7 — Denver out of the score; stale rows are not scored (home-search, M)

- Files: `src/scoring.py`, `score.py`, `tests/test_scoring.py`,
  `tests/test_score_batching.py` (if it constructs stats).
- Changes per §2.5: `score_commute(medtronic_minutes)`; delete
  `_denver_leg_score`, the two leg weights; `CollectionStats(kw_only=True)`
  without the Denver fields; `compute_collection_stats(*, sqft_values,
  room_count_values)`; `score_listing(listing, *, medtronic_minutes, stats,
  visual_condition_score=None, visual_outdoor_score=None)`;
  `has_incomplete_data` drops the Denver clause. `score.py`: drop the
  `denver_minutes_values` comprehension; pass `medtronic_minutes` only when
  `commute["commute_source"] == COMMUTE_SOURCE`, else `None`.
- Tests: rewrite the 12 positional `CollectionStats` sites; the four
  `test_score_commute_*` become single-argument; new: a stale-source row
  scores as missing and sets `has_incomplete_data`; a current-source row
  scores; `denver_minutes=None` alone does **not** set
  `has_incomplete_data`; the composite identity test still holds.
- Acceptance: tests green; `grep -n denver src/scoring.py score.py` returns
  only the `score.py` line that passes `denver_minutes` through for storage
  (it is not stored in `scores`; expect zero hits) — i.e. the score path has
  no Denver reference left.

#### T8 — `verify.py` invariants for commutes (home-search, S)

- Files: `verify.py`, `tests/test_verify.py`.
- `check_every_listing_has_a_commute_row`: listings with no `commute` row
  → violation listing addresses ("the stage never wrote a result").
- `check_commutes_share_one_source`: `SELECT commute_source, COUNT(*) FROM
  commute WHERE medtronic_minutes IS NOT NULL GROUP BY 1`; violation if more
  than one group **or** the single group is not `COMMUTE_SOURCE`; the detail
  names each source and its count ("a mixed corpus ranks the un-recomputed
  rows on free-flow numbers").
- Both added to `CHECKS`. Tests: mixed corpus fails; all-legacy fails naming
  `None`; all-current passes; a current-source row with NULL minutes does not
  count against uniformity; a listing with no row fails the first check.
- Acceptance: tests green; `python verify.py --warn` against a local sqlite
  fixture prints both `ok` lines.

#### T9 — Allowlist the token in short-list (short-list, S) + Ben adds it to Vercel

- **Ben first (manual):** create a Mapbox token scoped to Directions and
  Geocoding only (Mapbox tokens are scope-limited; do not reuse a `pk.`
  default token with map scopes); `vercel env add MAPBOX_ACCESS_TOKEN
  production` on the short-list project. Also add it to the desktop `.env`
  — the desktop home still works and would otherwise fail the stage.
- Files: `lib/pipeline/env.ts` — add `'MAPBOX_ACCESS_TOKEN'` to
  `REQUIRED_VARS` and to the `env` object; `lib/pipeline/env.test.ts` —
  extend `complete`, add "passes the routing token through" and "throws
  naming MAPBOX_ACCESS_TOKEN when absent"; the existing "never puts a secret
  value in the error" test covers the new value automatically.
- **Verified trap:** `REQUIRED_VARS` throws for every job, including
  `canary`, so merging this before the env var exists breaks the canary too.
  Order: env var, then merge, then preview check.
- Acceptance: `npm test` and `npx tsc --noEmit` green; preview deployment's
  `GET /api/pipeline/run?job=canary` with `CRON_SECRET` returns 202 (not 500
  naming the variable).

#### T10 — Rollout and the first traffic-aware run (Ben, manual)

1. #76 (`bgiese/unique-addresses`) merged and one run completed — the corpus
   is 101 and `verify` is green before anything here changes. T2 snapshot
   taken. T9 deployed to production with the env var present.
2. Merge T5, T4, T6 (or the combined PR). Either wait for the 6-hourly cron
   or `vercel crons run "/api/pipeline/run?job=pipeline"`.
3. Watch the run's log (`cmd.logs()` via the dashboard or `sandbox connect
   home-search-pipeline`): expect `arrive_by=<next Wednesday>T08:15`, 103
   attempted, ~103 routed, exit 0; `verify` will report **one FAIL** on
   `check_commutes_share_one_source` if T8 merged before this run — it does
   not, because T8 is behind T7. If T8 is in, the run's own commutes stage
   makes it pass.
4. Merge T7 + T8. Next run: `verify: all invariants hold`.
5. `SELECT commute_source, COUNT(*) FROM commute GROUP BY 1` → one row,
   103. `SELECT COUNT(*) FROM scores WHERE has_incomplete_data = 1` ≤ 1
   (today it is 1, for a non-commute reason — #69).
- Acceptance: the card's commute minutes on short-list changed for the
  listings in the brief's table by roughly the ratios measured (145 Caria ≈
  6 min, 5012 W 77th ≈ 18 min, 7263 Deframe ≈ 24 min, 12665 W 67th ≈ 26).

#### T11 — Distribution report and the numbers for two follow-ups (home-search, S)

- Files: new `ops/commute_distribution.py` (read-only; reads Turso now and
  T2's CSVs as "before").
- Prints: before/after `medtronic_minutes` min/median/max; count and share
  of listings with `commute_score == 100`; distinct commute-score values;
  per-listing ratio after/before with min/max and the corpus median; rank
  movement (mean absolute, top-10 churn) between the snapshot's
  `ranked_report.csv` and the new `scores`, joined on `listing_id` and
  listing the ids present in only one side (the two relists #76 removes are
  expected there and are not commute movement); **for the threshold follow-up:**
  how many listings fall in each of the 20/30/40 bands; **for #29:** the
  median commute score and the count scoring on the fallback.
- Acceptance: the output is pasted into the T13 journal entry and into a
  comment on #72; a follow-up issue "recalibrate commute thresholds against
  the traffic-aware distribution" is opened only if the flat region still
  holds > 50 % of the corpus — otherwise the entry says the curve is now
  doing its job and no recalibration is proposed.

#### T12 — The duplicate address: what #76 leaves for this plan (home-search, S)

The investigation the brief asked for is done: #76 and commit `478daab`
(`bgiese/unique-addresses`) establish both duplicates as relists and ship
detection, fetched-id-wins supersession via `apply_delisting` (blobs
reclaimed), and `check_addresses_are_unique` in `verify.py`. **Do not
re-implement any of it here.** Two interactions remain:

- **Confirm nothing keeps re-pinning the stale ids.** `find_relisted` only
  counts ids in `present_listings` (the collection fetch) as live, so a
  stale id is superseded as long as it is not also being scraped by URL.
  Local `.env`'s `LISTING_URLS` names none of the four ids (checked today);
  confirm the same for the production value (`vercel env pull` into a scratch
  file, grep for the ids, delete the file). `_backfill_orphans` refreshes
  DB-pinned rows from their stored URL, but it is DB-driven, so once the row
  is deleted it stops. If a stale id *is* in production `LISTING_URLS`, the
  explicit-URL loop re-inserts it every run and the supersession deletes it
  every run — ~23 photos downloaded, uploaded and reclaimed every six hours.
  Remove the URL first.
- **`verify` sequencing.** #76's commit says the new invariant fails against
  production today. In the first run after it merges, `scrape` supersedes
  both pairs *before* `verify` runs (gated on `fetch_succeeded`), so the run
  should pass; if the collection fetch fails that run, `verify` fails
  honestly and the next run heals it. Nothing for this plan to add — but T10
  should merge #76 *before* the commute PRs, so a `verify` failure during the
  commute rollout can only mean a commute problem.
- Acceptance: production `LISTING_URLS` checked and recorded in the T13
  entry; corpus is 101 after the first post-#76 run; `check_addresses_are_
  unique` passes. The commute invariants of T8 slot in beside it in `CHECKS`.

#### T13 — Docs, journal, issues (home-search, S)

- `docs/journal/decisions.md`: one dated entry covering T0's decision (with
  the clause), the source swap, Denver display-only, the pinned destinations
  (with the 0.19 mi honesty), the self-triggering migration, the T1 and T11
  tables, and the supersession of the 2026-08-13 spec's 80/20 split.
- `.env.example`: `MAPBOX_ACCESS_TOKEN=` with a two-line comment (scoped
  token; required by the commutes stage in both homes).
- `ops/spikes/README.md`: T1's spike; note `nominatim_burst.py` is
  historical.
- Issues: close #32 from the T5/T6 PR; comment on #29 with T11's two numbers
  and the deferral reasoning; comment on #75 with §2.8 and leave it open or
  close it per Ben; comment on #72 with the T11 output and close it after
  T10 is verified; #76 is closed by its own PR, not by this plan.
- Acceptance: `docs/journal/backlog.md`'s two commute items (neutral-50,
  `geocode_failed` naming) are marked resolved/deferred with a pointer.

#### T14 — Optional: `job=recompute-commutes` (home-search S + short-list S) — deferred

- Build only if a same-source re-measure is ever wanted from the cloud.
  `ops/sandbox/run.py:JOBS["recompute-commutes"] = ["pipeline.py",
  "--force-commutes"]` (**no** `--max-age`), parametrized test in
  `tests/test_sandbox_run.py`; short-list `run-handler.ts:JOBS` gains the
  name, `run-handler.test.ts` covers it. Not in the current scope because
  §2.3 makes the migration self-triggering.

#### T15 — Optional: label the number honestly in the viewer (short-list, S)

- The card's number changes meaning from free-flow to "typical traffic,
  arrive by 8:15 Wed" and the detail page's Denver leg now points at the
  coworking space. A label/tooltip change in `app/page.tsx` and
  `app/listing/[id]/page.tsx`. Not required for correctness; the numbers are
  right without it.

## 5. Risks and rollback

| Risk | Mitigation | Rollback |
| --- | --- | --- |
| Terms (§2.0) | T0 decides with the clause in front of Ben; provider is one module | switch adapter; `--force` re-measures in ~3 min |
| Mapbox misses addresses Census resolved | T1 before deletion; keep Census as fallback if < 103/103 | n/a — decided before code |
| T7 lands before T6 | §4.1 merge order; `score.py` stale-ignore makes the failure loud (whole corpus flagged incomplete) rather than silent | revert T7 |
| Env var missing in production | T9 order: var, then merge; stage exits 2 naming the variable; pipeline notifies | add the var; next cron recomputes |
| Partial recompute (outage mid-run) | rows carry `route_error`; unrouted rows score neutral and flagged; retried next run; `verify` names the mixed sources | wait one cron |
| `arrive_by` horizon shorter than 7 days | T1 measures; fallback rule in §2.1 | change `next_arrival`, bump `COMMUTE_SOURCE` |
| Want the old numbers back | T2 snapshot; old code + `--force` hits OSRM | revert PRs; either re-import or `--force` |
| Ranking churn surprises Megan | T11 quantifies it before anyone recalibrates; thresholds are untouched by design | the curve is the same curve; only the input changed |

## 6. What must NOT be done

- Do not change `WEIGHT_COMMUTE`, the 20/30/40 thresholds or `NEUTRAL_SCORE`
  in any task here. Those are follow-up decisions with T11's numbers.
- Do not add a `NOT NULL` column without a `DEFAULT` to any table
  (`ensure_schema` will fail every stage on the live database).
- Do not remove the Denver leg from scoring on free-flow data, or merge T7
  before T6 has run once in production.
- Do not query three arrival times. Do not use the Matrix API for this.
- Do not put the token in a URL that is logged, an exception message, or a
  marker file; `run.py` never prints the environment and the adapter must
  keep it that way.
- Do not run `score_photos.py`, and do not run the pipeline against
  production from a laptop to "test" this — T10 uses the cron or
  `vercel crons run`, so the lease, the markers and the reaper see it.
- Do not rename the `denver_*` columns; short-list reads them.
- Do not skip T2. Once the first Mapbox run lands there is no "before".
