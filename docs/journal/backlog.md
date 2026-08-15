# Backlog

Raw, ungroomed ideas that haven't been formalized into a spec yet. Items
graduate out of here (and get deleted from this file) once they're written
up properly under `docs/superpowers/specs/`.

## 2026-08-15 — nearby bike trails / parks as a scoring factor

Ben flagged proximity to bike trails and/or parks as worth assessing
somewhere in the listing scoring — currently not covered by any factor in
the v1 baseline-scoring rubric.

Plan for now: fold it into the photo-scoring v1 design as a cheap
text-keyword scan over the listing description/amenities (same lightweight
approach as the existing outdoor/hosting placeholder — phrases like
"trail," "park," "greenbelt," etc.).

Ben wants something more robust eventually, and named two candidate
directions for a v2 pass, undecided between them:

- **Geocoded proximity check** — look up actual nearby parks/trails via a
  real geo data source and compute distance, the same way the commute
  factor already does real routing via OSRM/Nominatim instead of guessing.
  Would need a parks/trails dataset or API to be identified (not yet
  researched).
- **Claude-API-prompt-based check** — ask a Claude model directly about
  what's nearby a given address, similar to the *rejected* original
  approach for commute distance. Worth remembering that the commute
  feature specifically moved *away* from LLM-prompted distance guessing
  because it was measurably less accurate than real routing data for a
  less-documented destination — the same caution would apply here before
  trusting an LLM's geographic knowledge over ground truth.
