# Potential improvements

A working list of ideas for where `home-search` could go next, beyond what's
already tracked as concrete-but-unfixed technical debt in
`docs/journal/backlog.md`. This doc is for bigger-picture "what could we
add" thinking; `backlog.md` stays the place for narrow, already-diagnosed
fixes. Nothing here is committed to — it's a menu, not a plan. When an idea
here gets picked up for real, it should graduate into a proper spec under
`docs/superpowers/specs/` the same way baseline-scoring and photo-scoring
did.

## External data sources (Zillow, Realtor.com, etc.)

Ben's idea, 2026-08-29: pull in metadata Compass's own listing data doesn't
carry, by hitting other real-estate sites for the same address. Compass
currently gives us price, beds/baths/sqft, lot size, year built, parking,
description text, photos, and (as of the status/property-type work) MLS
status. What's missing that Zillow/Realtor.com/others could plausibly add:

- **Zestimate + Zillow price history** — a second, independent price signal
  and a real time-series of past listing/sale prices for the same address,
  not just "changed since we last checked."
- **School ratings** (GreatSchools data, syndicated on both Zillow and
  Realtor.com) — not covered by any current factor, and a plausible real
  scoring input alongside commute.
- **Tax assessment history** — county assessor data, often surfaced on
  Zillow/Realtor.com detail pages; useful as a sanity check against listing
  price and for property-tax budgeting.
- **HOA fees** — Compass sometimes buries this in free-text description;
  Zillow/Realtor.com often have it as a structured field.
- **Walk/transit/bike score** (Walk Score, syndicated widely) — would
  directly answer the already-open backlog idea about scoring nearby trails
  and parks, and is a real API rather than a keyword guess.
- **Flood zone / natural hazard data** — increasingly common on both sites,
  not something Compass's collection API surfaces at all.
- **Comparable "recently sold" nearby listings** — useful context for
  judging whether a price is fair, distinct from anything currently
  computed.
- **Days on market, listing history across relistings** — Compass's own
  data is a snapshot; other sites sometimes show the full history of a
  property being listed/withdrawn/relisted over time, which would make the
  Active/Coming-Soon filtering (2026-08-27) even more informative.

Real caveats worth weighing before committing to this:

- **Terms of service / access risk differs a lot by source.** Compass's
  collection API is an internal endpoint reached through an authenticated
  session Ben already has legitimate access to as the buyer's client.
  Zillow and Realtor.com are much more aggressive about blocking
  scraping and have historically pursued legal action over it (Zillow's
  lawsuit against REX, its API terms, etc.) — scraping either site directly
  carries meaningfully more risk than what this project does today.
  Consider paid, ToS-compliant data providers instead (e.g. ATTOM Data,
  Estated, RentCast, Rentometer, or Zillow's own limited public API where
  available) rather than scraping either site's pages.
- **Address matching across sources is its own small project.** Compass,
  Zillow, and Realtor.com don't share a common listing ID — matching would
  need to go through normalized street address (+ unit, city, zip), with
  the usual fuzzy-matching edge cases (abbreviations, unit formatting,
  new-construction addresses not yet indexed elsewhere).
- **Rate limiting and anti-bot posture** would need the same care already
  taken for Compass (`docs/journal/decisions.md`, 2026-08-16 entry) — an
  authenticated-looking request shape, pacing, and a cheap way to notice a
  systemic failure rather than one that fails silently.
- Start narrow: one source, one or two fields (school rating and Walk
  Score are probably the best ROI/effort ratio — both answer a real
  scoring gap and are available via reasonably access-friendly APIs), doing
  the address-matching plumbing once, then extending source-by-source once
  that foundation is proven against real data.

## Scoring rubric gaps

Mostly already surfaced by the house-tour calibration exercise
(`docs/house-tour-calibration-findings.md`) but never folded into the live
rubric (`src/scoring.py`):

- **Layout/vertical-circulation is invisible to scoring.** The single most
  repeated real rejection reason across the 7 toured houses (4 of 7), and
  nothing in the current rubric or the photo-scoring schema detects a
  disjointed multi-half-floor layout. This is the biggest known gap between
  what gets scored and what actually drives Ben and Megan's real verdicts.
  Hard to do well from photos alone — the calibration doc found Claude's
  photo-only read got this wrong on the same two houses humans rejected for
  exactly this reason. Would likely need either a floor-plan-graphic read
  (already captured as `layout_plan`, informational-only today) promoted
  into an actual score, or accepting this stays a human-judgment-only
  factor.
