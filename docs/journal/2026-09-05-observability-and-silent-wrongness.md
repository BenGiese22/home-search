# Observability, and the failure mode this system actually has

*2026-09-05. Written after the cloud cutover, and after eight defects none of
which the test suite caught.*

## The shape of every bug so far

Nine defects surfaced while moving the pipeline into a Vercel Sandbox. The
test suite (817 in home-search, 294 in short-list) caught **none** of them.
Every one was found by running the real thing and reading the output.

That is not an argument for more tests. Most of these were unfalsifiable from
inside a test: they were assumptions about what a third-party SDK does, or
about what a run had actually accomplished. The pattern that matters is what
they looked like from outside:

> **HTTP 200. Valid JSON. No exception. Plausible row counts. And the wrong
> answer.**

The clearest case: the reaper returned `collect-and-stop` every ten minutes
for hours against a sandbox nobody was using. It resumed a stopped sandbox to
read its markers, found the previous run's `done` still on disk, and stopped
it again — 144 resume/stop cycles a day, each billing about twenty seconds of
provisioned 4 GB.

Every surface reported health. Status codes were 200. No invocation failed.
The database was correct. The only evidence was that six consecutive log lines
said `collect-and-stop` where they should have said `noop` — a semantic error
inside well-formed output, visible only to someone who already knew which word
was right.

A second case, same shape: a run completed with `exit_code: 0` having left a
listing with 37 photo URLs and zero hosted photos. The pipeline had *computed*
that the work was pending and then not done it. Nothing noticed, because
nothing was responsible for comparing what should have happened to what did.

## What Vercel offers, and what it is worth here

Reviewed against the docs on 2026-09-05, on Pro.

**Useless against this failure mode**, and worth naming so they are not
reached for again: error rates, p95 latency, status-code dashboards, Error
Anomaly alerts, Agent Investigations, Web Analytics, Speed Insights. Every one
keys on a signal a run of clean 200s never produces. Error anomaly alerts need
a route's five-minute error count to exceed its 24-hour baseline; to that
detector our resume loop was a perfectly healthy system. Agent Investigations
are downstream of alerts and inherit the blindness entirely.

**Partially useful.** Sandbox CPU and provisioned-memory metrics, grouped by
sandbox name, *would* have caught the resume loop — it had a physical cost
even without a semantic one. A volume signal works when the wrong behaviour
burns something. It would not have caught the zero-photos bug, where the
wrong behaviour was doing *less* work, not more.

**Actually capable.** `metric()` from `@vercel/functions` emits a named number
with arbitrary string tags, and those tags become dimensions you can filter,
group and alert on. It is the only native surface that can express a claim
about *meaning* rather than volume. This is what we adopted.

**Not what we assumed.** Runtime log retention on bare Pro is **1 day** — a
figure worth knowing given that reading logs was our entire detection method.
It turned out Observability Plus was already enabled on this account (a 7-day
metrics query returns data), so retention is 30 days and there was nothing to
decide. The lesson is the checking, not the outcome: the recommendation to
consider buying it was made before testing whether it was already on.

## What we changed

Three layers, deliberately at different altitudes.

**1. `verify.py` — stop emitting success when the data is wrong.**

The central conclusion, and it needed no Vercel feature at all:

> You do not need a tool that detects wrong-looking success. You need to stop
> returning success when you are wrong.

`verify.py` runs as the last pipeline stage, asserts four invariants, and
turns a violation into a **non-zero exit code** — the one signal every layer
above already reacts to. `pipeline.py` halts, `run.py` records it in the
`done` marker, the reaper reads that and pushes a notification. The invariants
are the failures actually observed, not a generic checklist:

- a listing with photo URLs and no hosted photos (it renders imageless)
- an empty corpus — the catastrophic case, where a scrape returning nothing
  delists everything and every stage downstream succeeds against zero rows
- a listing with no score row, invisible to the viewer's ranking
- orphaned child rows, where each stranded `hosted_photos` row is a blob
  nothing will ever delete

Run against production before the fix it exited 1 and named the real defect.

**2. The reaper's invariant, stated rather than assumed.**

```ts
if (action !== 'noop' && status !== 'running') {
  throw new Error(`reaper invariant violated: action=${action} with sandbox status=${status}`)
}
```

