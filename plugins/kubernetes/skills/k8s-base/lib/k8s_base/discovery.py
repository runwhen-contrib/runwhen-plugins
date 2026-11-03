"""k8s-base/discover: enumerate the cluster's resources with kubectl and build the rw.discovery.v1
sync. Types come from integration/types.yaml (discovery_types.TYPES, non-ephemeral only); a type
whose API the cluster does not serve is recorded `excluded`, not an error; a list that is
forbidden or fails is a scoped error (`ctx.error(code, scope=<type>)`) and a non-complete
partition, and the run goes on. `kubectl api-resources` that exits non-zero but still prints its
table (one aggregated API group down) is used: the types of the groups named in stderr are
`failed`, never `excluded`, and each group is a scoped `dependency` error. A `deadline` bounds the
whole enumeration: once past, no list is started, every type not yet listed is a `failed`
partition with a scoped `timeout` error, and the caller still pushes and commits.

Partitions are what papi sweeps by: only `complete` ones. So a partition is complete only when the
list was read in full; a narrowed scope (the `namespaces` setting), a forbidden or failed list and
a list cut at maxItems are never complete.
"""

import json
import re
import time
from urllib.parse import urlsplit

from k8s_base import discovery_doc as dd
from k8s_base.discovery_types import TYPES
from k8s_base.errors import RwError
from k8s_base.k8s import classify_error, run_kubectl, run_kubectl_result

LIST_TIMEOUT = 50
SIDE_TIMEOUT = 20
EXCLUDED_TYPES = {"secret": "secrets are never synced"}
MAX_UID = 1024
_now = time.monotonic  # replaced in tests
_GROUP_VERSION = re.compile(r"([A-Za-z0-9][A-Za-z0-9.-]*)/v\d+(?:alpha\d+|beta\d+)?\b")


def parse_served(text):
    """{(group, kind): (plural, namespaced)} from `kubectl api-resources --verbs=list -o wide`.
    Columns are read at the header's positions: SHORTNAMES and others may be empty."""
    lines = text.splitlines()
    if not lines:
        return {}
    header = lines[0]
    cols = [
        header.find(c) for c in ("NAME", "SHORTNAMES", "APIVERSION", "NAMESPACED", "KIND", "VERBS")
    ]
    if cols[0] != 0 or any(c < 0 for c in cols) or cols != sorted(cols):
        raise RwError(
            "internal", "kubectl api-resources printed columns this task does not recognise"
        )
    served = {}
    for line in lines[1:]:
        if not line.strip():
            continue
        cell = [line[cols[i] : cols[i + 1]].strip() for i in range(5)]
        name, _, version, namespaced, kind = cell
        group = version.rpartition("/")[0]
        served.setdefault((group, kind), (name, namespaced == "true"))
    return served


def resource_arg(plural, group):
    return f"{plural}.{group}" if group else plural


def get_json(ctx, argv, timeout=LIST_TIMEOUT):
    stdout, _ = run_kubectl(ctx, argv, timeout=timeout)
    try:
        value = json.loads(stdout)
    except ValueError:
        raise RwError("internal", f"kubectl {argv[0]} did not print JSON") from None
    if not isinstance(value, dict):
        raise RwError("internal", f"kubectl {argv[0]} did not print a JSON object")
    return value


def list_items(ctx, argv, timeout=LIST_TIMEOUT):
    """The `items` of a `kubectl get <list> -o json`. A document without a list of items is not
    an empty list: it is a reply this task does not understand."""
    items = get_json(ctx, argv, timeout).get("items")
    if not isinstance(items, list):
        raise RwError("internal", f"kubectl {argv[0]} printed no items list")
    return items


def groups_in(stderr):
    """The API groups a failed `kubectl api-resources` names (`metrics.k8s.io/v1beta1: ...`)."""
    seen = []
    for match in _GROUP_VERSION.finditer(stderr):
        if match.group(1) not in seen:
            seen.append(match.group(1))
    return seen


