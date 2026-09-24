import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))


# No test may send a real notification.
#
# send_email and notify default their `post` to requests.post, and
# pipeline.py reaches them with the real .env loaded. Any test that ran a
# failing stage without injecting notify_fn therefore emailed Ben for real:
# on 2026-09-23 every local test run sent "score failed ... on
# bengi-linux-G757", and he took them for production alerts. The
# revalidate POST had already been caught the same way (tests/test_pipeline.py).
#
# Wrapped here, at import time and before any test module imports them,
# so every `from src.mailer import send_email` gets the guarded version. A
# test that passes its own `post` is unaffected.
import functools

import src.mailer
import src.notify


def _refuse_network(*args, **kwargs):
    raise RuntimeError("tests must not send real notifications; pass post=")


def _guard(fn):
    @functools.wraps(fn)
    def guarded(*args, post=None, **kwargs):
        return fn(*args, post=post or _refuse_network, **kwargs)

    return guarded


src.mailer.send_email = _guard(src.mailer.send_email)
src.notify.notify = _guard(src.notify.notify)
