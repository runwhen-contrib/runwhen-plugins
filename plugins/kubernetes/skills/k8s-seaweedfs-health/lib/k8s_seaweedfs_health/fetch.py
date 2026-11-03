"""Fetching the release a task looks at, and the HTTP reads through its pods (the only I/O besides k8s.py)."""

import re

from k8s_seaweedfs_health.discovery import (
    NAMESPACE,
    STATEFULSET,
    pods_with_role,
    release_of_statefulset,
    releases_of,
    resolve_target,
    roles_of,
    selector,
)
from k8s_seaweedfs_health.errors import RwError
from k8s_seaweedfs_health.k8s import (
    MESSAGE_LIMIT,
    ProxyError,
    kubectl_json,
    kubectl_json_scoped,
    proxy_get,
    proxy_json,
)

PORT_SETTINGS = {
    "master": "master_port",
    "volume": "volume_port",
    "filer": "filer_port",
    "s3": "s3_port",
}
RELEASE_RE = re.compile(r"^[A-Za-z0-9]([-A-Za-z0-9_.]{0,61}[A-Za-z0-9])?$")


class Release:
    """The pods of one SeaweedFS release in one namespace."""

    def __init__(self, namespace, name, target, pods):
        self.namespace, self.name, self.target, self.pods = namespace, name, target, pods

    def role_pods(self, role):
        """(queryable pods, pods of that role that cannot be asked), each by name."""
        ok = pods_with_role(self.pods, role)
        names = {p["metadata"]["name"] for p in ok}
        rest = sorted(
            (p for p in self.pods if role in roles_of(p) and p["metadata"]["name"] not in names),
            key=lambda p: p["metadata"]["name"],
        )
        return ok, rest


def target_kind(ctx):
    """'namespace' or 'statefulset'. Needs no cluster call, so it runs before anything else."""
    return resolve_target(getattr(ctx.resource, "type", None), getattr(ctx.resource, "path", None))


def check_release_input(release):
    if release and not RELEASE_RE.match(release):
        raise RwError("invalid_input", f"release {release!r} is not a valid label value")


def load_release(ctx, release=""):
    """The release the target stands for and its pods.

    A StatefulSet target names its release by its app.kubernetes.io/instance label; a namespace target
    holds one release, or several of which `release` picks one. A missing StatefulSet raises
    not_found; so does a namespace with no SeaweedFS pods (an empty list is not a healthy one).
    """
    kind = target_kind(ctx)
    check_release_input(release)
    if kind == STATEFULSET:
        namespace = ctx.resource.namespace
        sts = kubectl_json(ctx, "-n", namespace, "get", "statefulsets", ctx.resource.name)
        name = release_of_statefulset(sts)
        if release and release != name:
            raise RwError(
                "invalid_input",
                f"release {release!r} is not the release of StatefulSet {ctx.resource.name!r} ({name!r})",
            )
    else:
        namespace, name = ctx.resource.name, release
    pods = kubectl_json(ctx, "-n", namespace, "get", "pods", "-l", selector(name))["items"]
    if kind == NAMESPACE:
        found = releases_of(pods)
        if not found:
            raise RwError(
                "not_found",
                f"no pod labelled {selector(name)} in namespace {namespace!r}: no SeaweedFS release"
                + (f" named {name!r}" if name else "")
                + " is running there",
            )
        if len(found) > 1:
            raise RwError(
                "invalid_input",
                f"namespace {namespace!r} holds several SeaweedFS releases ({', '.join(found)}); "
                "pass the release input",
            )
        name = found[0]
    return Release(namespace, name, kind, pods)


def load_workloads(ctx, rel):
    """(StatefulSet and Deployment objects of the release as [(kind, object)], failed scopes): a
    forbidden list is reported with ctx.error and leaves those objects out, other failures raise."""
    sel = selector(rel.name)
    out, denied = [], []
    for kind, plural in (("StatefulSet", "statefulsets"), ("Deployment", "deployments")):
        doc, state = kubectl_json_scoped(ctx, plural, "-n", rel.namespace, "get", plural, "-l", sel)
        if state == "denied":
            denied.append(plural)
        else:
            out += [(kind, o) for o in doc["items"]]
    return out, denied


class Reads:
    """Reads that each yield one part of a result. A failed read is remembered, not raised: when
    some other read worked (or `have_result`: the task already holds a primary read) it is reported
    with ctx.error (the result is partial); when none did, the first failure is raised (a result with nothing in it is a failure, not an empty answer)."""

    def __init__(self, ctx, have_result=False):
        self.ctx = ctx
        self.failures = []
        self.successes = 1 if have_result else 0

    def run(self, scope, fn):
        try:
            value = fn()
        except RwError as exc:
            if exc.code == "invalid_input":
                raise
            self.failures.append((scope, exc))
            return None
        self.successes += 1
        return value

    def finish(self):
        if self.failures and not self.successes:
            raise self.failures[0][1]
        for scope, exc in self.failures:
            self.ctx.error(
                exc.code,
                f"{scope}: {exc.message}"[:MESSAGE_LIMIT],
                scope=scope,
                hint=getattr(exc, "hint", None),
            )


def pod_port(ctx, role):
    return getattr(ctx.settings, PORT_SETTINGS[role])


def get_text(ctx, rel, pod, role, path):
    return proxy_get(
        ctx, rel.namespace, pod["metadata"]["name"], pod_port(ctx, role), path, ctx.settings.scheme
    )


def get_json(ctx, rel, pod, role, path, what):
    return proxy_json(
        ctx,
        rel.namespace,
        pod["metadata"]["name"],
        pod_port(ctx, role),
        path,
        what,
        ctx.settings.scheme,
    )


def healthz(ctx, rel, pod, role, path):
    """'ok' for a 2xx answer, 'unhealthy' when the process answered with an error status (the body
    is not shown by the API server, so the reason is not known); an unreachable pod, a denial or a
    missing endpoint raises."""
    try:
        get_text(ctx, rel, pod, role, path)
    except ProxyError as exc:
        if exc.kind == "upstream_error":
            return "unhealthy"
        raise
    return "ok"


def first_master_json(ctx, rel, path, what, reads):
    """The JSON document at `path` from the first master pod that answers. Pods tried before it that
    failed are remembered in `reads` (reported once something worked). Raises when none answers or
    there is no Running master pod to ask."""
    ok, rest = rel.role_pods("master")
    if not ok:
        raise RwError(
            "unreachable",
            "no Running master pod to ask"
            + (
                f" ({', '.join(p['metadata']['name'] for p in rest)} not Running)"
                if rest
                else " (no pod serves the master role)"
            ),
            scope="master",
        )
    failed = []
    for pod in ok:
        try:
            doc = get_json(ctx, rel, pod, "master", path, what)
        except RwError as exc:
            failed.append((f"master/{pod['metadata']['name']}", exc))
            continue
        reads.failures.extend(failed)
        return doc
    raise failed[-1][1]
