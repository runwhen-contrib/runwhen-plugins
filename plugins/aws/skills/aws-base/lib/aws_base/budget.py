"""The discover task's time budget for the sync push.

The task's timeout (tasks.yaml: 600s) covers the queries AND the push, and the push can be slow:
the commit alone may wait COMMIT_TIMEOUT (300s) for papi's sweep, and every request retries with a
backoff. `sync.py` is the copy of k8s-base's and stays verbatim, so the budget is applied through
the two hooks its client already has, the opener and the sleep: each request gets at most what is
left before `deadline`, a request that would start with nothing left fails as a `timeout` (and the
open sync is aborted, as for any failure), and a retry never sleeps past the deadline."""

import time

from aws_base import sync
from aws_base.errors import RwError

TASK_BUDGET = 600  # seconds: discover's `timeout` in tasks.yaml (a test keeps them equal)
MARGIN = 10  # seconds kept after the push for the summary and the result


def deadline_from(started):
    """The monotonic time by which the push must be over, for a task that started at `started`."""
    return started + TASK_BUDGET - MARGIN


def budgeted_opener(deadline, clock=time.monotonic):
    """An opener for SyncClient that clamps each request's timeout to what is left."""

    def open_(request, timeout=None):
        remaining = deadline - clock()
        if remaining < 1:
            raise RwError(
                "timeout", f"the task's {TASK_BUDGET}s time budget is used up before the sync ended"
            )
        return sync.default_open(request, timeout=min(timeout or remaining, remaining))

    return open_


def budgeted_sleep(deadline, clock=time.monotonic, sleep=time.sleep):
    """A sleep for SyncClient's retries that never goes past the deadline."""

    def wait(seconds):
        sleep(max(0.0, min(seconds, deadline - clock())))

    return wait
