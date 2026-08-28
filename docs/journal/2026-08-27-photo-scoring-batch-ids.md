# Photo-scoring batch IDs — 2026-08-27 first full run

Submitted ~10:23 MDT against the full 144-listing collection (142 eligible,
2 skipped for too few photos). Written here as a standalone backup,
separate from `data/.photo_scoring_batch_state.json` (the working
checkpoint `score_photos.py` reads/deletes automatically) — this file is
never touched by the script and won't disappear if that one does.

Each batch id can be re-fetched directly, independent of any local script,
via `client.messages.batches.retrieve(batch_id)` / `.results(batch_id)`
with the same Anthropic API key, for as long as Anthropic retains batch
results (well beyond same-day).

| Batch ID | Listings |
|---|---|
| `msgbatch_01ViQtSGizfCZDss3AsGYC2H` | 16 |
| `msgbatch_01FE1t64mtWkuxx3YjotMMPt` | 15 |
| `msgbatch_017YFEy93FcUR5Fvdasd1RB2` | 16 |
| `msgbatch_01Pui8fD5HoaHP9vp6FLLkvj` | 15 |
| `msgbatch_01MgXz1RpogE3TZoAVMkUM8B` | 17 |
| `msgbatch_01VMfZUvV8Duk5Xg8LQDN12y` | 15 |
| `msgbatch_01XmWUJ5wEBHW8tADNqJJLLC` | 15 |
| `msgbatch_01EGtyvJJkMgmMB9yAgJvjV1` | 18 |
| `msgbatch_01Dota9kdXG85ay1fSQLZv2h` | 15 |

142 listings total. As of the last check (~11:09 MDT): 5 of 9 ended
(78 succeeded, 0 errored), 4 still `in_progress`.