Every action but `noop` stops a sandbox, and stopping one that was not running
means the reaper woke it to do so. The handler's early return makes this
structurally true today; asserting it means a future edit reordering those
cannot quietly reintroduce the loop. A throw becomes a 500, which retrofits
the whole of Vercel's free alerting onto a problem it otherwise cannot see.

**3. `pipeline.decision` — make a wrong pattern visible over time.**

Both cron routes emit one metric per response, tagged with the decision:

```bash
vercel metrics pipeline.decision --group-by outcome --since 24h
vercel metrics pipeline.decision --filter "outcome:collect-and-stop"
```

The hour of log-reading that found the resume loop is now a one-line query.
`outcome` is deliberately a single field rather than several booleans: it is
the thing worth grouping by, and the thing that was wrong in every incident.
`401` stays distinct from `500` because one is a misconfigured caller and the
other is us; grouping them hides one behind the other's baseline. Emission
never throws — an observability path that can break the thing it observes is
worse than none.

Roughly 4,500 events a month, which is fractions of a cent.

### The altitudes matter

| Layer | Catches | Signal |
|---|---|---|
| `verify.py` | wrong **data** | exit code → ntfy |
| reaper invariant | wrong **decision** | throw → 500 → Vercel alerting |
| `pipeline.decision` | wrong **pattern over time** | queryable, 30-day retention |

Only the first two prevent anything. The third makes wrongness visible, which
is the job reading logs by hand was doing badly.

## The Sandbox blind spot

The runner holds no Vercel credential by design — an OIDC token expires
mid-run and a personal token is team-wide, which is the worst blast radius for
a VM driving Chromium against a third-party site. The consequence is that
**no Vercel-native telemetry can originate inside the sandbox.** `metric()`,
Queues, drains and OTEL all need auth. Anything the sandbox wants observed
must be carried out by something that holds a credential: the launcher or the
reaper.

The chosen approach is the `done` marker, which already exists, is already
atomically renamed, and is already read by the reaper. Extending it to carry
stage counts — photos uploaded, listings scraped — would let the reaper assert
on them from outside and turn "exited 0 but did nothing" into an alert. Not
built yet; `verify.py` covers the same ground from inside for now.

Rejected: holding `runCommand` open for stdout (we detach deliberately, and
`maxDuration` forbids it), and wholesale log forwarding (runtime logs cap at
256 lines / 1 MB per request, which a Playwright scrape blows through
instantly). Forwarding the *tail on failure only* is worth doing and is not
yet built.

## Gaps in the platform

Recorded so they stop being re-researched.

1. **No alerting on log message content.** Query exposes only infrastructure
   dimensions — status, route, region, WAF rule. There is no log-message
   metric and no user-defined dimension on request events. Only custom metrics
   add one. Alerting on log *text* requires a drain and an external service.
2. **No cron run history, no retries, no failure notification.** Vercel does
   not retry a failed cron invocation, offers no per-run status view, and a
   transient network error means the function never executes and *no log is
   created at all*. A missed run is invisible by construction.
3. **No dead-man's switch.** Vercel can alert on anomalous presence, never on
   absence. Nothing inside the platform can tell you a scheduled run did not
   happen. An external heartbeat checker is the only answer, and would not
   have caught any defect so far — it covers a class we have not hit yet.
4. **1-day runtime log retention on bare Pro.** Short against a six-hourly
   cron. Observability Plus lifts it to 30 days.
5. **No credential-free telemetry ingress for Sandbox.** There is no
   unauthenticated, sandbox-scoped write path for metrics or logs.
6. **`metric()` does not exist inside a Sandbox** — it is Function-only.
7. **Sandbox telemetry is resource-only.** CPU, memory, transfer, session
   counts. Nothing captures what the sandbox's processes actually printed.
8. **`vercel metrics` requires Observability Plus**, so scripted metric
   assertions are unavailable on bare Pro.

## The thing worth remembering

Six of the nine defects came from trusting a type signature or a remembered
API instead of reading what the SDK does. Two were compounding versions of the
same mistake: `cwd` accepted and ignored by the file APIs, and
`readFileToBuffer` auto-resuming a sandbox the caller was trying to observe
without waking. One was a wrong diagnosis of a bug — `status` was read as a
method when it is a getter — that produced working code only because the fix
was written to handle both shapes.

The general lesson is not "add monitoring". It is that a system running while
nobody watches does not fail loudly. It returns 200 with the wrong word in it,
and the only defence is to state what should be true and check.