def api_resources(ctx):
    """(served, failed groups). `kubectl api-resources` exits 1 when one group's discovery
    fails yet still prints the table of the others: that table is used, each group named in
    stderr is a scoped dependency error. Without a table the failure is the run's."""
    rc, stdout, stderr = run_kubectl_result(
        ctx, ["api-resources", "--verbs=list", "-o", "wide"], timeout=SIDE_TIMEOUT
    )
    if rc == 0:
        return parse_served(stdout), {}
    code = classify_error(stderr, rc)
    try:
        served = parse_served(stdout)
    except RwError:
        served = {}
    if not served:
        raise RwError(code, stderr[:1024] or f"kubectl exited {rc}")
    text = stderr.splitlines()[0][:300] if stderr else f"kubectl exited {rc}"
    failed = {}
    for group in groups_in(stderr):
        failed[group] = f"API group {group} could not be discovered: {text}"
        ctx.error("dependency", failed[group], scope=group)
    if not failed:
        ctx.error(
            code,
            f"kubectl api-resources failed, its table may be incomplete: {text}",
            scope="api-resources",
        )
    return served, failed


def scoped_error(ctx, exc, scope):
    ctx.error(exc.code, exc.message, scope=scope)


def cluster_facts(ctx):
    """(server URL without credentials, the /version document). Both are needed: a cluster that
    does not answer /version has nothing to report, so that failure propagates."""
    version = get_json(ctx, ["get", "--raw", "/version"], SIDE_TIMEOUT)
    stdout, _ = run_kubectl(
        ctx,
        ["config", "view", "--minify", "-o", "jsonpath={.clusters[0].cluster.server}"],
        timeout=SIDE_TIMEOUT,
    )
    return server_url(stdout), version


def server_url(text):
    """scheme://host[:port]/path of the kubeconfig's server, credentials dropped. The host is kept
    as written, so an IPv6 literal keeps its brackets."""
    try:
        parts = urlsplit(text.strip())
        parts.port  # noqa: B018 - a bad port raises ValueError here
    except ValueError:
        raise RwError("invalid_input", "the kubeconfig server URL is not a valid URL") from None
    host = parts.netloc.rpartition("@")[2]
    if not parts.scheme or not host:
        raise RwError("invalid_input", "the kubeconfig server URL has no scheme or host")
    return f"{parts.scheme}://{host}{parts.path}".rstrip("/")


def kube_system_uid(ctx):
    """The cluster's canonical key, or None (with a scoped error) when it cannot be read."""
    try:
        stdout, _ = run_kubectl(
            ctx,
            ["get", "namespaces", "kube-system", "-o", "jsonpath={.metadata.uid}"],
            timeout=SIDE_TIMEOUT,
        )
    except RwError as exc:
        if exc.code not in ("permission_denied", "not_found"):
            raise
        ctx.error(
            exc.code,
            f"cannot read the kube-system namespace, so root.canonicalKey is not reported: {exc.message}",
            scope="namespace/kube-system",
        )
        return None
    uid = stdout.strip()
    return uid if 0 < len(uid) <= MAX_UID else None


