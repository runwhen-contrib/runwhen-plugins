"""Fetching the target workload, its pods and their logs (the only I/O besides k8s.py)."""

from k8s_workload_logs.errors import RwError
from k8s_workload_logs.logs import (
    LOG_GONE,
    PREVIOUS_GONE,
    SKIPPED_BUDGET,
    SKIPPED_BYTES,
    unavailable_extra,
)
from k8s_workload_logs.k8s import LIMIT_BYTES, kubectl_json, kubectl_text
from k8s_workload_logs.workload import (
    DAEMONSET,
    DEPLOYMENT,
    STATEFULSET,
    owned_pods,
    owned_revisions,
    selector_string,
)

# plural resource name -> (Kind, kubectl resource)
TARGETS = {
    "deployments": (DEPLOYMENT, "deployments.apps"),
    "statefulsets": (STATEFULSET, "statefulsets.apps"),
    "daemonsets": (DAEMONSET, "daemonsets.apps"),
}


def target_plural(ctx):
    """'deployments' etc. from ctx.resource.type ('kubernetes/deployment'), else from the
    second-to-last segment of ctx.resource.path (.../deployments/<name>)."""
    rtype = getattr(ctx.resource, "type", None)
    if rtype:
        return rtype.rsplit("/", 1)[-1] + "s"
    parts = (getattr(ctx.resource, "path", None) or "").strip("/").split("/")
    return parts[-2] if len(parts) >= 2 else ""


def load_owned_pods(ctx):
    """The pods the target owns. A missing target or a failed list raises."""
    plural = target_plural(ctx)
    try:
        kind, resource = TARGETS[plural]
    except KeyError:
        raise ValueError(f"k8s-workload-logs does not apply to {plural!r}") from None
    ns = ctx.resource.namespace
    workload = kubectl_json(ctx, "-n", ns, "get", resource, ctx.resource.name)
    selector = selector_string(workload["spec"]["selector"])
    revisions = []
    if kind == DEPLOYMENT:
        items = kubectl_json(ctx, "-n", ns, "get", "replicasets.apps", "-l", selector)["items"]
        revisions = owned_revisions(workload, items)
    items = kubectl_json(ctx, "-n", ns, "get", "pods", "-l", selector)["items"]
    return owned_pods(kind, workload, revisions, items)


LOG_CALL_TIMEOUT = 20  # seconds for one `kubectl logs`


def log_file_missing(text):
    """kubectl exits 0 and prints this single line as the log when the node no longer has the
    container's log file (a removed dead container); it is not log content."""
    t = text.strip()
    return t.startswith("unable to retrieve container logs for ") and "\n" not in t


def _previous_is_gone(stream, message):
    return (
        stream["previous"] and "previous terminated container" in message and "not found" in message
    )


class StreamReader:
    """Reads the planned log streams one at a time: iterate for (stream, text) and drop
    the text before asking for the next, so memory holds one stream.

    Reading stops, and the streams left are reported as unavailable, when `budget`
    (wall clock) is exhausted or `max_bytes` have been read. A stream whose `kubectl
    logs` fails is reported with `ctx.error` (a timeout of LOG_CALL_TIMEOUT included)
    and the rest continue; a `--previous` log that no longer exists is unavailable,
    not an error. `finish()` ends with the errors: when every attempted call failed
    there is nothing to report, so the first failure is raised instead.
    """

    def __init__(self, ctx, streams, tail=None, since=None, budget=None, max_bytes=None):
        self.ctx, self.streams, self.tail, self.since = ctx, streams, tail, since
        self.budget, self.max_bytes = budget, max_bytes
        self.gone, self.failures, self.skipped = [], [], []
        self.read = 0  # streams read
        self.bytes_read = 0
        self.scan_incomplete = False
        self._stopped = None  # reason string once reading stopped early
        self._pos = 0  # index of the stream after the one last yielded

    def _args(self, s):
        args = [
            "-n",
            self.ctx.resource.namespace,
            "logs",
            s["pod"],
            "-c",
            s["container"],
            f"--limit-bytes={LIMIT_BYTES}",
        ]
        if s["previous"]:
            args.append("--previous")
        if self.tail is not None:
            args.append(f"--tail={int(self.tail)}")
        if self.since is not None:
            args.append(f"--since={self.since}s")
        return args

    def __iter__(self):
        for pos, s in enumerate(self.streams):
            if self.budget is not None and self.budget.expired():
                self._stopped = SKIPPED_BUDGET
            elif self.max_bytes is not None and self.bytes_read >= self.max_bytes:
                self._stopped = SKIPPED_BYTES
            if self._stopped:
                self.skipped = [(x, self._stopped) for x in self.streams[pos:]]
                return
            try:
                body = kubectl_text(self.ctx, *self._args(s), timeout=LOG_CALL_TIMEOUT)
            except RwError as exc:
                if _previous_is_gone(s, exc.message):
                    self.gone.append(s)
                else:
                    self.failures.append((s, exc))
                continue
            if log_file_missing(body):
                self.gone.append(s)
                continue
            size = len(body.encode("utf-8", "replace"))
            self.bytes_read += size
            self.read += 1
            self._pos = pos + 1
            yield s, body, size

    def stop_early(self):
        """The caller ran out of budget inside a stream: the streams after it are not read."""
        self.scan_incomplete = True
        self.skipped = [(x, SKIPPED_BUDGET) for x in self.streams[self._pos :]]

    def unavailable(self):
        """Streams with no readable log, as `unavailable` entries."""
        out = unavailable_extra(self.gone, lambda s: PREVIOUS_GONE if s["previous"] else LOG_GONE)
        for s, reason in self.skipped:
            out += unavailable_extra([s], lambda _s, reason=reason: reason)
        return out

    def finish(self):
        """Report what went wrong: raise when nothing could be read, else ctx.error per problem."""
        if self.failures and not self.read and not self.gone:
            raise self.failures[0][1]
        for s, exc in self.failures:
            scope = f"{s['pod']}/{s['container']}" + (" (previous)" if s["previous"] else "")
            self.ctx.error(exc.code, f"kubectl logs failed for {scope}: {exc.message}", scope=scope)
        if self.scan_incomplete or (self.skipped and self.skipped[0][1] == SKIPPED_BUDGET):
            self.ctx.error(
                "timeout",
                f"scan budget exhausted: {len(self.skipped)} stream(s) not read or only partly read; counts are partial",
                scope="scan",
            )
