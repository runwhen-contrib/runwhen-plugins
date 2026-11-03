"""Pure functions behind the k8s-postgres-operator-ops tasks.

Everything here takes already-parsed Kubernetes or `patronictl list -f json` data (or plain
inputs) and returns facts or a plan: no kubectl, no clock, no environment. Scripts fetch, call
these, run the mutating command and return. Every guard that can refuse a write lives here so a
unit test can show it.
"""

import json
import re

from k8s_postgres_operator_ops.errors import RwError

TEXT_LIMIT = 1024
NESTED_LIMIT = 50
MAX_WAIT_SECONDS = 240  # the task timeout is 300s

CRUNCHY = "PostgresCluster"
ZALANDO = "postgresql"
KINDS = {
    CRUNCHY: {
        "type": "kubernetes/postgrescluster",
        "plural": "postgresclusters",
        "resource": "postgresclusters.postgres-operator.crunchydata.com",
        "operator": "crunchy",
        "container": "database",
        "role_label": "postgres-operator.crunchydata.com/role",
    },
    ZALANDO: {
        "type": "kubernetes/postgresql",
        "plural": "postgresqls",
        "resource": "postgresqls.acid.zalan.do",
        "operator": "zalando",
        "container": "postgres",
        "role_label": "spilo-role",
    },
}
_BY_TYPE = {v["type"]: k for k, v in KINDS.items()}
_BY_PLURAL = {v["plural"]: k for k, v in KINDS.items()}

PRIMARY_ROLES = ("master", "primary")
LEADER_ROLES = ("leader", "standby leader")
UP_STATES = ("running", "streaming")

# spec.metadata.annotations of a PostgresCluster reach the pod template of every instance;
# changing this one makes the operator roll the instances.
RESTART_ANNOTATION = "postgres-operator.crunchydata.com/restarted"


# --------------------------------------------------------------- target ----


def resolve_kind(resource_type, resource_path=None):
    """Operator Kind from `ctx.resource.type`, else from the plural segment of
    `ctx.resource.path`. Anything else raises RwError('invalid_input')."""
    if resource_type in _BY_TYPE:
        return _BY_TYPE[resource_type]
    if not resource_type and resource_path:
        for segment in reversed(str(resource_path).strip("/").split("/")):
            if segment in _BY_PLURAL:
                return _BY_PLURAL[segment]
    raise RwError(
        "invalid_input",
        f"k8s-postgres-operator-ops does not apply to {resource_type or resource_path!r}",
    )


def pod_selector(kind, name):
    """Label selector for the cluster's database pods (see the health skill)."""
    if kind == CRUNCHY:
        return f"postgres-operator.crunchydata.com/cluster={name},postgres-operator.crunchydata.com/instance"
    return f"application=spilo,cluster-name={name}"


def limit_text(value, limit=TEXT_LIMIT):
    """(text, truncated) for a free-text field."""
    if value is None:
        return None, False
    value = str(value)
    return value[:limit], len(value) > limit


# ------------------------------------------------------------ gitops ----

_GITOPS_PREFIXES = (
    ("flux", "kustomize.toolkit.fluxcd.io/"),
    ("flux", "helm.toolkit.fluxcd.io/"),
    ("flux", "fluxcd.io/"),
    ("argocd", "argocd.argoproj.io/"),
)
_GITOPS_MANAGED_BY = {"flux": "flux", "fluxcd": "flux", "argocd": "argocd", "argo-cd": "argocd"}


def gitops(obj):
    """Whether Flux or Argo CD labels or annotations are on the object. A fact for the approver,
    never a refusal; only keys are reported."""
    meta = obj.get("metadata", {})
    signals, tool = [], None
    for where, mapping in (("label", meta.get("labels")), ("annotation", meta.get("annotations"))):
        for key, value in sorted((mapping or {}).items()):
            found = next((t for t, prefix in _GITOPS_PREFIXES if key.startswith(prefix)), None)
            if key == "app.kubernetes.io/managed-by":
                found = _GITOPS_MANAGED_BY.get(str(value).lower())
            if found:
                tool = tool or found
                signals.append({"kind": where, "key": key, "tool": found})
    return {"managed": bool(signals), "tool": tool, "signals": signals[:NESTED_LIMIT]}


def target_view(kind, obj, patroni_cluster=None):
    """The `target` output every task returns: the cluster object acted on, the Patroni cluster
    name when it was read, and whether a GitOps controller manages the object."""
    meta, spec = obj["metadata"], obj.get("spec") or {}
    g = gitops(obj)
    version = (
        spec.get("postgresVersion")
        if kind == CRUNCHY
        else (spec.get("postgresql") or {}).get("version")
    )
    return {
        "kind": kind,
        "operator": KINDS[kind]["operator"],
        "name": meta["name"],
        "namespace": meta.get("namespace"),
        "generation": meta.get("generation"),
        "postgresVersion": None if version is None else str(version),
        "patroniCluster": patroni_cluster,
        "gitopsManaged": g["managed"],
        "gitops": g,
    }


