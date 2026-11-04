"""ring: the members of each Loki hash ring, from the ring pages. The request asks for JSON
(`Accept: application/json`); a Loki that ignores it and answers HTML is read from its table."""

import json

from k8s_loki_health.common import (
    MESSAGE_LIMIT,
    NESTED_LIMIT,
    age_seconds,
    cap,
    iso,
    parse_time,
    unparseable,
)
from k8s_loki_health.errors import RwError
from k8s_loki_health.fetch import Budget, fetch_one, no_pods, queried
from k8s_loki_health.proxy import status_error
from k8s_loki_health.ringpage import members as html_members

DEFAULT_PATHS = ("/ring", "/distributor/ring", "/compactor/ring")
ASK_JSON = {"Accept": "application/json"}  # dskit's ring page answers JSON when asked


def ring_members(reply, what):
    """(the member list, the server's `now` text or None, the page's format 'json' or 'html') of a
    ring page. The request asks for JSON, which is what a Loki that honours `Accept` answers; the
    HTML table is read only as a fallback for one that ignores it. A page that is neither a
    ring's JSON (`shards`) nor a ring's HTML table, or any status but 200, is an error, never an
    empty ring. A 404 is not an error here: the caller reads it as 'this pod does not serve that
    ring' before calling this."""
    if reply.status != 200:
        raise status_error(reply, what)
    try:
        doc = json.loads(reply.body)
    except ValueError:
        doc = None
    if doc is None:
        parsed = html_members(reply.body)
        if parsed is not None:
            return parsed[0], parsed[1], "html"
    shards = doc.get("shards") if isinstance(doc, dict) else None
    if not isinstance(shards, list) or not all(isinstance(s, dict) for s in shards):
        raise RwError(
            "internal",
            f"{what} did not return a ring page (JSON with shards, or the HTML ring table): "
            f"{reply.body.strip()[:80]!r}",
        )
    return shards, doc.get("now"), "json"


def member_view(shard, now, unparsed):
    """One member. A timestamp that is present and not RFC 3339 reads as null, and (member id,
    field, value) is added to `unparsed` so the caller reports it."""
    for field in ("timestamp", "registered_timestamp"):
        if unparseable(shard.get(field)):
            unparsed.append((shard.get("id"), field, shard.get(field)))
    beat = parse_time(shard.get("timestamp"))
    registered = parse_time(shard.get("registered_timestamp"))
    tokens = shard.get("tokens")
    return {
        "id": shard.get("id"),
        "state": shard.get("state"),
        "address": shard.get("address"),
        "zone": shard.get("zone") or None,
        "tokenCount": len(tokens) if isinstance(tokens, list) else shard.get("token_count"),
        "registeredAt": iso(registered),
        "lastHeartbeat": iso(beat),
        "heartbeatAgeSeconds": age_seconds(beat, now),
        "readOnly": shard.get("read_only") if isinstance(shard.get("read_only"), bool) else None,
    }


def ring_view(path, shards, viewed_from, unserved_by, now, unparsed, fmt):
    members = [member_view(s, now, unparsed) for s in shards]
    by_state = {}
    for member in members:
        by_state[member["state"]] = by_state.get(member["state"], 0) + 1
    members.sort(key=lambda m: (m["state"] == "ACTIVE", str(m["id"])))
    return {
        "path": path,
        "served": True,
        "format": fmt,
        "viewedFrom": viewed_from,
        "unservedBy": unserved_by,
        "memberCount": len(members),
        "byState": by_state,
        "notActiveCount": sum(1 for m in members if m["state"] != "ACTIVE"),
        "members": cap(members, NESTED_LIMIT),
        "membersTruncated": len(members) > NESTED_LIMIT,
    }


def unserved(path, unserved_by):
    return {
        "path": path,
        "served": False,
        "format": None,
        "viewedFrom": None,
        "unservedBy": unserved_by,
        "memberCount": 0,
        "byState": {},
        "notActiveCount": 0,
        "members": [],
        "membersTruncated": False,
    }


