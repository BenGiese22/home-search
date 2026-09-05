"""What the traffic-aware commute did to the corpus and to the ranking.

    venv/bin/python ops/commute_distribution.py [data/snapshots/<date>]

Reads Turso for "after" and a snapshot directory written by
`ops/snapshot_tables.py` for "before". Defaults to the most recent snapshot.

This exists to answer two questions that were deliberately left open when the
measurement changed, because both are decisions to make *from* the new
distribution rather than guesses to bake into the change that produced it:

1. **Do the 20/30/40 thresholds still need recalibrating?** They were drawn
   around a lived rush-hour commute and then fed free-flow durations, so most
   of the corpus sat in the flat `<= 20 -> 100` region and the heaviest
   factor in the rubric was very nearly a constant. The input is fixed now.
   The question is whether the curve is doing its job.

2. **What should an unrouted listing score?** (#29) A flat neutral 50 was
   meant to keep one missing field from tanking a composite, but if every
   routed listing scores above 50 then "neutral" is really "last".

The intent, the argument on both sides of the threshold question, and the one
normalisation that must NOT be used are written up in
`docs/commute-scoring.md`. Read that before changing a threshold on the
strength of a number this prints.

**Read-only.** `connect()` rather than `stage_connection()`, so it cannot
reach `ensure_schema` and alter a table on the way past, and it issues
nothing but SELECTs.
"""

import csv
import statistics
import sys
from pathlib import Path

# Run as `python ops/<name>.py`, which puts ops/ on sys.path rather than the
# repo root. Same line as ops/canary.py, for the same reason.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.commute import COMMUTE_SOURCE
from src.turso_db import connect

SNAPSHOT_ROOT = Path("data") / "snapshots"

# The curve's own breakpoints. Reported as bands so the shape of the
# distribution against the curve is visible rather than inferred.
BANDS = ((0.0, 20.0), (20.0, 30.0), (30.0, 40.0), (40.0, float("inf")))

# Above this share sitting in the flat region, the factor is still closer to
# a constant than to a signal and recalibration is worth proposing.
FLAT_REGION_CONCERN = 0.50


def latest_snapshot() -> Path | None:
    if not SNAPSHOT_ROOT.exists():
        return None
    directories = sorted(p for p in SNAPSHOT_ROOT.iterdir() if p.is_dir())
    return directories[-1] if directories else None


def read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open() as handle:
        return list(csv.DictReader(handle))


def as_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def summarise(label: str, values: list[float]) -> None:
    if not values:
        print(f"  {label:10} (none)")
        return
    print(
        f"  {label:10} n={len(values):>4}  min {min(values):6.1f}  "
        f"median {statistics.median(values):6.1f}  max {max(values):6.1f}"
    )


def band_counts(values: list[float]) -> list[int]:
    return [sum(1 for v in values if low < v <= high) for low, high in BANDS]


