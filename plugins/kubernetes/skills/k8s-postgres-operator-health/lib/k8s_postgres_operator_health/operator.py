"""Pure functions over the Crunchy PostgresCluster / Zalando postgresql objects, their member
pods, `patronictl list -f json` and the JSON one psql query prints (parsed JSON in, outputs out)."""

import json
import re
from datetime import datetime, timezone

from k8s_postgres_operator_health.errors import RwError

TEXT_LIMIT = 1024
NESTED_LIMIT = 50

CRUNCHY = "PostgresCluster"
ZALANDO = "postgresql"
# `resource` is fully qualified so that kubectl never resolves the name against another API group.
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

# Pod label values that mark the primary (older operators say master, newer primary).
PRIMARY_ROLES = ("master", "primary")
# Patroni `Role` values that are the cluster's writer.
LEADER_ROLES = ("leader", "standby leader")
# Patroni `State` values of a member that is up (replicas say streaming, the leader running).
UP_STATES = ("running", "streaming")


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
        f"k8s-postgres-operator-health does not apply to {resource_type or resource_path!r}",
    )


def pod_selector(kind, name):
    """The label selector for the cluster's database pods.

    Crunchy: the cluster label and the instance label, which the pgBackRest repo host and
    pgBouncer pods do not carry. Zalando: spilo pods of the cluster."""
    if kind == CRUNCHY:
        return f"postgres-operator.crunchydata.com/cluster={name},postgres-operator.crunchydata.com/instance"
    return f"application=spilo,cluster-name={name}"


def limit_text(value, limit=TEXT_LIMIT):
    """(text, truncated) for a free-text field."""
    if value is None:
        return None, False
    value = str(value)
    return value[:limit], len(value) > limit


def cap(items, max_items):
    """Bound a top-level list output to max_items entries."""
    return list(items)[: int(max_items)]


def parse_time(text):
    """RFC 3339 string -> aware datetime, None when absent or unparsable."""
    if not text:
        return None
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def age_seconds(text, now):
    """Whole seconds from an RFC 3339 time to `now`, None when the time is absent or unparsable."""
    moment = parse_time(text)
    return None if moment is None else max(0, int((now - moment).total_seconds()))


def _int(value):
    return value if isinstance(value, int) and not isinstance(value, bool) else None


# ------------------------------------------------------------------ pods ----


def container_views(pod):
    """Every container of the pod, init containers (and native sidecars) included with init true."""
    status = pod.get("status") or {}
    views = []
    for init, key in ((True, "initContainerStatuses"), (False, "containerStatuses")):
        for c in status.get(key) or []:
            state = c.get("state") or {}
            which = next((s for s in ("running", "waiting", "terminated") if s in state), None)
            views.append(
                {
                    "name": c.get("name"),
                    "init": init,
                    "ready": bool(c.get("ready")),
                    "restartCount": c.get("restartCount", 0),
                    "state": which,
                    "reason": (state.get(which) or {}).get("reason") if which else None,
                }
            )
    return views[:NESTED_LIMIT]


def pod_ready(pod):
    for c in (pod.get("status") or {}).get("conditions") or []:
        if c.get("type") == "Ready":
            return c.get("status") == "True"
    return False


def pod_role(kind, pod):
    """The operator's role label on the pod (master / primary / replica), None when absent."""
    labels = (pod.get("metadata") or {}).get("labels") or {}
    return labels.get(KINDS[kind]["role_label"])


def pod_view(kind, pod):
    meta, spec, status = pod.get("metadata") or {}, pod.get("spec") or {}, pod.get("status") or {}
    containers = container_views(pod)
    role = pod_role(kind, pod)
    return {
        "name": meta.get("name"),
        "phase": status.get("phase"),
        "ready": pod_ready(pod),
        "restarts": sum(c["restartCount"] for c in containers),
        "role": role,
        "primary": role in PRIMARY_ROLES,
        "node": spec.get("nodeName"),
        "startTime": status.get("startTime"),
        "deleting": meta.get("deletionTimestamp") is not None,
        "containers": containers,
    }


def members_view(kind, pods, selector, max_items):
    """The `members` output: the cluster's database pods with ready and restarts."""
    views = sorted((pod_view(kind, p) for p in pods), key=lambda v: v["name"] or "")
    items = cap(views, max_items)
    return {
        "selector": selector,
        "count": len(views),
        "readyCount": sum(1 for v in views if v["ready"]),
        "truncated": len(views) > len(items),
        "items": items,
    }