# --------------------------------------------------------------- inputs ----

_DURATION = re.compile(r"(\d+)(ms|s|m|h|d)")
_UNIT_SECONDS = {"ms": 0.001, "s": 1, "m": 60, "h": 3600, "d": 86400}
_MEMBER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,252}$")


def parse_duration(text, name="timeout"):
    """'90s', '2m', '1m30s', '500ms', '1h' -> seconds (float). Bad input raises
    RwError('invalid_input')."""
    s = str(text).strip().lower()
    parts = _DURATION.findall(s)
    if not parts or "".join(f"{n}{u}" for n, u in parts) != s:
        raise RwError("invalid_input", f"{name} must be a duration such as 90s or 2m, got {text!r}")
    return sum(int(n) * _UNIT_SECONDS[u] for n, u in parts)


def check_timeout(text):
    """The wait in seconds (0 does not wait); more than MAX_WAIT_SECONDS is invalid_input."""
    wait = parse_duration(text)
    if not 0 <= wait <= MAX_WAIT_SECONDS:
        raise RwError(
            "invalid_input", f"timeout must be between 0s and {MAX_WAIT_SECONDS}s, got {text!r}"
        )
    return wait


def clean_member(value, name="member"):
    """A Patroni member name taken from an input. It becomes one argv element of patronictl, so it
    is held to the characters a pod name has (nothing a shell or an option could mean)."""
    if value is None or not str(value).strip():
        raise RwError("invalid_input", f"{name} is required")
    text = str(value).strip()
    if not _MEMBER.match(text):
        raise RwError(
            "invalid_input",
            f"{name} must be a member name (letters, digits, '.', '_' or '-', starting with a letter or digit), got {text[:64]!r}",
        )
    return text


# ----------------------------------------------------------------- pods ----


def pod_ready(pod):
    for c in (pod.get("status") or {}).get("conditions") or []:
        if c.get("type") == "Ready":
            return c.get("status") == "True"
    return False


def pod_role(kind, pod):
    labels = (pod.get("metadata") or {}).get("labels") or {}
    return labels.get(KINDS[kind]["role_label"])


def container_running(pod, container):
    for c in (pod.get("status") or {}).get("containerStatuses") or []:
        if c.get("name") == container:
            return "running" in (c.get("state") or {})
    return False


def exec_pod(kind, pods, avoid=()):
    """The pod to run patronictl in: Running, not being deleted, database container running.
    Ready first, then one that is not in `avoid` (the member being changed, the leader being
    replaced), then not the primary, then by name. None when there is none."""
    container = KINDS[kind]["container"]
    live = [
        p
        for p in pods
        if (p.get("status") or {}).get("phase") == "Running"
        and (p.get("metadata") or {}).get("deletionTimestamp") is None
        and container_running(p, container)
    ]
    if not live:
        return None
    return sorted(
        live,
        key=lambda p: (
            not pod_ready(p),
            p["metadata"]["name"] in avoid,
            pod_role(kind, p) in PRIMARY_ROLES,
            p["metadata"]["name"],
        ),
    )[0]


def pod_summary(kind, pod):
    meta, status = pod.get("metadata") or {}, pod.get("status") or {}
    annotations = meta.get("annotations") or {}
    return {
        "name": meta.get("name"),
        "phase": status.get("phase"),
        "ready": pod_ready(pod),
        "role": pod_role(kind, pod),
        "startTime": status.get("startTime"),
        "restartAnnotation": annotations.get(RESTART_ANNOTATION),
    }


# -------------------------------------------------------------- patroni ----


def parse_patroni(text):
    """Members from `patronictl list -f json`. Anything that is not a JSON array of objects is an
    `internal` error, never an empty list."""
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise RwError("internal", f"patronictl list did not print JSON: {exc}") from None
    if not isinstance(data, list) or not all(isinstance(m, dict) for m in data):
        raise RwError("internal", "patronictl list -f json did not print an array of members")
    return data


def _lag(raw):
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return raw
    try:
        return float(str(raw).strip())
    except ValueError:
        return None


def member_view(raw):
    role = str(raw.get("Role") or "")
    state = str(raw.get("State") or "")
    tl = raw.get("TL")
    return {
        "name": raw.get("Member"),
        "role": role or None,
        "state": state or None,
        "leader": role.lower() in LEADER_ROLES,
        "running": state.lower() in UP_STATES,
        "timeline": tl if isinstance(tl, int) and not isinstance(tl, bool) else None,
        "lagMb": _lag(raw.get("Lag in MB")),
    }


