# home-search: v0 "scrape + view" design

## Context

Ben works with a realtor (Anne Singleton, Compass) who shares a Compass "collection" —
a saved search of ~200–250 homes matching Ben's criteria. Reviewing that many listings
one at a time inside Compass's UI is slow. The longer-term goal is to have Claude help
score homes against Ben's preferences (based on labeled examples), but that requires
real scraped data to work from first.

This spec covers **v0 only**: get structured data and photos out of Compass and into a
form Ben can actually browse and reason about. No AI scoring in this phase.

Future phases (not in this spec):
- **v1 — scoring**: rubric + labeled-example-based scoring via the Claude API, using the
  yes/no verdicts Ben provides after browsing v0 output.
- **v2 — continuous system**: scheduled scraping, persistent storage, dashboard. Only
  pursued if v1 proves useful.

## Known example listings

Six homes have already been toured. One was a clear yes; the rest were noes for reasons
Ben will provide once he can browse the scraped data:

| Address | Verdict |
|---|---|
| 2765 Canossa Dr, Broomfield, CO 80020 | **YES** |
| 960 E 9th Ave, Broomfield, CO 80020 | no |
| 10538 Kipling Pl, Broomfield, CO 80021 | no |
| 5012 W 77th Dr, Westminster, CO 80030 | no |
| compass.com/listing/2130651237632606465 | no |
| compass.com/listing/2120506603298373729 | no |

These will seed the v1 rubric/examples once scoring is designed.

## Goals

- Scrape listing data (structured fields + photos) from Compass, authenticated as Ben.
- Support two input modes: a shared collection URL (bulk, ~200-250 listings) and
  individual listing URLs (ad hoc, like the two above).
- Produce output Ben can actually use today: a CSV for sorting/filtering in Google
  Sheets, and an HTML gallery for looking at photos without reopening Compass.
- Be resumable — a 250-listing scrape is slow and Compass may rate-limit or challenge
  the browser session, so a rerun should not restart from zero.

## Non-goals (v0)

- No AI/Claude scoring of listings.
- No commute/location analysis.
- No scheduling, database, or persistent server component.
- No React dashboard.

## Tech stack

- **Language**: Python 3.10+
- **Browser automation**: Playwright (chosen over Selenium — better session/auth-state
  persistence so Ben doesn't re-login every run, and generally lower bot-detection
  fingerprint out of the box)
- **Config**: `.env` via `python-dotenv`, mirroring the pattern used in Ben's other
  local tools (single entry point, `.env.example` always current, no hardcoded secrets)
- **Output**: `csv` (stdlib) + a single self-contained HTML file (Jinja2 or simple
  string templating — no build step, no server)

## Architecture

```
login → scrape (collection URL and/or listing URLs) → normalize → download photos
      → write CSV + write HTML gallery
```

### Auth

Ben logs into `compass.com` with email/password (already confirmed this is a normal
email+password form, not SSO). The scraper:
1. Launches Playwright, navigates to the collection URL (or a listing URL).
2. If redirected to a login screen, fills email/password from `.env` and submits.
3. Persists the authenticated browser storage state to disk (e.g.
   `data/.auth/compass_state.json`, gitignored) so subsequent runs skip login entirely
   until the session expires.

### Input modes

1. **Collection mode**: given `COMPASS_COLLECTION_URL`, navigate to it, scroll/paginate
   until all listing cards are loaded, and collect each listing's detail-page URL.
2. **Listing mode**: given one or more individual Compass listing URLs directly, via
   `LISTING_URLS` in `.env` (comma-separated), scrape each directly without needing
   the collection page.

Both modes converge on the same per-listing scrape step.

### Per-listing scrape

For each listing detail page, extract:
- Address (street, city, state, zip)
- Price (current listed price)
- Beds, baths, square footage, lot size, year built
- Description text
- Amenities / feature hashtags (Compass exposes these — e.g. "Renovated Kitchen",
  "Private Yard", "Walk-in Closet")
- All photo URLs (full carousel, not just the first N — cost isn't a concern in v0
  since there's no AI call yet)
- Canonical listing URL and a stable listing ID (Compass exposes one in the page URL)

### Photo download

Every photo URL for a listing is downloaded to `data/photos/<listing-id>/NN.jpg`
(numbered by carousel order). Downloads are skipped if the file already exists,
which is what makes reruns cheap.

### Output 1: CSV

One row per listing, columns: address, price, beds, baths, sqft, lot size, year built,
description, amenities (semicolon-joined), listing URL, local photo folder path.
Written to `data/listings.csv`. Ben opens this manually in Google Sheets — no auth or
API integration with Sheets in v0.

### Output 2: HTML gallery

A single static file (`data/gallery.html`) with one section per listing: a photo grid
(all downloaded photos, referencing local file paths — this file is meant to be opened
locally, not shared/hosted) plus the same structured details as the CSV row. No
JavaScript framework, no build step — plain HTML/CSS generated by the scraper, openable
by double-clicking.

### Resumability / state

A `data/state.json` (or similar) tracks which listing IDs have been fully scraped
(data extracted + all photos downloaded). On rerun, listings already marked complete
are skipped; the CSV and HTML gallery are regenerated from all previously-scraped data
plus any newly scraped listings — so a full regeneration is always cheap even if the
underlying scrape was incremental.

## Config (`.env`)

| Variable | Purpose |
|---|---|
| `COMPASS_EMAIL` | Login email |
| `COMPASS_PASSWORD` | Login password |
| `COMPASS_COLLECTION_URL` | Shared collection URL (optional if only using listing mode) |
| `LISTING_URLS` | Comma-separated individual listing URLs (optional) |

`.env.example` kept current; `.env` and `data/` (photos, state, auth) are gitignored —
this is personal scraped data and session state, not something to commit.

## Error handling

- Login failure → fail fast with a clear message.
- A single listing failing to scrape (page structure changed, network error) → log and
  skip; does not abort the whole run.
- Compass bot detection / CAPTCHA-style challenge → surfaced clearly to Ben rather than
  silently hanging; known risk, no automated workaround planned for v0. If scraping
  becomes consistently blocked, the fallback is Ben manually saving a listing page's
  HTML for the parser to run against directly (parsing logic is decoupled from the
  live browser fetch, so this stays possible without a redesign).
- Photo download failure for an individual photo → log and skip that photo, don't fail
  the listing.

## Testing

- Unit tests for the HTML-parsing/normalization logic using saved fixture HTML (e.g. a
  sanitized copy of a real listing page, with any personal/session data stripped).
- No automated tests against the live Compass site (too fragile against DOM changes).
- Manual smoke test: run against the two known individual listing URLs plus a handful
  from the collection before trusting a full 250-listing run.

## Open questions for v1 (not blocking v0)

- Exact rubric wording (appliances, renovation recency, backyard size/privacy, primary
  suite layout, etc.) — to be defined once Ben has browsed v0 output and can articulate
  what made Canossa Dr a yes.
- Whether commute/location judgment is handled by Claude reasoning directly (decided
  earlier) or needs revisiting once real listing addresses are in hand.