class Run:
    """One discovery run: collects items (root first, then namespaces, then the rest) and partitions."""

    def __init__(
        self, ctx, scope_path, cluster, namespaces, max_items, max_string, deadline_at=None
    ):
        self.ctx = ctx
        self.scope = scope_path
        self.cluster = cluster
        self.requested = namespaces
        self.max_items = max_items
        self.max_string = max_string
        self.deadline_at = deadline_at
        self.items = []
        self.rest = []
        self.partitions = []
        self.counts = {}

    def expired(self):
        return self.deadline_at is not None and _now() >= self.deadline_at

    def timed_out(self, type_name, parent_path=None, report=True):
        """A list that was not started because the deadline passed: failed, so nothing is swept."""
        if report:
            self.ctx.error(
                "timeout", "the deadline passed before this type was listed", scope=type_name
            )
        self.partitions.append(
            dd.partition(
                type_name, "failed", parent_path, reason="the deadline passed before it was listed"
            )
        )

    def chain(self, *pairs):
        return [("cluster", self.cluster), *pairs]

    def add(self, items, type_name, bucket):
        bucket.extend(items)
        self.counts[type_name] = self.counts.get(type_name, 0) + len(items)

    def ns_path(self, ns):
        return f"{self.scope}/namespaces/{ns}"

    def cluster_item(self, server, version, uid):
        doc = {
            "server": server,
            "version": {
                k: version[k] for k in ("gitVersion", "major", "minor", "platform") if k in version
            },
        }
        item = {
            "identity": {
                "platform": dd.PLATFORM,
                "chain": [{"type": "cluster", "name": self.cluster}],
            },
            "displayName": self.cluster[:255],
            "labels": {},
            "document": doc,
        }
        if uid:
            item["providerUid"] = uid[:255]
        self.add([item], "cluster", self.items)

    def namespaces(self, served):
        """The namespaces in scope, as a list of names; namespace items and the namespace partition."""
        row = served.get(("", "Namespace"))
        if row is None:
            self.partitions.append(
                dd.partition(
                    "namespace", "excluded", reason="the cluster does not serve namespaces"
                )
            )
            return []
        if self.requested:
            return self._requested_namespaces()
        try:
            listed = list_items(self.ctx, ["get", "namespaces", "-o", "json"])
        except RwError as exc:
            scoped_error(self.ctx, exc, "namespace")
            status = "forbidden" if exc.code == "permission_denied" else "failed"
            self.partitions.append(
                dd.partition("namespace", status, self.scope, reason=exc.message)
            )
            return []
        objs = [o for o in listed if (o.get("metadata") or {}).get("name")]
        objs, cut_at = self._cap(objs)
        self._add_namespaces(objs)
        self.partitions.append(
            dd.partition("namespace", "failed", self.scope, reason=cut_at)
            if cut_at
            else dd.partition("namespace", "complete", self.scope, len(objs))
        )
        return sorted(o["metadata"]["name"] for o in objs)

    def _requested_namespaces(self):
        found = []
        names = []
        for name in self.requested:
            try:
                obj = get_json(self.ctx, ["get", "namespaces", name, "-o", "json"], SIDE_TIMEOUT)
            except RwError as exc:
                scoped_error(self.ctx, exc, f"namespace/{name}")
                if exc.code != "permission_denied":
                    continue
                # not readable, but asked for: its children may be readable with a namespaced Role.
                # No stub item; the namespace partition is never complete, so nothing is swept.
                names.append(name)
                continue
            found.append(obj)
            names.append(name)
        self._add_namespaces(found)
        self.partitions.append(
            dd.partition(
                "namespace",
                "excluded",
                self.scope,
                reason="the namespaces setting narrows the scope",
            )
        )
        return names

    def _add_namespaces(self, objs):
        items = [
            dd.build_item(
                self.chain(("namespace", o["metadata"]["name"])), o, "Namespace", self.max_string
            )
            for o in objs
        ]
        self.add(items, "namespace", self.items)

    def _cap(self, objs):
        if len(objs) > self.max_items:
            return objs[: self.max_items], f"{len(objs)} items, cut at maxItems {self.max_items}"
        return objs, None

    def cluster_scoped(self, type_name, plural, group, kind):
        arg = resource_arg(plural, group)
        try:
            objs = list_items(self.ctx, ["get", arg, "-o", "json"])
        except RwError as exc:
            self._failed(type_name, exc, self.scope)
            return
        objs, cut_at = self._cap(objs)
        self._push(type_name, kind, objs, None, self.scope, cut_at)

    def namespaced(self, type_name, plural, group, kind, namespaces):
        arg = resource_arg(plural, group)
        if not self.requested:
            try:
                objs = list_items(self.ctx, ["get", arg, "--all-namespaces", "-o", "json"])
            except RwError as exc:
                self._failed(type_name, exc, None)
                return
            by_ns = {ns: [] for ns in namespaces}
            for obj in objs:
                ns = (obj.get("metadata") or {}).get("namespace")
                if ns in by_ns:  # created since the namespace list was read: next run
                    by_ns[ns].append(obj)
            for ns in namespaces:
                chunk, cut_at = self._cap(by_ns[ns])
                self._push(type_name, kind, chunk, ns, self.ns_path(ns), cut_at)
            return
        for i, ns in enumerate(namespaces):
            if self.expired():
                for rest in namespaces[i:]:
                    self.timed_out(type_name, self.ns_path(rest), report=rest == ns)
                return
            try:
                objs = list_items(self.ctx, ["get", arg, "--namespace", ns, "-o", "json"])
            except RwError as exc:
                self._failed(type_name, exc, self.ns_path(ns))
                continue
            objs, cut_at = self._cap(objs)
            self._push(type_name, kind, objs, ns, self.ns_path(ns), cut_at)

    def _push(self, type_name, kind, objs, ns, parent_path, cut_at):
        if type_name == "job":
            objs = [o for o in objs if not dd.owned_by_cronjob(o)]
        parent = (("namespace", ns),) if ns else ()
        items = [
            dd.build_item(
                self.chain(*parent, (type_name, o["metadata"]["name"])), o, kind, self.max_string
            )
            for o in objs
        ]
        self.add(items, type_name, self.rest)
        self.partitions.append(
            dd.partition(type_name, "failed", parent_path, reason=cut_at)
            if cut_at
            else dd.partition(type_name, "complete", parent_path, len(items))
        )

    def _failed(self, type_name, exc, parent_path):
        scoped_error(self.ctx, exc, type_name)
        status = "forbidden" if exc.code == "permission_denied" else "failed"
        self.partitions.append(dd.partition(type_name, status, parent_path, reason=exc.message))