def container_running(pod, container):
    for c in (pod.get("status") or {}).get("containerStatuses") or []:
        if c.get("name") == container:
            return "running" in (c.get("state") or {})
    return False


def exec_candidates(kind, pods):
    """Pods that can be exec'd into (Running, not being deleted, database container running),
    best first: Ready, then primary, then by name."""
    container = KINDS[kind]["container"]
    live = [
        p
        for p in pods
        if (p.get("status") or {}).get("phase") == "Running"
        and (p.get("metadata") or {}).get("deletionTimestamp") is None
        and container_running(p, container)
    ]
    return sorted(
        live,
        key=lambda p: (
            not pod_ready(p),
            pod_role(kind, p) not in PRIMARY_ROLES,
            p["metadata"]["name"],
        ),
    )


def primary_pods(kind, pods):
    """Exec candidates carrying the primary role label, best first."""
    return [p for p in exec_candidates(kind, pods) if pod_role(kind, p) in PRIMARY_ROLES]


# --------------------------------------------------------- cluster-status ----


def _spec(obj):
    return obj.get("spec") or {}


def _status(obj):
    st = obj.get("status")
    return st if isinstance(st, dict) else {}


def condition_views(obj):
    views = []
    for c in (_status(obj).get("conditions") or [])[:NESTED_LIMIT]:
        message, truncated = limit_text(c.get("message"))
        views.append(
            {
                "type": c.get("type"),
                "status": c.get("status"),
                "reason": c.get("reason"),
                "message": message,
                "truncated": truncated,
                "observedGeneration": c.get("observedGeneration"),
                "lastTransitionTime": c.get("lastTransitionTime"),
            }
        )
    return views


def instances_view(kind, obj, pods):
    """Instances desired and ready.

    Crunchy: desired from spec.instances[].replicas (1 when unset), ready from
    status.instances[].readyReplicas (null while the operator has written no status).
    Zalando: desired from spec.numberOfInstances; the object carries no readiness, so ready is
    the number of Ready member pods (null when the pods could not be listed)."""
    spec, status = _spec(obj), _status(obj)
    if kind == CRUNCHY:
        by_name = {i.get("name"): i for i in status.get("instances") or []}
        sets = []
        for s in (spec.get("instances") or [])[:NESTED_LIMIT]:
            got = by_name.get(s.get("name"))
            desired = s.get("replicas")
            sets.append(
                {
                    "name": s.get("name"),
                    "desired": desired if isinstance(desired, int) else 1,
                    "replicas": _int((got or {}).get("replicas")),
                    "ready": _int((got or {}).get("readyReplicas")),
                    "updated": _int((got or {}).get("updatedReplicas")),
                }
            )
        have_status = "instances" in status
        return {
            "desired": sum(s["desired"] for s in sets),
            "ready": sum(s["ready"] or 0 for s in sets) if have_status else None,
            "readySource": "status",
            "sets": sets,
        }
    desired = spec.get("numberOfInstances")
    return {
        "desired": desired if isinstance(desired, int) else None,
        "ready": None if pods is None else sum(1 for p in pods if pod_ready(p)),
        "readySource": "pods",
        "sets": [],
    }


def cluster_view(kind, obj, pods):
    meta, spec, status = obj.get("metadata") or {}, _spec(obj), _status(obj)
    generation, observed = meta.get("generation"), status.get("observedGeneration")
    if kind == CRUNCHY:
        version = spec.get("postgresVersion")
    else:
        version = (spec.get("postgresql") or {}).get("version")
    return {
        "kind": kind,
        "operator": KINDS[kind]["operator"],
        "apiVersion": obj.get("apiVersion"),
        "name": meta.get("name"),
        "namespace": meta.get("namespace"),
        "creationTimestamp": meta.get("creationTimestamp"),
        "deleting": meta.get("deletionTimestamp") is not None,
        "generation": generation,
        "observedGeneration": observed,
        # Zalando writes no observedGeneration, so null there; false while Crunchy has not seen the latest spec
        "generationObserved": None
        if generation is None or observed is None
        else observed == generation,
        "postgresVersion": None if version is None else str(version),
        "image": spec.get("image") if kind == CRUNCHY else None,
        "shutdown": bool(spec.get("shutdown", False)) if kind == CRUNCHY else None,
        # Zalando: Running, Creating, Updating, UpdateFailed, SyncFailed, AddFailed, Invalid ...
        "clusterStatus": status.get("PostgresClusterStatus") if kind == ZALANDO else None,
        "instances": instances_view(kind, obj, pods),
        "conditions": condition_views(obj),
    }


