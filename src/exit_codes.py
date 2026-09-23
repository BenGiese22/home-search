"""Exit codes shared between pipeline stages and pipeline.py.

EXIT_PARTIAL means: the stage finished its work and wrote everything it
could, but some items failed. pipeline.py alerts on it and carries on to
the next stage, because stopping would throw away the items that succeeded
and gain nothing. Any other nonzero code still stops the run.
"""

EXIT_PARTIAL = 10

# The last line a stage prints when it returns EXIT_PARTIAL:
#
#     PARTIAL: <stage>: <sorted comma-separated failed ids>
#
# A permanently broken listing makes its stage partial on every run, so an
# alert per partial run is an alert four times a day forever. pipeline.py
# reads this line back out of the stage's log tail and stays quiet while the
# set of failed ids is the one it already alerted on. <stage> is the
# pipeline's name for the stage, not the script's.
PARTIAL_PREFIX = "PARTIAL: "


def format_partial_line(stage: str, failed_ids) -> str:
    return f"{PARTIAL_PREFIX}{stage}: {','.join(sorted(set(failed_ids)))}"


def parse_partial_line(text: str, stage: str) -> frozenset[str] | None:
    """The failed ids from the stage's last PARTIAL line, or None if it
    printed none."""
    head = f"{PARTIAL_PREFIX}{stage}:"
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith(head):
            ids = line[len(head):].strip()
            return frozenset(i for i in ids.split(",") if i)
    return None
