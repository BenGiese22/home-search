# Routing provider terms, and the decision to proceed anyway

*2026-09-05. Written because the decision is deliberate, not an oversight,
and the next person to read the code should find the reasoning rather than
rediscover the problem.*

## The short version

**Every commercial routing provider's terms prohibit what this pipeline
does.** We are proceeding with Mapbox knowingly. This document records what
the terms say, why the alternatives are not better, what the actual risk is,
and what we built to make revocation survivable.

## What the pipeline does that the terms forbid

It runs a cron every six hours, loops over ~101 listings without a human
present, requests a traffic-aware duration for each, and writes the result
into Turso where it lives indefinitely and is read by a web viewer.

Three properties of that are contentious: it is **automated** rather than
human-initiated, it is **bulk**, and it **stores** results.

## Mapbox — what we are actually using

From the Mapbox Product Terms (July 21, 2026 revision), extracted from the
PDF at `mapbox.com/legal/product-terms`.

**§1.9 Default Restrictions.** The blanket clause, not navigation-specific:

> Except as otherwise expressly permitted under these Product Terms,
> Customer shall (i) only query the Services in response to human user
> queries and human application interactions, (ii) not perform bulk or
> automated queries, (iii) not scrape or systematically download Licensed
> Map Content, (iv) only access Licensed Map Content (other than Data
> Products) directly from Mapbox APIs, and (v) not export, download, cache
> or store Licensed Map Content or other results from the Service Offerings.

**§2.7.2 Temporary Geocodes.**

> Customer shall not export, store, or cache Temporary Geocodes.

Permanent Geocodes (§2.7.3) may be stored, but there is no free tier for
them — $5 per 1,000 with a card on file.

So a scheduled job storing durations and coordinates violates (i), (ii) and
(v) of §1.9, plus §2.7.2.

## TomTom — checked, and stricter

Worth recording, because the instinct is to assume the smaller vendor is
more permissive. From `docs.tomtom.com/legal/terms-and-conditions`, which
renders client-side and needs a browser to read.

**§11.4:**

> The caching or storing of any Results shall be prohibited except that you
> may cache Results delivered by the Licensed Products provided that:
> 11.4.1. such Results may only be cached in clients where the control
> headers are present in the Result;
> 11.4.2. such Results must not be cached in clients for longer than the
> maximum age period indicated in such cache control headers […]
>
> Nothing under Clause 11.4 entitles any form of caching for the purpose of
> scaling results to serve multiple clients or users.

"Results" is defined in §1 as *"any information delivered by the Maps APIs
in response to a request and which, without limitation may include geocodes
and reverse geocodes, map data tiles and route information."* Storing a
duration in a database indefinitely is prohibited outright, not merely
time-limited.

**§11.6.1** is the harder one:

> You shall not use the Licensed Products or part thereof […] to create any
> derivative work, product or service. This prohibition includes, without
> limitation […] the creation of any secondary or derived database populated
> wholly or partially with your data and/or data supplied or created by any
> third party.

The `commute` table is precisely a derived database.

TomTom also reserves **audit rights** (§16), requires records be kept three
years, and can charge for the inspection if it finds non-compliance. Mapbox
has no equivalent.

## Google — separately disqualified

Google Routes API cannot answer the question at all. From its RPC reference,
`arrival_time` is *"ignored when requests specify a RouteTravelMode other
than TRANSIT."* Arrive-by is transit-only; the consumer Maps website does
what a person does by hand, and the API does not expose it.

Its caching prohibition and mandatory attribution would have been problems
too, and `trafficModel: PESSIMISTIC` is genuinely the best available answer
to "give me the high end of the range" — but none of that matters when the
core parameter is inert.

## HERE — unverified

Its terms render client-side and were not read. Same commercial model, so
the same clauses are likely. Not evaluated further because it offers no
compliance advantage worth the smaller free tier.

## Why Mapbox anyway

**The prohibition is universal, so it is not a differentiator.** Every map
vendor writes terms to stop customers building a database out of their data.
A pipeline that scores houses is unavoidably that. Choosing a provider on
compliance grounds would mean choosing none of them, and the alternative —
free-flow OSRM — produces a number that is wrong in a way that makes the
heaviest-weighted factor in the rubric inert (70 of 92 listings scored an
identical 100).

**Enforcement posture differs, and Mapbox's is the mildest.** Mapbox
enforces by quota: 100,000 requests a month free, against an expected usage
of roughly 200 for a full backfill and single digits a day thereafter.
TomTom explicitly reserves audit rights and a three-year record-keeping
obligation.

**The scale is genuinely trivial.** Two people, one house search, ~101
addresses, one destination that matters. There is no product, no end users,
no resale, and nothing that competes with Mapbox.

**Ben's call, recorded verbatim:** *"yes we're going against MapBox's terms
of service but it's incredibly minor and we're not doing it at much scale.
So maybe it's fine in the long run? They can disable our usage whenever."*

## The risk that actually matters is operational, not legal

Nobody is going to sue over 200 requests a month. The realistic failure is
that the key stops working — revoked, quota-exceeded, or the free tier
changes — and it fails at 3am inside a cron nobody is watching.

Without a guard, that degrades silently: new listings get no commute, the
scorer falls back to a neutral 50, and the ranking quietly becomes wrong for
exactly the listings that are newest and most interesting. That is the same
shape as every other defect this project has produced — a well-formed
success containing the wrong answer.

So the mitigations are about revocation, not about lawyers:

- **`commute_source` on every row.** A row measured under a different
  provider is treated as stale by the selector, so switching providers
  re-measures the corpus automatically rather than leaving a mixture. It
  also makes "which rows came from Mapbox" answerable in one query.
- **The provider lives behind one module.** Changing vendor is one file, one
  environment variable, and one allowlist entry in short-list.
- **`verify.py` must fail when the routing source is unreachable**, rather
  than letting the corpus half-fill. A revoked key should stop the run and
  send a notification, not produce a quieter version of the same data.
- **Free-flow OSRM remains a working fallback.** It is wrong in a known
  direction — optimistic, no traffic — which is a better failure than
  absent.

## If this needs revisiting

The honest options, in order of how much they cost:

1. **Do nothing.** Current position.
2. **Ask Mapbox.** They have a support channel and this is a sympathetic
   case; a written exception would remove the question entirely.
3. **Pay for Permanent Geocodes** ($5/1K) to make the coordinate half
   compliant. Does not fix the routing half.
4. **Store only what is not a Result** — keep the score, discard the
   duration. Loses the number Megan actually reads.
5. **Go back to free-flow OSRM** and accept an inert commute factor.

None are worth doing today. All are cheaper than discovering the problem
later without having written it down.