def _backup_entry(b, now):
    return {
        "type": b.get("type"),
        "repo": b.get("repo"),
        "cronJobName": b.get("cronJobName"),
        "startTime": b.get("startTime"),
        "completionTime": b.get("completionTime"),
        "ageSeconds": age_seconds(b.get("completionTime"), now),
        "succeeded": _int(b.get("succeeded")),
        "failed": _int(b.get("failed")),
        "active": _int(b.get("active")),
    }


def _job_view(job):
    if not isinstance(job, dict):
        return None
    return {
        "id": job.get("id"),
        "repo": job.get("repo") or job.get("repoName"),
        "startTime": job.get("startTime"),
        "completionTime": job.get("completionTime"),
        "finished": bool(job.get("finished", False)),
        "succeeded": _int(job.get("succeeded")),
        "failed": _int(job.get("failed")),
        "active": _int(job.get("active")),
    }


def backups_view(kind, obj, now):
    """pgBackRest facts from the PostgresCluster status.

    Only scheduled backups appear in status.pgbackrest.scheduledBackups, each with the time its
    Job completed: the latest completed full, incremental and differential are the newest
    completionTime of that type. A schedule that never completed has no completionTime (counted
    in neverCompleted). Absent scheduledBackups gives null for every `last`, never an age of 0.
    A Zalando postgresql object carries no backup status: supported false."""
    if kind != CRUNCHY:
        return {
            "supported": False,
            "note": "A Zalando postgresql object carries no backup status; its WAL-G/WAL-E state is in the pod environment and is not read.",
            "configured": None,
            "repos": [],
            "scheduledCount": 0,
            "neverCompleted": 0,
            "last": None,
            "lastFull": None,
            "lastIncr": None,
            "lastDiff": None,
            "manual": None,
            "restore": None,
        }
    spec_repos = (((_spec(obj).get("backups") or {}).get("pgbackrest")) or {}).get("repos") or []
    status = _status(obj).get("pgbackrest") or {}
    repos = {}
    for r in spec_repos[:NESTED_LIMIT]:
        repos[r.get("name")] = {"name": r.get("name"), "schedules": r.get("schedules") or None}
    for r in (status.get("repos") or [])[:NESTED_LIMIT]:
        view = repos.setdefault(r.get("name"), {"name": r.get("name"), "schedules": None})
        view.update(
            {
                "bound": r.get("bound"),
                "stanzaCreated": r.get("stanzaCreated"),
                "replicaCreateBackupComplete": r.get("replicaCreateBackupComplete"),
            }
        )
    for view in repos.values():
        view.setdefault("bound", None)
        view.setdefault("stanzaCreated", None)
        view.setdefault("replicaCreateBackupComplete", None)
    scheduled = status.get("scheduledBackups") or []
    done = [
        (parse_time(b.get("completionTime")), b)
        for b in scheduled
        if parse_time(b.get("completionTime"))
    ]

    def newest(kind_of=None):
        pool = [(t, b) for t, b in done if kind_of is None or b.get("type") == kind_of]
        return _backup_entry(max(pool, key=lambda x: x[0])[1], now) if pool else None

    return {
        "supported": True,
        "note": None,
        "configured": bool(spec_repos),
        "repos": list(repos.values()),
        "scheduledCount": len(scheduled),
        "neverCompleted": len(scheduled) - len(done),
        "last": newest(),
        "lastFull": newest("full"),
        "lastIncr": newest("incr"),
        "lastDiff": newest("diff"),
        "manual": _job_view(status.get("manualBackup")),
        "restore": _job_view(status.get("restore")),
    }


# --------------------------------------------------------- patroni-status ----


def parse_patroni(text):
    """The members `patronictl list -f json` printed. Anything that is not a JSON array of
    objects is an `internal` error (never an empty list)."""
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise RwError("internal", f"patronictl list did not print JSON: {exc}") from None
    if not isinstance(data, list) or not all(isinstance(m, dict) for m in data):
        raise RwError("internal", "patronictl list -f json did not print an array of members")
    return data


