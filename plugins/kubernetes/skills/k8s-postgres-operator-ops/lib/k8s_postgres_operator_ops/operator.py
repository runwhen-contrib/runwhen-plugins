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

# Time. Every write task declares `timeout: 420s` (tasks.yaml; a unit test holds the two equal).
# A wait is capped at what is left of that less a margin, so the runner never kills the task after
# a write has gone out without its result. The `timeout` input is at most MAX_WAIT_SECONDS.
TASK_TIMEOUT_SECONDS = 420
BUDGET_MARGIN_SECONDS = 30
MAX_WAIT_SECONDS = 300
WRITE_EXEC_SECONDS = 120  # one patronictl write (kubectl exec) may run this long
MIN_STEP_SECONDS = 30  # less than this left: do not start another member's restart
# After `reinit` the member may still read `streaming` until Patroni's next loop (loop_wait is 10s
# by default): it is not called back before it was seen down or this long has passed.
REINIT_SETTLE_SECONDS = 15

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
# A leader in one of these states is not coming back by itself: failover is allowed. Any other
# state that is not `running` (restarting, starting, stopping, ...) is transient: wait.
TERMINAL_LEADER_STATES = ("stopped", "crashed", "start failed", "restart failed")

# spec.metadata.annotations of a PostgresCluster reach the pod template of every pod the operator
# creates for it (instances, pgBouncer, the pgBackRest repo host); changing this one, the key
# the PGO v5 documentation uses for a rolling restart, makes the operator roll them.
RESTART_ANNOTATION = "restarted"


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
        "restartAnnotation": limit_text(annotations.get(RESTART_ANNOTATION))[0],
    }


# -------------------------------------------------------------- patroni ----


REQUIRED_MEMBER_FIELDS = ("Member", "Role", "State")


def parse_patroni(text):
    """Members from `patronictl list -f json`. Anything that is not a non-empty JSON array of
    objects that each name Member, Role and State is an `internal` error, never an empty list: a
    cluster is never acted on from a list that says nothing."""
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise RwError("internal", f"patronictl list did not print JSON: {exc}") from None
    if not isinstance(data, list) or not all(isinstance(m, dict) for m in data):
        raise RwError("internal", "patronictl list -f json did not print an array of members")
    if not data:
        raise RwError("internal", "patronictl list -f json printed an empty array: no members")
    for index, member in enumerate(data):
        missing = [f for f in REQUIRED_MEMBER_FIELDS if not member.get(f)]
        if missing:
            raise RwError(
                "internal",
                f"patronictl list -f json member {index} has no {', '.join(missing)}",
            )
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
        "pendingRestart": bool(raw.get("Pending restart")),
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
    """The member to reinitialise. Refuses the leader (reinit wipes the data directory and
    re-clones it from the leader, so on the leader it would destroy the only good copy) and a
    cluster without a leader in state `running` (there is nothing to clone from)."""
    found = find_member(views, member)
    if found["leader"]:
        raise RwError(
            "invalid_input",
            f"{member!r} is the leader ({found['role']}); reinit wipes the member and clones it from the leader. "
            "Switch over first, then reinitialise a replica.",
        )
    leader = leader_of(views)
    if leader is None or (leader["state"] or "").lower() != "running":
        what = (
            "has no leader" if leader is None else f"leader {leader['name']!r} is {leader['state']}"
        )
        raise RwError(
            "invalid_input",
            f"the Patroni cluster {what}; a reinitialised member is cloned from a running leader, "
            "so nothing was changed. Restore a leader first: kubernetes:k8s-postgres-operator-ops/switchover "
            "(it fails over to a named candidate when there is no healthy leader).",
        )
    return found


def _lag_text(member):
    return "lag unknown" if member["lagMb"] is None else f"lag {member['lagMb']:g} MB"


