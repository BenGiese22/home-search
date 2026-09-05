# The commute factor: what it measures, and the decision left open

*2026-09-05. Written because the commute rebuild deliberately stopped
half-way — it fixed the input and did not touch the curve — and that is a
choice someone will otherwise read as an oversight.*

## The short version

The commute factor carries **28.65%** of the composite, the heaviest single
weight in the rubric. For a year it was fed the wrong quantity, and the
symptom was invisible: every number was well-formed and plausible.

That is fixed. What is not settled is whether the **curve** consuming those
numbers is still the right curve, because it was drawn against data that no
longer exists. `ops/commute_distribution.py` exists to answer that question
with measurements instead of intuition.

**Nothing about the curve has been changed. This document is the case for
looking at it, and the reason the looking was separated from the fixing.**

## What went wrong, and why nothing caught it

The thresholds were set on 2026-08-14 from Megan's own description: about
20 minutes is ideal, about 30 is the ceiling. Those describe a **lived
commute** — a real drive, on a real morning, in traffic.

The pipeline was measuring free-flow OSRM durations: the drive at 3am on an
empty road. So the rubric was asking "how long is Megan's commute" and being
answered "how far away is this house, expressed in minutes".

The two are correlated, which is exactly why it survived a year. Every
listing got a number, the numbers were in a believable range, and the
ranking looked sensible. What it produced was **79 of 101 listings scoring
an identical 100** on the heaviest-weighted factor in the rubric. The
factor was not ranking anything. It was a constant with a plausible
disguise.

This is the same failure this project keeps producing, and the reason
`verify.py` exists: exit 0, valid data, plausible counts, wrong answer.

## What changed, and what deliberately did not

`medtronic_minutes` is now a traffic-aware duration from Mapbox, asking to
arrive at 08:15 on a Wednesday. That is the quantity the thresholds were
always describing.

**`WEIGHT_COMMUTE`, the 20/30/40 thresholds and `NEUTRAL_SCORE` were left
exactly as they were.**

That was not caution for its own sake. A change that moves a measurement
*and* retunes the thing consuming it cannot be evaluated: if the ranking
moves, nothing tells you whether the new data or the new curve did it. The
input had to land first, alone, so the curve could be judged against real
numbers rather than predicted ones.

## What the corpus looks like now

Measured 2026-09-05 across 101 listings:

```
p0    5.9    p30  13.7    p60  19.1    p90  25.7
p10  10.0    p40  15.7    p70  20.3    p100 28.0
p20  13.1    p50  17.3    p80  23.1
```

Two things fall out of that, and neither was visible before.

**Nobody in the corpus exceeds 28 minutes.** The 30- and 40-minute
breakpoints are dead code against this data. A curve documented as having
four segments has two.

**The factor still uses under half its range.**

| | |
| --- | --- |
| listings scoring exactly 100 | 70 / 101 |
| range the corpus occupies | 51.8 – 100.0 |
| of 28.65 composite points available | **13.8 are in play** |

So the rebuild made the input honest and the factor is *still* contributing
less than half its nominal weight as a discriminator.

### A number that looks like a regression and is not

The report shows the commute sub-score going from **99 distinct values to
32**. Read quickly, that says the rebuild made the factor *less*
discriminating. It is worth being precise about, because it is the opposite.

The old score was 80% Medtronic leg and 20% Denver leg, and the Denver leg
was normalised against the corpus's own min and max — a continuous function
of the collection. That is where the 99 values came from. The Medtronic
component, the part anyone was actually asking about, was pinned at 100 for
79 of 101 listings.

So the old factor had lots of variance and most of it was **not about the
commute**. It moved when other listings were added or removed. Ninety-nine
distinct values, of which perhaps twenty-two meant anything.

Thirty-two values that all describe Megan's drive is a better instrument than
ninety-nine that partly describe the composition of the collection. But the
honest summary is not "near-constant became discriminating" — it is *"a
constant plus corpus-relative noise became a real but compressed signal"*.
The compression is the open question above.

## The decision this leaves open

**Whether the 20-minute ideal should move.** There is a real argument on
both sides, and it is not a technical argument.

*Leave it.* The threshold came from Megan describing a lived commute, and a
lived commute is now exactly what is measured. 70 listings genuinely being
within a 20-minute drive is a fact about where the search is pointed, not a
bug. A rubric that says "under 20 minutes is ideal" and then ranks a
14-minute commute above an 18-minute one is inventing a preference nobody
expressed.

*Move it.* A factor weighted at 28.65% that cannot separate 70% of the
corpus is not doing the job the weight implies, and the composite is being
decided by the remaining factors whether or not that was intended.

**This is Ben and Megan's call, not the code's.** It reorders houses that
are being chosen between. What the code can do is make the trade-off legible,
which is what the report is for.

### One option that must not be taken

Normalising the commute score against the corpus's own min and max — the way
`sqft` and `room_count` are normalised.

It scores best on every "range used" metric and it is the wrong answer. It
makes a listing's score depend on which **other** listings happen to be in
the collection that week, so a house moves in the ranking with nothing about
it having changed, and a score from one run cannot be compared with a score
from another.

This is not hypothetical: the Denver leg worked exactly that way until
2026-09-05, and removing it was half the reason that change was made. Do not
reintroduce it here.

## The related open question (#29)

`NEUTRAL_SCORE` is 50, applied when a listing has no usable commute. The
intent was that one missing field should not tank a composite.

Against the current corpus, **every routed listing scores above 50**. So a
listing with no commute is not being treated neutrally — it is being ranked
beneath the entire corpus on the heaviest factor in the rubric, which is a
penalty wearing the word "neutral".

Deferred for the same reason as the thresholds: the right imputation is read
off this distribution, and it should not ride along in the change that
produced the distribution. The two candidates are the routed median and the
routed 25th percentile; the report prints both.

Nothing is in that state today (0 listings unrouted), which is why this is a
latent problem rather than an active one.

## Running the report

```bash
venv/bin/python ops/commute_distribution.py [data/snapshots/<date>]
```

Read-only. It opens with `connect()` rather than `stage_connection()`, so it
cannot reach `ensure_schema` and alter a table on the way past.

It needs a "before" side, which is why `ops/snapshot_tables.py` exists and
why it had to run before the first traffic-aware pipeline run: the commute
columns are overwritten in place, so there is exactly one moment when the old
numbers still exist.

## What the report cannot tell you

It says the ranking is **different** and **more discriminating**. It cannot
say it is **better**.

Only Ben and Megan, looking at the new top ten, can say that. That check is
the one thing in this whole rebuild that no script covers, and it is the one
that actually matters.

## See also

- `docs/journal/decisions.md` (2026-09-05) — what the rebuild changed and
  what it measured
- `docs/routing-provider-terms.md` — why Mapbox, and what happens when the
  key is revoked
- `docs/superpowers/plans/2026-09-05-commute-rebuild.md` — the plan, kept for
  the reasoning and the tasks deliberately not done
- Issue #29 — the neutral-50 imputation
