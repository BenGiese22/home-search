"""Exit codes shared between pipeline stages and pipeline.py.

EXIT_PARTIAL means: the stage finished its work and wrote everything it
could, but some items failed. pipeline.py alerts on it and carries on to
the next stage, because stopping would throw away the items that succeeded
and gain nothing. Any other nonzero code still stops the run.
"""

import re
from typing import NamedTuple

EXIT_PARTIAL = 10

# The last line a stage prints when it returns EXIT_PARTIAL:
#
#     PARTIAL: <stage>: <kind>: <sorted comma-separated failed ids>
#
# A permanently broken listing makes its stage partial on every run, so an
# alert per partial run is an alert four times a day forever. pipeline.py
# reads this line back out of the stage's log tail and stays quiet while the
# (kind, failed ids) pair is the one it already alerted on. <stage> is the
# pipeline's name for the stage, not the script's.
#
# <kind> is why the items failed. The same ids can fail for a new reason --
# listings whose batch results errored last run, then a revoked API key that
# keeps those very listings from being submitted at all -- and that is news,
# so the kind is part of what has to match.
PARTIAL_PREFIX = "PARTIAL: "

# The items were attempted and came back failed.
KIND_ITEMS_FAILED = "items-failed"
# A batch never reached the API, so its items were not attempted.
KIND_SUBMIT_FAILED = "submit-failed"
# The API key was refused; nothing more is attempted until it is replaced.
KIND_KEY_REJECTED = "key-rejected"

_WITH_KIND = re.compile(r"([a-z][a-z-]*):\s*(.*)")


class PartialReport(NamedTuple):
    kind: str
    ids: frozenset[str]


def format_partial_line(stage: str, kind: str, failed_ids) -> str:
    return f"{PARTIAL_PREFIX}{stage}: {kind}: {','.join(sorted(set(failed_ids)))}"


def parse_partial_line(text: str, stage: str) -> PartialReport | None:
    """The cause and failed ids from the stage's last PARTIAL line, or None
    if it printed none.

    A line with no kind -- the format before kinds existed -- reads as
    items-failed, which is what every such line meant.
    """
    head = f"{PARTIAL_PREFIX}{stage}:"
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith(head):
            rest = line[len(head):].strip()
            match = _WITH_KIND.fullmatch(rest)
            kind, ids = match.groups() if match else (KIND_ITEMS_FAILED, rest)
            return PartialReport(kind, frozenset(i for i in ids.split(",") if i))
    return None
