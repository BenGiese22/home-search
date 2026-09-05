# Phase 3 gate spikes

Scripts behind the Phase 3 gates in issue #24. Each is deliberately
self-contained — only `requests` — so it runs unchanged on the desktop and
inside a bare Vercel Sandbox. That is the whole design: **run the same
script in both places and the only variable is the egress IP.**

## Running them

```bash
# Baseline, from the desktop's residential IP
python ops/spikes/phase3_egress_spike.py desktop-residential
python ops/spikes/nominatim_burst.py     desktop-residential

# From a datacenter IP. Needs Vercel CLI >= 59.11 -- 59.5 returns
# "invalidToken" against /v3/sandboxes even though `vercel whoami` works.
npx vercel@latest sandbox create --name gate-spike --timeout 20m \
    --scope ben-gieses-projects --project short-list
npx vercel@latest sandbox cp ops/spikes/phase3_egress_spike.py \
    gate-spike:/tmp/spike.py --scope ben-gieses-projects --project short-list
npx vercel@latest sandbox exec gate-spike \
    --scope ben-gieses-projects --project short-list -- \
    bash -lc 'pip install -q --break-system-packages requests && python3 /tmp/spike.py vercel-sandbox-iad1'
npx vercel@latest sandbox stop gate-spike --scope ben-gieses-projects --project short-list
npx vercel@latest sandbox rm   gate-spike --scope ben-gieses-projects --project short-list
```

**No secrets are involved.** Nominatim, OSRM and the Compass photo CDN all
answer unauthenticated, so nothing has to be copied into the sandbox. Stop
and remove the sandbox when finished — a persistent sandbox keeps a
snapshot, and an abandoned one bills.

Results are recorded in `docs/journal/decisions.md` (2026-09-03).