- **Staging detection exists but doesn't affect the score.** Both
  watermarked and suspected-unwatermarked staging are detected and stored
  (`visual_scores.watermarked_staging_detected` /
  `suspected_unwatermarked_staging`), but `condition_photo_score` doesn't
  discount for it — a heavily staged listing scores on the staged photos at
  face value. Worth a deliberate decision on whether/how much to penalize.
- **Neutral-50 commute imputation functions as a penalty, not neutral**
  (backlog.md, 2026-08-15) — a failed geocode currently ranks below every
  successfully-routed listing on the heaviest-weighted factor (30%).
- **`score_condition`'s keyword-miss fallback and `score_outdoor`'s were
  inconsistent** until the 2026-08-25 fix; worth periodically re-auditing
  the two keyword lists against real listing descriptions the way that fix
  did, since Compass's marketing copy conventions can drift.
- **`YEAR_BUILT_MIN`/`MAX` (1955–2005) is still hardcoded** rather than
  computed from the live collection like sqft and commute are (backlog.md).

## Pipeline robustness

Concrete, already-diagnosed items living in `docs/journal/backlog.md` —
listed here only as pointers so this doc gives a full picture of "things
that could be better," not to duplicate the detail:

- No retry path for listings whose geocode failed.
- No un-pin mechanism once a listing is marked `is_pinned`.
- `geocode_failed` is a misleading name (also covers routing failures).
- `get_scores()` in `src/db.py` is unused; `score.py` re-sorts in Python
  instead.
- `score.py` does an N+1 query pattern per listing (irrelevant at current
  scale).

## New capabilities

- **Scheduled, unattended runs.** Every run today (`scrape.py`, `check.py`,
  `score_photos.py`, `score.py`) is manually invoked. A cron-style schedule
  (even a simple local cron entry, or Vercel's Cron Jobs if the project
  ever moves compute off a local machine) would keep the dataset fresh
  without Ben remembering to run four scripts by hand.
- **Change notifications.** Right now `check.py` prints new
  listings/price-changes/delistings to the terminal, and only if someone's
  watching. A notification (email, SMS, or a Slack/Discord webhook) on a
  new listing crossing some composite-score threshold, or a price drop on
  an already-pinned listing, would close the loop between "the data updated"
  and "Ben actually finds out."
- **Full price history, not just latest-vs-before.** `compute_changes()`
  only ever compares the current fetch to the immediately-prior snapshot —
  there's no durable time series of a listing's price over its whole
  tracked lifetime. Worth considering a simple `price_history` table
  (listing_id, price, observed_at) purely additive alongside the existing
  price-change detection, useful for both trend display and answering "how
  long has this been sitting at this price."
- **Web viewer.** Already underway as a separate effort — the `short-list`
  project (a different repo, coordinated with via cross-session messaging
  on 2026-08-28) is building exactly this: a Turso-backed, Vercel Blob
  photo-hosted public viewer, with `publish.py` in this repo pushing local
  data to it. As of 2026-08-29 that work merged into `home-search`'s `main`
  via two PRs (`origin/main`, now a real GitHub remote) — the sync pipeline
  itself is built and merged, though its first real run against live Turso/
  Blob credentials hadn't happened as of this writing. Worth checking with
  Ben/that project before duplicating any of this effort here.
- **Duplex/multi-family filter.** Still open — no confirmed real example
  exists in the data yet to build a rule against (the one listing flagged
  as a suspected duplex turned out to be Single Family on investigation,
  2026-08-27). Revisit once a real one shows up.

## Priority note

If picking one thing to start with: **Walk Score integration** is probably
the best next step from the external-data-sources section — it's a real
API (not a scrape), it closes an already-identified scoring gap (bike
trails/parks, backlog.md 2026-08-15), and building the address-matching
plumbing for it sets up every other external-source idea in this doc to be
cheaper to add later. From the scoring-gaps section, **layout/circulation**
is the highest-value fix by evidence (4 of 7 real rejections), but also the
hardest to solve well — worth a dedicated brainstorming pass on its own
before committing to an approach.
