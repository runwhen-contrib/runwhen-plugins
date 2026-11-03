"""The time budget of a task: it stops asking pods before the task's own timeout kills it."""

import time

from k8s_vault_health.errors import RwError

TOTAL_SECONDS = 45  # inside the 60s task timeout: leaves room to shape and return the result


class Budget:
    """Elapsed wall time since construction, on a monotonic clock (ctx.now() is fixed for the run
    and cannot measure time). `clock` is injectable for tests."""

    def __init__(self, total=TOTAL_SECONDS, clock=time.monotonic):
        self.total = total
        self._clock = clock
        self._start = clock()

    def left(self):
        return self.total - (self._clock() - self._start)

    def spent(self):
        return self.left() <= 0

    def timeout(self, cap):
        """Seconds a call may take: `cap`, or what is left of the budget if that is less. Raises
        a timeout RwError once the budget is spent, so the call is not made."""
        left = self.left()
        if left <= 0:
            raise RwError(
                "timeout", f"not asked: the {self.total}s time budget of the task was spent"
            )
        return max(1.0, min(cap, left))
