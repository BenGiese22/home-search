# House Tour Calibration Findings

Synthesis of `docs/house-tour-feedback.md` (Ben and Megan's actual in-person
verdicts on 7 toured homes) against two independent reads of the same
listings: `assess_six_houses.py`'s Claude photo-only assessment, and the
*current live* `src/scoring.py` v1 algorithm (keyword/year-based condition and
outdoor scoring, no vision model involved).

**Methodology caveat:** all 7 listings were scored together as their own tiny
`score.py` run, so `sqft_score` and the Denver leg of `commute_score` (both
collection-relative, min-max normalized) are normalized against *this 7-house
set only* — not the real ~117-listing collection. Treat the composite numbers
and ranks below as directionally illustrative, not as what these listings
would score once run through a real full-collection pass. `condition_score`,
`outdoor_score`, `parking_score`, and the Medtronic leg of commute are
absolute, not collection-relative, so those are read normally.

## The full comparison

| # | Address | Human verdict | Claude (photos only) | v1 composite (rank /7) | v1 condition | v1 outdoor |
|---|---|---|---|---|---|---|
| 1 | Canossa Dr | **YES** | YES ✓ | 93.0 (#1) | 97.6 | 100.0 |
| 2 | 111th Ave | NO — layout | YES ✗ | 85.2 (#2) | 89.2 | 100.0 |
| 3 | 93rd Way | NO — layout, bath count | YES ✗ | 68.6 (#5) | 88.8 | 40.0 |
| 4 | 960 E 9th Ave | NO — staging/unfinished | YES ✗ | 80.2 (#4) | 82.8 | 100.0 |
| 5 | 77th Dr | NO — genuinely dated | **NO ✓** | 54.9 (#7, last) | 14.0 | 40.0 |
| 6 | Kipling Pl | NO — virtual staging | YES ✗ | 81.5 (#3) | 94.8 | 100.0 |
| 7 | Utica St | "No, but ok" — too small | YES ✗ | 56.6 (#6) | 16.8 | 100.0 |

**5 of 7 disagree with Claude's photo-only read, and every miss runs the same
direction** — over-calling YES. The one house Claude correctly flagged NO
(77th Dr) is also the one where the v1 keyword algorithm most agrees (lowest
composite, lowest condition score) — the case where the wear was genuinely
visible and nothing was staged over it. That consistency across both
independent, differently-built scorers is itself informative: photo-based
reads (human-prompted or keyword-based) work when the negative signal is
actually visible in the photos. The failures cluster into distinct,
identifiable causes below, not random noise.

**Encouraging signal underneath the misses:** even with today's placeholder
condition/outdoor scoring, the v1 composite still ranked the actual YES house
#1 and the worst actual house dead last among these 7. The errors are
concentrated in the middle of the ranking, exactly where layout/staging/scale
factors (none of them scored today) are what actually decided the verdict.

## What's driving the misses

### 1. Layout / vertical circulation — the single biggest, most repeated cause
Houses 2 and 3's rejections were *entirely* about a disjointed split-level
layout ("spiral staircase maze of 4 half floors," "immediately throw you into
a split level stairway"), not condition — and House 4 and House 5 both
mention layout/stairwell placement as a real negative too. That's 4 of 7
houses citing this. Neither Claude's photo read nor the v1 keyword score
(nor the planned `layout_plan` field in the photo-scoring spec, which only
detects whether a floor-plan *graphic* exists) addresses circulation/flow at
all. Already logged in `docs/journal/backlog.md` (2026-08-24 entry) after
houses 2–3; this larger dataset confirms it's the dominant pattern, not a
one-off.

### 2. Staging — real and virtual — actively fools a photo-only read
Two houses, two flavors of the same problem:
- **House 6 (Kipling Pl):** Ben's own note: *"the images on the listing were
  'virtually staged'... very misleading."* Claude's independent photo
  assessment still said YES, sensing only that "some secondary bathrooms and
  the backyard feel unfinished" — it got fooled by the same virtual staging
  Ben flagged, exactly as he predicted it might.
- **House 4 (960 E 9th Ave):** Ben's note: *"felt like a staged fake
  kitchen... unfinished... noticed gloves, screws, an unfinished bathroom
  just la[y]ing around. Can't tell from the pictures."* Claude called this
  one YES too — "tastefully and thoroughly remodeled... easy, low-stress
  move-in" — the photo-only read had no way to catch what only became
  obvious in person.

This is a real, distinct gap neither the current rubric nor the photo-scoring
plan addresses: nothing currently tries to detect staging (real or virtual)
as a confidence-lowering signal on the other scores.

### 3. Room count / scale relative to buyer needs — a different kind of miss
House 7's rejection wasn't about condition or layout at all: *"3 beds and 3
baths just wasn't enough... needed one or two more rooms."* Both Claude and
the v1 keyword score treated this listing well on every dimension they
measure (Claude: YES; only real ding was a "dated late-90s" kitchen/bath
look). `src/scoring.py`'s `score_listing` never reads `listing.beds` at all —
only `sqft` is scored, and only relative to the collection's own min/max.
Total square footage and bed/bath count answer different practical questions
(open floor space vs. "do we have a guest room/office"), and this house shows
they can diverge — decent sqft, insufficient room count.

### 4. The keyword heuristics have concrete, verified false negatives (and Utica had zero renovation-keyword hits, 93rd Way had zero outdoor-keyword hits)
Checked directly against the raw listing descriptions in the DB:
- **93rd Way's** actual description says "2 newly renovated Bathrooms" (a
  real keyword hit — `condition_score` correctly landed at 88.8) but
  describes the backyard as "mature landscaping... brand new Trex deck...
  extended paver patio" — none of which literally matches
  `OUTDOOR_KEYWORDS` (`"mature trees"`, `"backyard"`, etc.). Result:
  `outdoor_score = 40.0`, the no-signal default — for the exact feature Ben's
  notes call *"the biggest selling point... it was large, spacious,
  multi-tiered and had mature trees."* This is the sharpest concrete evidence
  yet that the keyword list is too narrow, not just theoretically weak.
- **Utica St's** description ("open layout," "natural light," "premium
  landscaping") never uses any `RENOVATION_KEYWORDS` phrase, despite both
  Claude and Ben describing the home itself as solid/decent condition.
  Result: `condition_score = 16.8`, near the bottom of all 7 — even though
  condition was never the actual objection.

### 5. Attached vs. detached garage isn't distinguished
House 4: *"The not-attached garage in the backyard doesn't really make sense
for us, especially by the fact that it is the size of a two car garage but
only has one car door."* `score_parking` only checks `parking_spaces >= 2`
— attached/detached and door configuration aren't represented at all.

### 6. Lot/neighborhood context that photos don't show
House 3: a fire hydrant in the driveway corner, absent from listing photos
("likely on purpose"). House 5: a large community drainage area next to the
house, and an empty field behind it — a real negative with nothing to do
with the house itself. `commute_score` only measures travel time; nothing
scores lot context or adjacent land use.

## Recommendation (not acted on — flagged for a future rubric pass)

None of this has been implemented — these are calibration findings, not
scope changes. Worth having in view whenever the rubric (v1 keywords or the
photo-scoring vision rubric) gets revisited:
- Layout/circulation has no home in the current design at all, and is now
  confirmed as the single most repeated real rejection reason.
- Staging-detection (flagging apparent virtual staging or over-staging as a
  reason to lower confidence in the other scores) is a genuinely new
  category this data surfaced — not in the original photo-scoring spec.
- Bed/bath count as its own signal, distinct from sqft, may be worth a
  weighted factor rather than leaving `listing.beds` unused entirely.
- The `RENOVATION_KEYWORDS`/`OUTDOOR_KEYWORDS` lists have real, demonstrated
  gaps beyond their already-acknowledged weakness as placeholders — this
  reinforces (doesn't newly discover) why photo-based scoring is the
  intended replacement, not a nice-to-have.
