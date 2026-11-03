"""Pure functions on redis-cli output: INFO parsing, the facts a task reports, reply checks."""

import re
from datetime import UTC, datetime

from k8s_redis_health.errors import RwError
from k8s_redis_health.workload import NESTED_MAX, limit_text

# Runs inside the redis container. The container's own REDIS_PASSWORD (set from a Secret by its
# manifest) becomes REDISCLI_AUTH, so the password is never read by this skill, never on argv and
# never printed. It is exported only when set: an empty REDISCLI_AUTH makes redis-cli send AUTH ""
# to a server without a password, which prints an "AUTH failed" warning.
EXEC_SCRIPT = (
    'if [ -n "${REDIS_PASSWORD:-}" ]; then REDISCLI_AUTH="$REDIS_PASSWORD"; export REDISCLI_AUTH; fi;'
    ' exec redis-cli "$@"'
)

# redis-cli exits 0 on an error reply and prints it; a line that starts like one of these means
# "the credentials did not work". Only line starts count: INFO's errorstats section lists
# errorstat_NOAUTH and errorstat_WRONGPASS counters of earlier failures.
_AUTH_MARKERS = ("NOAUTH", "WRONGPASS", "AUTH failed", "NOPERM", "ERR invalid password")

AUTH_HINT = (
    "Redis requires a password and the container has no working REDIS_PASSWORD environment "
    "variable; set it on the container (from a Secret) or run the check another way"
)


def auth_failure(text):
    """True when redis-cli output says authentication is required or was refused."""
    return any(
        line.strip().startswith(_AUTH_MARKERS)
        for line in (text or "").replace("\r", "").splitlines()
    )


def reply_text(stdout):
    """redis-cli's reply with carriage returns and surrounding whitespace removed."""
    return (stdout or "").replace("\r", "").strip()


def parse_info(text):
    """INFO output -> {field: value}. Section headers and blank lines are skipped."""
    fields = {}
    for line in reply_text(text).splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, _, value = line.partition(":")
        fields[key] = value
    return fields


def check_info(fields, required="redis_version"):
    """The parsed INFO must carry a field of the section asked for; an error reply or foreign output
    does not."""
    if required not in fields:
        raise RwError("internal", f"redis-cli INFO returned no {required} field")
    return fields


def _int(fields, key):
    value = fields.get(key)
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float(fields, key):
    value = fields.get(key)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _flag(fields, key):
    value = _int(fields, key)
    return None if value is None else value != 0


def _epoch(value):
    if value is None or value <= 0:
        return None
    return datetime.fromtimestamp(value, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _since(value):
    """-1 is Redis' "not applicable" for a seconds counter."""
    return None if value is None or value < 0 else value


def _replicas(fields):
    out = []
    for key in sorted(fields, key=lambda k: (len(k), k)):
        if not re.fullmatch(r"slave\d+", key):
            continue
        parts = dict(p.split("=", 1) for p in fields[key].split(",") if "=" in p)
        out.append(
            {
                "ip": parts.get("ip"),
                "port": int(parts["port"]) if parts.get("port", "").isdigit() else None,
                "state": parts.get("state"),
                "offset": int(parts["offset"]) if parts.get("offset", "").isdigit() else None,
                "lagSeconds": int(parts["lag"]) if parts.get("lag", "").isdigit() else None,
            }
        )
    return out


def instance_facts(fields, memory_threshold_percent):
    """The facts of one Redis instance from its parsed INFO. A field the server version does not
    report is null, never 0."""
    used = _int(fields, "used_memory")
    maxmemory = _int(fields, "maxmemory")
    percent = None
    if used is not None and maxmemory:
        percent = round(used * 100 / maxmemory, 1)
    replicas = _replicas(fields)
    port = _int(fields, "master_port")
    return {
        "server": {
            "redisVersion": fields.get("redis_version"),
            "redisMode": fields.get("redis_mode"),
            "uptimeSeconds": _int(fields, "uptime_in_seconds"),
            "tcpPort": _int(fields, "tcp_port"),
        },
        "replication": {
            "role": fields.get("role"),
            "connectedReplicas": _int(fields, "connected_slaves"),
            "replicas": replicas[:NESTED_MAX],
            "replicasTruncated": len(replicas) > NESTED_MAX,
            "masterHost": fields.get("master_host"),
            "masterPort": port,
            "masterLinkStatus": fields.get("master_link_status"),
            "masterLinkDownSinceSeconds": _since(_int(fields, "master_link_down_since_seconds")),
            "masterLastIoSecondsAgo": _since(_int(fields, "master_last_io_seconds_ago")),
            "masterSyncInProgress": _flag(fields, "master_sync_in_progress"),
            "masterReplOffset": _int(fields, "master_repl_offset"),
        },
        "memory": {
            "usedBytes": used,
            "maxBytes": maxmemory,
            "usedPercent": percent,
            "overThreshold": None if percent is None else percent >= memory_threshold_percent,
            "maxmemoryPolicy": fields.get("maxmemory_policy"),
        },
        "clients": {
            "connected": _int(fields, "connected_clients"),
            "blocked": _int(fields, "blocked_clients"),
        },
        "stats": {
            "evictedKeys": _int(fields, "evicted_keys"),
            "rejectedConnections": _int(fields, "rejected_connections"),
        },
        "persistence": {
            "loading": _flag(fields, "loading"),
            "rdb": {
                "lastBgsaveStatus": fields.get("rdb_last_bgsave_status"),
                "lastSaveTime": _epoch(_int(fields, "rdb_last_save_time")),
                "changesSinceLastSave": _int(fields, "rdb_changes_since_last_save"),
                "bgsaveInProgress": _flag(fields, "rdb_bgsave_in_progress"),
            },
            "aof": {
                "enabled": _flag(fields, "aof_enabled"),
                "lastRewriteStatus": fields.get("aof_last_bgrewrite_status"),
                "lastWriteStatus": fields.get("aof_last_write_status"),
            },
        },
    }


def role_of(fields):
    """'master' or 'slave' from parsed INFO replication; None when INFO has no role."""
    return fields.get("role")


def reply_view(text):
    """(text, truncated) of a reply, bounded for output."""
    return limit_text(reply_text(text), 256)


def counts(instances):
    return {
        "pods": len(instances),
        "ready": sum(1 for i in instances if i["ready"]),
        "queried": sum(1 for i in instances if i["queried"]),
        "failed": sum(1 for i in instances if i["error"] is not None),
    }
