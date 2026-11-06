"""What the asks of a task showed, and how that decides its status."""

from k8s_jenkins_health.common import MESSAGE_LIMIT
from k8s_jenkins_health.errors import RwError

AUTH_HINT = (
    "anonymous read is off; a Jenkins API token needs a generic connection (misc plugin, M4)"
)


class Tally:
    """Replies, replies that wanted credentials, and failures, over every ask of a task."""

    def __init__(self):
        self.answered = 0
        self.auth = 0
        self.failures = []  # (scope, RwError)
        self.auth_scopes = []  # (scope, what, HTTP status)

    def answer(self, reply, scope, what):
        self.answered += 1
        if reply.status in (401, 403):
            self.auth += 1
            self.auth_scopes.append((scope, what, reply.status))

    def fail(self, scope, exc):
        self.failures.append((scope, exc))

    def finish(self, ctx, asked):
        """End the task's asks: raise the first failure when nothing answered at all, skip the task
        when everything that answered wanted credentials, else record each failure and each
        endpoint that wanted credentials as a scoped error (the result is partial)."""
        if asked and not self.answered:
            scope, exc = self.failures[0]
            raise RwError(exc.code, exc.message, scope=scope) from exc
        if self.answered and self.auth == self.answered:
            ctx.skip(
                "every endpoint asked answered 401 or 403 to the anonymous caller. " + AUTH_HINT
            )
        for scope, what, status in self.auth_scopes:
            ctx.error(
                "permission_denied",
                f"{what} answered HTTP {status}: it needs credentials",
                scope=scope,
                hint=AUTH_HINT,
            )
        for scope, exc in self.failures:
            ctx.error(exc.code, exc.message[:MESSAGE_LIMIT], scope=scope)
