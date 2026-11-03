"""Fetching the target workload, its pods and their logs (the only I/O besides k8s.py)."""

from k8s_workload_logs.errors import RwError
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


def log_file_missing(text):
    """kubectl exits 0 and prints this single line as the log when the node no longer has the
    container's log file (a removed dead container); it is not log content."""
    t = text.strip()
    return t.startswith("unable to retrieve container logs for ") and "\n" not in t


def _previous_is_gone(stream, message):
    return (
        stream["previous"] and "previous terminated container" in message and "not found" in message
    )


def fetch_streams(ctx, streams, tail=None, since=None):
    """Run `kubectl logs` for every planned stream.

    Returns (fetched, gone): `fetched` is [(stream, text)]; `gone` is the
    streams whose log no longer exists (a removed previous run is normal; reported as
    unavailable). Any other failure is reported with `ctx.error` and the rest
    continue; when every attempted call failed there is nothing to report, so
    the first failure is raised instead.
    """
    fetched, gone, failures = [], [], []
    for s in streams:
        args = [
            "-n",
            ctx.resource.namespace,
            "logs",
            s["pod"],
            "-c",
            s["container"],
            f"--limit-bytes={LIMIT_BYTES}",
        ]
        if s["previous"]:
            args.append("--previous")
        if tail is not None:
            args.append(f"--tail={int(tail)}")
        if since is not None:
            args.append(f"--since={since}s")
        try:
            body = kubectl_text(ctx, *args)
            if log_file_missing(body):
                gone.append(s)
            else:
                fetched.append((s, body))
        except RwError as exc:
            if _previous_is_gone(s, exc.message):
                gone.append(s)
            else:
                failures.append((s, exc))
    if failures and not fetched and not gone:
        raise failures[0][1]
    for s, exc in failures:
        scope = f"{s['pod']}/{s['container']}" + (" (previous)" if s["previous"] else "")
        ctx.error(exc.code, f"kubectl logs failed for {scope}: {exc.message}", scope=scope)
    return fetched, gone