def undetermined(path, unserved_by):
    """A ring no pod could be asked about (every attempt failed): served is null."""
    return {**unserved(path, unserved_by), "served": None}


def reference_time(ctx, server_now, now, path, name):
    """The clock heartbeat ages are measured against: the ring page's own `now` (the server's
    clock, which is what the ring judges heartbeats by), else the run's reference time. A `now`
    that does not parse is reported and `now` is used."""
    if server_now is None:
        return now
    stamp = parse_time(server_now)
    if stamp is None:
        ctx.error(
            "internal",
            f"{path} on pod {name}: the ring's now {str(server_now)[:40]!r} is not RFC 3339; "
            "ages use the run's reference time"[:MESSAGE_LIMIT],
            scope=f"pod/{name}",
        )
        return now
    return stamp


def report_unparsed(ctx, unparsed, path, name):
    if unparsed:
        member, field, value = unparsed[0]
        ctx.error(
            "internal",
            f"{path} on pod {name}: {len(unparsed)} member timestamp(s) are not RFC 3339 and are "
            f"null (first: member {str(member)[:80]} {field} {str(value)[:40]!r})"[:MESSAGE_LIMIT],
            scope=f"pod/{name}",
        )


def collect(ctx, workload, pods, scheme, port, paths, now, limit, budget=None):
    """The ring view. Heartbeat ages are measured against the ring page's own `now` when it has
    one. If no asked pod serves any path, `dependency` is reported (is this a component that
    runs a ring?), so the result is partial. A pod not asked because the task's deadline passed
    or three pods in a row timed out on the path is a scoped `timeout`. For each path the pods are asked in name order until one serves it as ring
    JSON; a 404 puts a pod in `unservedBy`; any other failure of a pod is recorded and the next
    pod is tried. Nothing served and nothing failed is a fact (`served: false`); a path every
    asked pod failed on is `served: null`. If every call failed the first failure is raised."""
    budget = budget or Budget()
    asked = queried(pods)
    if not asked:
        raise no_pods(workload)
    rings, failures, answered = [], [], 0
    for path in paths:
        unserved_by, found, failed = [], None, 0
        for pod in asked:
            name = pod["metadata"]["name"]
            try:
                reply = fetch_one(ctx, workload, pod, scheme, port, path, budget, ASK_JSON)
                if reply.status == 404:
                    unserved_by.append(name)
                    answered += 1
                    continue
                shards, server_now, fmt = ring_members(reply, f"{path} on pod {name}")
            except RwError as exc:
                if exc.code == "permission_denied":
                    raise
                failures.append((name, path, exc))
                failed += 1
                continue
            answered += 1
            unparsed = []
            clock = reference_time(ctx, server_now, now, path, name)
            found = ring_view(path, shards, name, unserved_by, clock, unparsed, fmt)
            report_unparsed(ctx, unparsed, path, name)
            break
        if found is not None:
            rings.append(found)
        elif failed:
            rings.append(undetermined(path, unserved_by))
        else:
            rings.append(unserved(path, unserved_by))
    if not answered:
        name, path, exc = failures[0]
        raise RwError(exc.code, exc.message, scope=f"pod/{name}") from exc
    if rings and all(r["served"] is False for r in rings):
        ctx.error(
            "dependency",
            "no ring served; is this a Loki component that runs a ring?",
            scope="rings",
        )
    reported = set()
    for name, path, exc in failures:
        if (name, exc.code) not in reported:  # one error per pod and code, not one per ring path
            reported.add((name, exc.code))
            ctx.error(exc.code, exc.message[:MESSAGE_LIMIT], scope=f"pod/{name}")
    return {
        "workload": {"kind": workload["kind"], "name": workload["metadata"]["name"]},
        "queriedPods": [p["metadata"]["name"] for p in asked],
        "rings": cap(rings, limit),
    }