def members(raw_members):
    return [member_view(m) for m in raw_members]


def patroni_cluster_name(raw_members):
    """The Patroni scope the commands take (the `Cluster` column). Crunchy's is `<name>-ha`, so it
    is read, never derived from the object's name."""
    for m in raw_members:
        if m.get("Cluster"):
            return str(m["Cluster"])
    raise RwError("internal", "patronictl list did not name the cluster (no Cluster field)")


def leader_of(views):
    """The leader member view, None when there is none."""
    return next((m for m in views if m["leader"]), None)


def find_member(views, name):
    """The named member. A name that is not a member is invalid_input and lists the members."""
    found = next((m for m in views if m["name"] == name), None)
    if found is None:
        names = ", ".join(str(m["name"]) for m in views[:NESTED_LIMIT]) or "none"
        raise RwError(
            "invalid_input", f"{name!r} is not a member of the Patroni cluster (members: {names})"
        )
    return found


def check_reinit(views, member):
    """The member to reinitialise. Refuses the leader: reinit wipes the data directory and
    re-clones it from the leader, so on the leader it would destroy the only good copy."""
    found = find_member(views, member)
    if found["leader"]:
        raise RwError(
            "invalid_input",
            f"{member!r} is the leader ({found['role']}); reinit wipes the member and clones it from the leader. "
            "Switch over first, then reinitialise a replica.",
        )
    return found


def plan_switchover(views, candidate):
    """What to run: ('switchover', leader, candidate-or-None, chosen_by) when a healthy leader
    exists, else ('failover', old-leader-or-None, candidate, chosen_by).

    A candidate must be another member that is running or streaming. A failover needs one
    (patronictl refuses without), so none given means the up replica with the least known lag
    (unknown lag last, then by name). No member to promote is invalid_input."""
    leader = leader_of(views)
    healthy = leader is not None and leader["running"]
    chosen = None
    if candidate is not None:
        chosen = find_member(views, candidate)
        if chosen["leader"]:
            raise RwError("invalid_input", f"{candidate!r} is already the leader")
        if not chosen["running"]:
            raise RwError(
                "invalid_input",
                f"candidate {candidate!r} is not running or streaming (state {chosen['state']!r})",
            )
    if healthy:
        return "switchover", leader, chosen, "input" if chosen else None
    if chosen is None:
        pool = [m for m in views if not m["leader"] and m["running"]]
        if not pool:
            raise RwError(
                "invalid_input",
                "there is no running or streaming member to promote; nothing was changed",
            )
        pool.sort(key=lambda m: (m["lagMb"] is None, m["lagMb"] or 0, str(m["name"])))
        chosen = pool[0]
        return "failover", leader, chosen, "auto"
    return "failover", leader, chosen, "input"


def leader_change_done(views, mode, old_leader, candidate):
    """Whether the cluster now has a running leader that is the one asked for: the candidate when
    one was named, else any member other than the old leader."""
    now = leader_of(views)
    if now is None or not now["running"]:
        return False
    if candidate is not None:
        return now["name"] == candidate["name"]
    return old_leader is None or now["name"] != old_leader["name"]


def all_members_up(views):
    return bool(views) and all(m["running"] for m in views)


def restart_patch(restarted_at):
    """The merge patch of a Crunchy restart: one annotation under spec.metadata.annotations."""
    return {"spec": {"metadata": {"annotations": {RESTART_ANNOTATION: restarted_at}}}}


def desired_instances(obj):
    """Crunchy: instances the spec asks for (replicas defaults to 1 per set)."""
    sets = (obj.get("spec") or {}).get("instances") or []
    return sum(s["replicas"] if isinstance(s.get("replicas"), int) else 1 for s in sets)


def pods_restarted(summaries, restarted_at):
    """How many pods carry the restart annotation we set, or were started at or after it."""
    count = 0
    for s in summaries:
        if s["restartAnnotation"] == restarted_at or (
            s["startTime"] and restarted_at and s["startTime"] >= restarted_at
        ):
            count += 1
    return count


def crunchy_restart_done(summaries, desired, restarted_at):
    """The rolling restart has finished: as many pods as the spec asks for, every one Ready, Running
    and restarted at or after the request. Returns (done, restarted_count, ready_count)."""
    restarted = pods_restarted(summaries, restarted_at)
    ready = sum(1 for s in summaries if s["ready"] and s["phase"] == "Running")
    done = desired > 0 and len(summaries) == desired and ready == desired and restarted == desired
    return done, restarted, ready