def plan_switchover(views, candidate):
    """What to run: (mode, leader-or-None, candidate-or-None).

    mode `switchover`: the leader is running; the candidate is optional (Patroni picks).
    mode `failover`: there is no leader, or it is in a terminal state (TERMINAL_LEADER_STATES);
    the candidate is required, must be another member that is running or streaming, and must
    have a known lag: a failover promotes it without the old leader, so a replica whose lag
    Patroni cannot tell is never chosen for the operator.
    A leader in any other state (restarting, starting, stopping ...) is transient: invalid_input,
    retry later. Nothing is chosen automatically."""
    leader = leader_of(views)
    state = (leader["state"] or "").lower() if leader else None
    if leader is not None and state != "running" and state not in TERMINAL_LEADER_STATES:
        raise RwError(
            "invalid_input",
            f"leader is {leader['state']}; retry later (leader {leader['name']!r}). "
            "Nothing was changed: a failover while the leader is only changing state could split the cluster.",
        )
    mode = "switchover" if state == "running" else "failover"
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
    if mode == "failover":
        why = (
            "there is no leader"
            if leader is None
            else f"leader {leader['name']!r} is {leader['state']}"
        )
        if chosen is None:
            up = [m for m in views if not m["leader"] and m["running"]]
            listed = (
                ", ".join(f"{m['name']} ({_lag_text(m)})" for m in up[:NESTED_LIMIT])
                or "none running"
            )
            raise RwError(
                "invalid_input",
                f"{why}, so this is a failover, which needs an explicit candidate; none is chosen for you "
                f"(running members: {listed}). Nothing was changed.",
            )
        if chosen["lagMb"] is None:
            raise RwError(
                "invalid_input",
                f"{why}, but candidate {candidate!r} has an unknown replication lag; refusing to promote it. "
                "Nothing was changed.",
            )
    return mode, leader, chosen


def leader_change_done(views, mode, old_leader, candidate):
    """Whether the cluster now has a running leader that is the one asked for: the candidate when
    one was named, else any member other than the old leader."""
    now = leader_of(views)
    if now is None or not now["running"]:
        return False
    if candidate is not None:
        return now["name"] == candidate["name"]
    return old_leader is None or now["name"] != old_leader["name"]


def restart_order(views):
    """Members in the order a rolling restart takes them: replicas first (by name), the leader
    last. Without a leader, by name."""
    return [m["name"] for m in sorted(views, key=lambda m: (m["leader"], str(m["name"])))]


def member_back(views, name):
    """The member is in the list, running or streaming, and its `Pending restart` is cleared."""
    found = next((m for m in views if m["name"] == name), None)
    return found is not None and found["running"] and not found["pendingRestart"]


class ReinitWatch:
    """`done` for the wait after a reinit: the member runs or streams again AND was either seen
    not up in an earlier reading or REINIT_SETTLE_SECONDS have passed (a reading right after the
    command may still show the state from before it)."""

    def __init__(self, member):
        self.member = member
        self.seen_down = False

    def __call__(self, views, elapsed):
        found = next((m for m in views if m["name"] == self.member), None)
        if found is None or not found["running"]:
            self.seen_down = True
            return False
        return self.seen_down or elapsed >= REINIT_SETTLE_SECONDS


# ----------------------------------------------- patronictl write output ----

# Patroni's own wording, in one place. Patroni 3.x and 4.x print, per targeted member,
#   Success: restart on member <name>          Success: reinitialize for member <name>
#   Failed: restart for member <name>, status code=503, (<reason>)
# after a cluster table. Matching is case-insensitive and tolerates on/for/of, member/node/
# instance, quotes, and restart(ed) / reinit(ialise|ialize)(d).
_NAME = r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?"
_SUBJECT = rf"(?:on|for|of)\s+(?:member|node|instance)\s+[\"']?({_NAME})"
WRITE_SUCCESS = {
    "restart": re.compile(rf"^\s*success\b[:!.-]*\s*restart\w*\s+{_SUBJECT}", re.I | re.M),
    "reinit": re.compile(rf"^\s*success\b[:!.-]*\s*reinit\w*\s+{_SUBJECT}", re.I | re.M),
}
WRITE_FAILED = re.compile(r"^\s*(?:failed\b.*|(?:switchover|failover)\s+failed\b.*)$", re.I | re.M)


def write_result_lines(text):
    """The lines of patronictl output that are not the cluster table (`+---` and `| ... |` rows)."""
    return "\n".join(
        line.rstrip()
        for line in str(text).splitlines()
        if line.strip() and not line.lstrip().startswith(("+", "|"))
    )


def verify_write(action, text, member):
    """(ok, reason): whether patronictl's output says `action` (restart, reinit) was done for
    `member`. A `Failed:` line anywhere, or no `Success: ... member <member>` line, is not ok, even
    though patronictl exited 0."""
    failed = WRITE_FAILED.search(str(text))
    if failed:
        return False, failed.group(0).strip()
    for found in WRITE_SUCCESS[action].finditer(str(text)):
        if found.group(1).lower() == member.lower():
            return True, None
    return False, f"patronictl printed no 'Success: {action} ... member {member}' line"


def switch_failed(text):
    """The `Failed:` / `Switchover failed` / `Failover failed` line of a switchover or failover's
    output, or None."""
    failed = WRITE_FAILED.search(str(text))
    return failed.group(0).strip() if failed else None


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