def main() -> int:
    snapshot = Path(sys.argv[1]) if len(sys.argv) > 1 else latest_snapshot()
    if snapshot is None or not snapshot.exists():
        print(
            "no snapshot to compare against; run ops/snapshot_tables.py first",
            file=sys.stderr,
        )
        return 2
    print(f"before: {snapshot}")

    before_commute = {
        row["listing_id"]: as_float(row.get("medtronic_minutes"))
        for row in read_csv(snapshot / "commute.csv")
    }
    before_scores = {
        row["listing_id"]: as_float(row.get("commute_score"))
        for row in read_csv(snapshot / "scores.csv")
    }

    conn = connect()
    after_commute = {
        row["listing_id"]: (row["medtronic_minutes"], row["commute_source"])
        for row in conn.execute(
            "SELECT listing_id, medtronic_minutes, commute_source FROM commute"
        )
    }
    after_scores = {
        row["listing_id"]: (row["commute_score"], row["has_incomplete_data"])
        for row in conn.execute(
            "SELECT listing_id, commute_score, has_incomplete_data FROM scores"
        )
    }

    print("\n" + "=" * 70)
    print("PROVENANCE")
    for row in conn.execute(
        "SELECT commute_source, COUNT(*) AS n FROM commute GROUP BY commute_source"
    ):
        marker = "  <- current" if row["commute_source"] == COMMUTE_SOURCE else ""
        print(f"  {str(row['commute_source']):28} {row['n']:>4}{marker}")

    print("\n" + "=" * 70)
    print("MEDTRONIC MINUTES")
    old = [v for v in before_commute.values() if v is not None]
    new = [m for m, _ in after_commute.values() if m is not None]
    summarise("before", old)
    summarise("after", new)

    ratios = sorted(
        after_commute[lid][0] / before_commute[lid]
        for lid in before_commute
        if before_commute.get(lid)
        and lid in after_commute
        and after_commute[lid][0] is not None
    )
    if ratios:
        print(
            f"\n  ratio after/before  n={len(ratios)}  min {ratios[0]:.2f}  "
            f"median {statistics.median(ratios):.2f}  max {ratios[-1]:.2f}"
        )

    print("\n" + "=" * 70)
    print("AGAINST THE CURVE  (thresholds unchanged: 20 / 30 / 40 min)")
    if new and max(new) <= 30.0:
        print(
            f"  NOTE: nothing in the corpus exceeds {max(new):.1f} min, so the 30"
            "\n  and 40 breakpoints are dead against this data -- a curve"
            "\n  documented as four segments is behaving as two."
        )
    labels = ["<=20 -> 100", "20-30", "30-40", ">40 -> 0"]
    old_bands, new_bands = band_counts(old), band_counts(new)
    print(f"  {'band':14} {'before':>8} {'after':>8}")
    for label, b, a in zip(labels, old_bands, new_bands):
        print(f"  {label:14} {b:>8} {a:>8}")

    print("\n" + "=" * 70)
    print("COMMUTE SUB-SCORE")
    old_cs = [v for v in before_scores.values() if v is not None]
    new_cs = [s for s, _ in after_scores.values() if s is not None]
    summarise("before", old_cs)
    summarise("after", new_cs)
    print(f"  distinct values   before {len(set(old_cs)):>4}   after {len(set(new_cs)):>4}")
    at_100 = sum(1 for s in new_cs if s == 100.0)
    share = at_100 / len(new_cs) if new_cs else 0.0
    print(f"  scoring exactly 100   {at_100}/{len(new_cs)}  ({share:.0%})")

    print("\n" + "=" * 70)
    print("FOR #29 -- what an unrouted listing is really being told")
    routed = sorted(new_cs)
    if routed:
        below_neutral = sum(1 for s in routed if s < 50.0)
        print(f"  routed listings scoring below the neutral 50: {below_neutral}")
        print(f"  routed median: {statistics.median(routed):.1f}")
        quartile = routed[len(routed) // 4]
        print(f"  routed 25th percentile: {quartile:.1f}")
        print(
            "  -> a neutral 50 ranks an unrouted listing beneath "
            f"{len(routed) - below_neutral} of {len(routed)} routed listings"
        )
    unrouted = sum(1 for m, _ in after_commute.values() if m is None)
    incomplete = sum(1 for _, flag in after_scores.values() if flag)
    print(f"  listings currently unrouted: {unrouted}")
    print(f"  listings flagged has_incomplete_data: {incomplete}")

    print("\n" + "=" * 70)
    print("VERDICT")
    if share > FLAT_REGION_CONCERN:
        print(
            f"  {share:.0%} of the corpus still sits in the flat region, so the"
            "\n  commute factor remains closer to a constant than to a signal."
            "\n  Recalibrating the thresholds is worth proposing -- but it is now"
            "\n  a question about the curve, not about the data, and it is Ben"
            "\n  and Megan's call rather than the code's: it reorders houses"
            "\n  they are choosing between."
            "\n"
            "\n  Read docs/commute-scoring.md before acting on this. It carries"
            "\n  the argument for leaving the threshold alone -- which is not"
            "\n  weak -- and names the one normalisation that must not be used."
        )
    else:
        print(
            f"  Only {share:.0%} sits in the flat region. The curve is"
            "\n  discriminating across the corpus; no recalibration proposed."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
