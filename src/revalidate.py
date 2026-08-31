"""Telling the hosted viewer its data changed.

The one part of publish.py that outlives it. short-list wraps its reads in
`'use cache'` + `cacheTag('listings')`, so its only contract with this
project is: the Turso database it reads is fresh, and someone POSTs
revalidate after writing. Under the single-source-of-truth architecture the
stages keep the first half; this keeps the second.
"""
from typing import Callable

import requests

REVALIDATE_TIMEOUT_SECONDS = 10


def revalidate(short_list_url: str, secret: str, post: Callable = requests.post) -> bool:
    """POSTs the viewer's revalidate hook. Returns True on success.

    Deliberately never raises. A failed revalidate is not a failed run: every
    write already landed, and short-list's cache expires on its own -- the
    only cost is that the site shows slightly stale data until it does.
    Turning that into a non-zero exit would fail runs that actually
    succeeded, and the exit-nonzero signal is reserved for data that did not
    make it to Turso.
    """
    try:
        response = post(
            f"{short_list_url.rstrip('/')}/api/revalidate",
            headers={"Authorization": f"Bearer {secret}"},
            timeout=REVALIDATE_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
    except Exception as exc:
        print(f"warning: revalidate call failed ({exc}) -- cache will expire naturally")
        return False
    print("revalidated the hosted site")
    return True
