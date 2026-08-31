# Decommission runbook

Two directions, because the migration is reversible at both ends. Find the
one you need and run it top to bottom; each is independently complete.

- **[A. The cloud pipeline works](#a-cloud-works--retire-the-local-timer)** —
  retire the local timer, keep the manual path.
- **[B. The cloud pipeline does not work](#b-cloud-fails--return-to-local)** —
  return to local execution.
- **[C. Remove the automation entirely](#c-remove-the-automation-entirely)** —
  back to running scripts by hand.

Neither A nor B is a rewrite. Both execution homes run the same
`pipeline.py` against the same database, so switching is a matter of which
trigger is enabled.

---

## A. Cloud works — retire the local timer

Run this once the scheduled cloud runs have been healthy for a week or two.

**Do not uninstall `pipeline.py`.** It is what the cloud sandbox executes.
Only the *local trigger* is being retired.

```bash
# 1. Stop and disable the local timer
systemctl --user disable --now home-search-pipeline.timer
systemctl --user list-timers home-search-pipeline    # expect: no rows

# 2. Confirm the cloud is actually running before removing the fallback
#    (check the last few runs landed and the site is current)
vercel logs --scope ben-gieses-projects short-list | head -30
```

```bash
# 3. Optional: remove the unit symlinks. Skip this if you may come back.
rm -f ~/.config/systemd/user/home-search-pipeline.{service,timer}
systemctl --user daemon-reload
```

**Keep, even after the cloud takes over:**

| Thing | Why |
|---|---|
| `pipeline.py` | It is what the sandbox runs |
| `ops/systemd/*` (in git) | Re-enabling is two commands; deleting buys nothing |
| Local `.env` | Manual runs and recovery still need it |
| `data/.auth/compass_state.json` | A local run refreshes the session the cloud reuses |

**Manual runs still work unchanged** — useful for recovery, and for stages
you may deliberately keep local (see B2):

```bash
cd ~/code/home-search && python pipeline.py
```

**`loginctl enable-linger`** can stay. It is harmless and other user timers
may rely on it. Remove with `loginctl disable-linger "$USER"` only if
nothing else needs it.

---

## B. Cloud fails — return to local

The whole point of the phased design: this is a trigger change, not a
migration back.

```bash
# 1. Stop the cloud from running
#    Remove the "crons" entry from the short-list project's vercel.json,
#    commit, and redeploy — or pause the project in the Vercel dashboard.

# 2. Re-enable the local timer
systemctl --user enable --now home-search-pipeline.timer
systemctl --user list-timers home-search-pipeline    # expect: a next-fire time

# 3. Catch up now rather than waiting for the next trigger
systemctl --user start home-search-pipeline
journalctl --user -u home-search-pipeline -f
```

That is the whole rollback **if the Turso cutover has already happened** —
both homes share one database, so nothing needs restoring.

### B2. Partial retreat (one stage fails in the cloud, the rest are fine)

The likely shapes: Nominatim blocks the datacenter IP (commutes), or the
photo CDN does (photo downloads). Because every stage reads and writes the
same database, stages can be split across homes:

```bash
# Cloud cron runs everything except the failing stage, e.g.:
python pipeline.py --from=commutes           # or a --skip=commutes equivalent

# Laptop picks up the rest opportunistically:
python pipeline.py --only=commutes
```

This is worth reaching for before abandoning the cloud entirely. Log which
stage failed and why in `docs/journal/decisions.md`.

### B3. Reversing the Turso cutover

The cutover happened on 2026-08-31. Turso is the source of truth; there is
no local `data/listings.db` in the pipeline any more, and `publish.py` is
gone. To reverse it:

```bash
# Revert the three cutover commits, newest first.
# 89e833b  delete publish.py
# e4b38d1  prune hosted_photos with the listing
# 4e3cdc2  make Turso the source of truth
git revert 89e833b e4b38d1 4e3cdc2

# Restore the archived local database taken immediately before the flip.
# Verified at archive time: 1,368,064 bytes, integrity_check ok, 8,190 rows
# across all six tables, byte-identical to the live db (sha256 cd069c631d92df9e).
cp data/archive/listings-pre-turso-cutover.db data/listings.db

# Bring it up to date from Turso, which has been the live copy since, then
# re-mirror. publish.py exists again after the revert.
python score.py && python publish.py
```

The archive is a point-in-time copy from the cutover, so anything written to
Turso *after* the cutover is not in it. `score.py` recomputes scores from the
restored rows; genuinely new listings scraped after the cutover would need a
`python scrape.py` to repopulate. Turso's schema never changes shape during
the cutover, so the old two-database flow resumes immediately.

Turso's schema never changes shape during the cutover, so the old two-database
flow resumes immediately. `visual_scores.raw_response` lives in Turso
throughout, so the paid vision scoring survives regardless of which database
is authoritative.

---

## C. Remove the automation entirely

Back to running scripts by hand, keeping the data.

```bash
systemctl --user disable --now home-search-pipeline.timer
rm -f ~/.config/systemd/user/home-search-pipeline.{service,timer}
systemctl --user daemon-reload
```

`pipeline.py` still works as a manual entry point, and the five scripts still
work individually. Nothing about the data changes.

To also clear the orchestrator's own bookkeeping (safe — it is only
scheduling state, never listing data):

```bash
rm -f data/.pipeline.lock data/.pipeline-last-success.json
rm -rf data/logs/
```

---

## What is never safe to delete

| Path | Contains |
|---|---|
| `data/listings.db` | Every listing, score, and commute — plus the vision scoring |
| `data/listings/*.json` | The JSON store; `is_scraped()` reads it to decide what to re-fetch |
| `data/photos/` | ~1 GB of photos; re-downloading means re-scraping every listing |
| `data/.auth/compass_state.json` | The Compass session; deleting forces a cold login |
| `data/.photo_scoring_batch_state.json` | In-flight vision batches. **Deleting this can cause the same listings to be paid for twice.** |
| `.env` | Every credential. Gitignored, never committed, and not recoverable from the repo |

`data/` is gitignored in full, so none of it is in version control. Back it
up before any destructive step.