def _lag(raw):
    """Lag in MB as a number, None when Patroni printed nothing or 'unknown'."""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return raw
    try:
        return float(str(raw).strip())
    except ValueError:
        return None


def patroni_member(raw, threshold_mb):
    role = str(raw.get("Role") or "")
    state = str(raw.get("State") or "")
    leader = role.lower() in LEADER_ROLES
    lag = _lag(raw.get("Lag in MB"))
    return {
        "name": raw.get("Member"),
        "host": raw.get("Host"),
        "role": role or None,
        "state": state or None,
        "leader": leader,
        "running": state.lower() in UP_STATES,
        "timeline": _int(raw.get("TL")),
        "lagMb": lag,
        # a replica whose lag Patroni could not tell (printed 'unknown' or nothing)
        "lagUnknown": (not leader) and lag is None,
        "lagAboveThreshold": lag is not None and lag > threshold_mb,
        "pendingRestart": bool(raw.get("Pending restart")),
    }


def patroni_view(raw_members, queried, threshold_mb, max_items):
    """The `patroni` output. No leader, a member that is not running and unknown lag are facts here."""
    members = [patroni_member(m, threshold_mb) for m in raw_members]
    leaders = [m for m in members if m["leader"]]
    timelines = sorted({m["timeline"] for m in members if m["timeline"] is not None})
    lags = [m["lagMb"] for m in members if m["lagMb"] is not None]
    items = cap(members, max_items)
    clusters = [str(m.get("Cluster")) for m in raw_members if m.get("Cluster")]
    return {
        "queried": queried,
        "cluster": clusters[0] if clusters else None,
        "memberCount": len(members),
        "leaderPresent": bool(leaders),
        "leader": leaders[0]["name"] if leaders else None,
        "leaderRunning": bool(leaders) and leaders[0]["running"],
        "timelines": timelines,
        "timelineMismatch": len(timelines) > 1,
        "maxLagMb": max(lags) if lags else None,
        "lagThresholdMb": threshold_mb,
        "notRunning": [m["name"] for m in members if not m["running"]][:NESTED_LIMIT],
        "aboveLagThreshold": [m["name"] for m in members if m["lagAboveThreshold"]][:NESTED_LIMIT],
        "truncated": len(members) > len(items),
        "members": items,
    }


def patroni_unavailable(threshold_mb):
    """The `patroni` output when there was no member pod to ask: nothing was read, so every
    fact is null (not 'no leader')."""
    return {
        "queried": None,
        "cluster": None,
        "memberCount": None,
        "leaderPresent": None,
        "leader": None,
        "leaderRunning": None,
        "timelines": [],
        "timelineMismatch": None,
        "maxLagMb": None,
        "lagThresholdMb": threshold_mb,
        "notRunning": [],
        "aboveLagThreshold": [],
        "truncated": False,
        "members": [],
    }


# ------------------------------------------------------------ connections ----

# One document, so the output never depends on psql's column layout. Every count excludes this
# session. No query text is selected.
CONNECTIONS_SQL = """SET statement_timeout = '10s';
SELECT json_build_object(
  'inRecovery', pg_is_in_recovery(),
  'maxConnections', current_setting('max_connections')::int,
  'superuserReserved', current_setting('superuser_reserved_connections')::int,
  'total', (SELECT count(*) FROM pg_stat_activity
            WHERE backend_type = 'client backend' AND pid <> pg_backend_pid()),
  'byState', (SELECT coalesce(json_object_agg(s, c), '{}'::json)
              FROM (SELECT coalesce(state, 'unknown') AS s, count(*) AS c FROM pg_stat_activity
                    WHERE backend_type = 'client backend' AND pid <> pg_backend_pid()
                    GROUP BY 1) t),
  'longestQuerySeconds', (SELECT floor(extract(epoch FROM max(now() - query_start)))::bigint
                          FROM pg_stat_activity
                          WHERE state = 'active' AND backend_type = 'client backend'
                            AND pid <> pg_backend_pid()),
  'longestIdleInTransactionSeconds', (SELECT floor(extract(epoch FROM max(now() - state_change)))::bigint
                                      FROM pg_stat_activity
                                      WHERE state IN ('idle in transaction', 'idle in transaction (aborted)')
                                        AND pid <> pg_backend_pid()),
  'waitingLocks', (SELECT count(*) FROM pg_locks WHERE NOT granted),
  'sessionsWaitingOnLock', (SELECT count(*) FROM pg_stat_activity
                            WHERE wait_event_type = 'Lock' AND pid <> pg_backend_pid())
);"""


