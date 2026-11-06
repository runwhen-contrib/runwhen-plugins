"""Pure functions on redis-cli output: INFO parsing, the facts a task reports, reply checks."""

import re
from datetime import UTC, datetime

from k8s_redis_health.errors import RwError
from k8s_redis_health.workload import NESTED_MAX, limit_text

# Runs inside the redis container. The container's own password becomes REDISCLI_AUTH, so it is
# never read by this skill, never on argv and never printed. Sources, first one set wins:
# REDIS_PASSWORD, the contents of the file REDIS_PASSWORD_FILE names, VALKEY_PASSWORD, the contents
# of the file VALKEY_PASSWORD_FILE names. REDISCLI_AUTH is exported only when a password was found:
# an empty one makes redis-cli send AUTH "" to a server without a password, which prints an
# "AUTH failed" warning. A Valkey image may carry valkey-cli alone.
EXEC_SCRIPT = (
    'pw="${REDIS_PASSWORD:-}";'
    ' if [ -z "$pw" ] && [ -n "${REDIS_PASSWORD_FILE:-}" ] && [ -r "$REDIS_PASSWORD_FILE" ];'
    ' then pw=$(cat "$REDIS_PASSWORD_FILE"); fi;'
    ' if [ -z "$pw" ]; then pw="${VALKEY_PASSWORD:-}"; fi;'
    ' if [ -z "$pw" ] && [ -n "${VALKEY_PASSWORD_FILE:-}" ] && [ -r "$VALKEY_PASSWORD_FILE" ];'
    ' then pw=$(cat "$VALKEY_PASSWORD_FILE"); fi;'
    ' if [ -n "$pw" ]; then REDISCLI_AUTH="$pw"; export REDISCLI_AUTH; fi;'
    ' if command -v redis-cli >/dev/null; then exec redis-cli "$@"; fi;'
    ' exec valkey-cli "$@"'
)

DEFAULT_PORT = 6379
REPLY_LINE_LIMIT = 256  # longest Redis reply line carried in a message

# redis-cli exits 0 on an error reply and prints it; a line that starts like one of these means
# "the credentials did not work". Only line starts count: INFO's errorstats section lists
# errorstat_NOAUTH and errorstat_WRONGPASS counters of earlier failures.
_AUTH_MARKERS = ("NOAUTH", "WRONGPASS", "AUTH failed", "NOPERM", "ERR invalid password")

AUTH_REFUSED_HINT = "the supplied password was refused, or none is configured on the server"
NOAUTH_HINT = (
    "Redis requires a password and the container supplies none: set REDIS_PASSWORD, "
    "REDIS_PASSWORD_FILE, VALKEY_PASSWORD or VALKEY_PASSWORD_FILE on the container"
)
NOPERM_HINT = "the Redis user the container authenticates as may not run this command (ACL)"

# the Redis error replies INFO can answer, and what they mean for the task
_INFO_ERROR_CODES = {
    "LOADING": "unreachable",
    "BUSY": "unreachable",
    "MASTERDOWN": "unreachable",
    "NOPERM": "permission_denied",
    "NOAUTH": "permission_denied",
}

MODES_REFUSED = ("cluster", "sentinel")


def check_port(port):
    """The port as an int, or invalid_input (before any kubectl call)."""
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise RwError("invalid_input", f"port must be 1 to 65535, not {port!r}")
    return port


def auth_line(text):
    """The first line of redis-cli output that says authentication is required or was refused, cut
    to REPLY_LINE_LIMIT; None when there is none."""
    for line in (text or "").replace("\r", "").splitlines():
        line = line.strip()
        if line.startswith(_AUTH_MARKERS):
            return line[:REPLY_LINE_LIMIT]
    return None


def auth_failure(text):
    """True when redis-cli output says authentication is required or was refused."""
    return auth_line(text) is not None


def auth_hint(line):
    """The hint that goes with an authentication failure line."""
    if line.startswith("NOAUTH"):
        return NOAUTH_HINT
    if line.startswith("NOPERM"):
        return NOPERM_HINT
    return AUTH_REFUSED_HINT


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


def info_error(reply):
    """The RwError for an INFO reply that is a Redis error reply (LOADING, BUSY, MASTERDOWN,
    NOPERM, NOAUTH, any other), carrying the first reply line; None when it is INFO output."""
    if parse_info(reply):
        return None
    line = reply_text(reply).split("\n", 1)[0][:REPLY_LINE_LIMIT]
    token = line.split(" ", 1)[0]
    code = _INFO_ERROR_CODES.get(token, "internal")
    if not line:
        return RwError("internal", "Redis answered INFO with no reply")
    hint = auth_hint(line) if code == "permission_denied" else None
    return RwError(code, f"Redis answered INFO with an error: {line}", hint=hint)


def require_server(fields):
    """The INFO fields of a Redis server (standalone, replica, primary or cluster node). Sentinel
    and anything else that answers INFO without a `role` is not one: invalid_input."""
    if "role" not in fields:
        raise RwError(
            "invalid_input",
            "not a Redis server (sentinel?): INFO has no role field; "
            "point the container and port inputs at a Redis data node",
        )
    return fields


def refuse_unsupported_mode(fields):
    """invalid_input for a cluster or sentinel: the write check does not apply there."""
    mode = fields.get("redis_mode")
    if mode in MODES_REFUSED:
        raise RwError(
            "invalid_input",
            f"unsupported_mode: this Redis runs in {mode} mode; the write check supports "
            "standalone and replication only",
            details={"reason": "unsupported_mode", "mode": mode},
        )


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


def _text(fields, key):
    """A string the server reports, bounded (it is server-supplied free text)."""
    return limit_text(fields.get(key))[0]


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
                "ip": limit_text(parts.get("ip"))[0],
                "port": int(parts["port"]) if parts.get("port", "").isdigit() else None,
                "state": limit_text(parts.get("state"))[0],
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
            "redisVersion": _text(fields, "redis_version"),
            "redisMode": _text(fields, "redis_mode"),
            "uptimeSeconds": _int(fields, "uptime_in_seconds"),
            "tcpPort": _int(fields, "tcp_port"),
        },
        "replication": {
            "role": _text(fields, "role"),
            "connectedReplicas": _int(fields, "connected_slaves"),
            "replicas": replicas[:NESTED_MAX],
            "replicasTruncated": len(replicas) > NESTED_MAX,
            "masterHost": _text(fields, "master_host"),
            "masterPort": port,
            "masterLinkStatus": _text(fields, "master_link_status"),
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
            "maxmemoryPolicy": _text(fields, "maxmemory_policy"),
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
                "lastBgsaveStatus": _text(fields, "rdb_last_bgsave_status"),
                "lastSaveTime": _epoch(_int(fields, "rdb_last_save_time")),
                "changesSinceLastSave": _int(fields, "rdb_changes_since_last_save"),
                "bgsaveInProgress": _flag(fields, "rdb_bgsave_in_progress"),
            },
            "aof": {
                "enabled": _flag(fields, "aof_enabled"),
                "lastRewriteStatus": _text(fields, "aof_last_bgrewrite_status"),
                "lastWriteStatus": _text(fields, "aof_last_write_status"),
            },
        },
    }


def role_of(fields):
    """'master' or 'slave' from parsed INFO replication; None when INFO has no role."""
    return _text(fields, "role")


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