def discover(ctx, scope_path, cluster, namespaces, max_items, max_string, deadline_s=None):
    """Run the whole enumeration. Returns (run, root uid, duration ms)."""
    started = _now()
    server, version = cluster_facts(ctx)
    uid = kube_system_uid(ctx)
    served, failed_groups = api_resources(ctx)

    deadline_at = None if deadline_s is None else started + deadline_s
    run = Run(ctx, scope_path, cluster, namespaces, max_items, max_string, deadline_at)
    run.cluster_item(server, version, uid)
    ns_list = run.namespaces(served)
    for type_name, parent, plural, group, kind in TYPES:
        if type_name == "namespace":
            continue
        if type_name in EXCLUDED_TYPES:
            run.partitions.append(
                dd.partition(type_name, "excluded", reason=EXCLUDED_TYPES[type_name])
            )
            continue
        row = served.get((group, kind))
        want_namespaced = parent == "namespace"
        if row is None and group in failed_groups:
            # not "not served": the group's discovery failed, so the cluster may well serve it
            run.partitions.append(dd.partition(type_name, "failed", reason=failed_groups[group]))
            continue
        if row is None or row[1] != want_namespaced:
            why = (
                "API not served by this cluster"
                if row is None
                else "served with a different scope than types.yaml declares"
            )
            run.partitions.append(dd.partition(type_name, "excluded", reason=why))
            continue
        served_plural = row[0]
        if not want_namespaced:
            if run.expired():
                run.timed_out(type_name)
            else:
                run.cluster_scoped(type_name, served_plural, group, kind)
        elif ns_list or run.requested:
            if run.expired():
                run.timed_out(type_name)
            else:
                run.namespaced(type_name, served_plural, group, kind, ns_list)
        else:
            run.partitions.append(
                dd.partition(
                    type_name,
                    "excluded",
                    reason="no namespace list to enumerate (namespaces not listable; set the namespaces setting)",
                )
            )
    run.items.extend(run.rest)
    return run, uid, int((_now() - started) * 1000)