def psql_json(text, what):
    """The single JSON document a psql query printed; anything else is an `internal` error."""
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise RwError("internal", f"psql ({what}) did not print JSON: {exc}") from None
    if not isinstance(data, dict):
        raise RwError("internal", f"psql ({what}) did not print a JSON object")
    return data


def connections_view(doc, queried, primary_count, threshold_percent):
    for key in ("maxConnections", "superuserReserved", "total", "byState", "waitingLocks"):
        if doc.get(key) is None:
            raise RwError("internal", f"psql (connections) result has no {key}")
    maximum, reserved, total = doc["maxConnections"], doc["superuserReserved"], doc["total"]
    usable = maximum - reserved
    utilization = round(100.0 * total / usable, 1) if usable > 0 else None
    return {
        "queried": queried,
        "primaryCandidates": primary_count,
        "inRecovery": doc.get("inRecovery"),
        "maxConnections": maximum,
        "superuserReserved": reserved,
        "usableConnections": usable,
        "total": total,
        "byState": dict(sorted(doc["byState"].items())),
        "utilizationPercent": utilization,
        "utilizationThresholdPercent": threshold_percent,
        "aboveThreshold": utilization is not None and utilization >= threshold_percent,
        "longestQuerySeconds": doc.get("longestQuerySeconds"),
        "longestIdleInTransactionSeconds": doc.get("longestIdleInTransactionSeconds"),
        "waitingLocks": doc["waitingLocks"],
        "sessionsWaitingOnLock": doc.get("sessionsWaitingOnLock"),
    }


# --------------------------------------------------------------- settings ----

# Names that never leave the pod (the task statement's pattern; the same regex is in the SQL).
EXCLUDED_NAME = re.compile(r"password|secret|key|ssl_.*file", re.IGNORECASE)
# Names whose value can embed a connection string or a command line with a credential: the row
# is kept (it is a fact that the setting is set) but its value is withheld.
WITHHELD_NAME = re.compile(r"conninfo|_command$|_url$", re.IGNORECASE)
_CREDENTIAL_VALUE = re.compile(
    r"(password|passwd|secret|token|apikey|api_key)\s*=|://[^/@\s:]+:[^/@\s]*@", re.IGNORECASE
)

# Non-default settings of the server. Settings whose source is `client` or `session` belong to
# this psql connection (application_name, the statement_timeout set below), not to the server.
SETTINGS_SQL = """SET statement_timeout = '10s';
SELECT json_build_object(
  'items', (SELECT coalesce(json_agg(json_build_object(
                     'name', name, 'setting', setting, 'unit', unit, 'source', source) ORDER BY name),
                     '[]'::json)
            FROM pg_settings
            WHERE source NOT IN ('default', 'client', 'session') AND name !~* 'password|secret|key|ssl_.*file'),
  'excluded', (SELECT count(*) FROM pg_settings
               WHERE source NOT IN ('default', 'client', 'session') AND name ~* 'password|secret|key|ssl_.*file')
);"""


def settings_view(doc, queried, max_items):
    raw = doc.get("items")
    if not isinstance(raw, list):
        raise RwError("internal", "psql (settings) result has no items")
    items = []
    for s in raw:
        name = str(s.get("name"))
        if EXCLUDED_NAME.search(name):
            continue  # the query already left these out; never rely on that alone
        value = s.get("setting")
        withheld = bool(WITHHELD_NAME.search(name)) or bool(
            value is not None and _CREDENTIAL_VALUE.search(str(value))
        )
        text, truncated = limit_text(None if withheld else value)
        items.append(
            {
                "name": name,
                "setting": text,
                "truncated": truncated,
                "unit": s.get("unit"),
                "source": s.get("source"),
                "redacted": withheld,
            }
        )
    shown = cap(items, max_items)
    return {
        "queried": queried,
        "count": len(items),
        "excludedCount": _int(doc.get("excluded")) or 0,
        "truncated": len(items) > len(shown),
        "items": shown,
    }
