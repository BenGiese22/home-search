"""Exit codes shared between pipeline stages and pipeline.py.

EXIT_PARTIAL means: the stage finished its work and wrote everything it
could, but some items failed. pipeline.py alerts on it and carries on to
the next stage, because stopping would throw away the items that succeeded
and gain nothing. Any other nonzero code still stops the run.
"""

EXIT_PARTIAL = 10
