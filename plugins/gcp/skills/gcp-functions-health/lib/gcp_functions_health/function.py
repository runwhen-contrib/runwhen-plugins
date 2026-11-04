"""function-state: what Cloud Functions reports about one function, as typed facts.

The row is in its generation's own format (fnlist.py). Both are read into the same output;
what only one generation has is null on the other. Environment variables and secret references
are only counted: no name, value or version is returned, and neither is the source location."""

import re

from . import fnlist
from .logtext import clip

MAX_NESTED = 50
_SECONDS = re.compile(r"(\d+(?:\.\d+)?)s")
SERVICE = re.compile(r"[a-z][a-z0-9-]{0,62}")  # a Cloud Run service name


def _str(value):
    return value if isinstance(value, str) and value else None


def _int(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _count(value):
    return len(value) if isinstance(value, (dict, list)) else 0


def _section(row, key):
    value = row.get(key)
    return value if isinstance(value, dict) else {}


def _seconds(value):
    """Seconds from `60s` (1st gen `timeout`) or an integer (2nd gen `timeoutSeconds`)."""
    if isinstance(value, str):
        match = _SECONDS.fullmatch(value)
        return int(float(match.group(1))) if match else None
    return _int(value)


def state_messages(row):
    """(messages cut to 50, how many the function has). 2nd gen only; 1st gen has none."""
    raw = row.get("stateMessages")
    raw = [m for m in raw if isinstance(m, dict)] if isinstance(raw, list) else []
    out = []
    for item in raw[:MAX_NESTED]:
        message, cut = clip(item.get("message") or "")
        out.append(
            {
                "severity": _str(item.get("severity")),
                "type": _str(item.get("type")),
                "message": message,
                "messageTruncated": cut,
            }
        )
    return out, len(raw)


def trigger(row, generation):
    """{kind: http | event | None, eventType, url}. A 2nd gen function without an event trigger
    is called over HTTP (its Cloud Run service); a 1st gen one says which it has."""
    event = _section(row, "eventTrigger")
    if generation == 2:
        service = _section(row, "serviceConfig")
        if event:
            return {"kind": "event", "eventType": _str(event.get("eventType")), "url": None}
        return {"kind": "http", "eventType": None, "url": _str(service.get("uri"))}
    https = _section(row, "httpsTrigger")
    if https:
        return {"kind": "http", "eventType": None, "url": _str(https.get("url"))}
    if event:
        return {"kind": "event", "eventType": _str(event.get("eventType")), "url": None}
    return {"kind": None, "eventType": None, "url": None}


def service_name(row):
    """The Cloud Run service behind a 2nd gen function (`serviceConfig.service` ends in
    `/services/<name>`), or None."""
    name = _section(row, "serviceConfig").get("service")
    if isinstance(name, str) and "/services/" in name:
        service = name.rsplit("/services/", 1)[1]
        return service if SERVICE.fullmatch(service) else None
    return None


def labels(row):
    raw = row.get("labels")
    if not isinstance(raw, dict):
        return {}
    kept = {k: v for k, v in raw.items() if isinstance(k, str) and isinstance(v, str)}
    return dict(sorted(kept.items())[:MAX_NESTED])


def timeout_seconds(row):
    """The function's configured timeout in seconds, or None."""
    if fnlist.generation(row) == 2:
        return _seconds(_section(row, "serviceConfig").get("timeoutSeconds"))
    return _seconds(row.get("timeout"))


def function_facts(row, build, deployment):
    """The `function` output. `build` is the build's facts or None; `deployment` is
    {"buildId", "buildLocation"} or None when the function names no build."""
    project, region, name = fnlist.parse_name(row)
    generation = fnlist.generation(row)
    messages, message_count = state_messages(row)
    second = generation == 2
    service = _section(row, "serviceConfig") if second else row
    build_config = _section(row, "buildConfig") if second else row
    memory = service.get("availableMemory") if second else service.get("availableMemoryMb")
    return {
        "name": name,
        "region": region,
        "generation": generation,
        "state": fnlist.state(row),
        "stateMessages": messages,
        "stateMessageCount": message_count,
        "runtime": _str(build_config.get("runtime")),
        "entryPoint": _str(build_config.get("entryPoint")),
        "trigger": trigger(row, generation),
        "updateTime": _str(row.get("updateTime")),
        "versionId": _str(row.get("versionId")),
        "revision": _str(service.get("revision")) if second else None,
        "runService": service_name(row) if second else None,
        "timeoutSeconds": _seconds(service.get("timeoutSeconds" if second else "timeout")),
        "memory": memory
        if second and isinstance(memory, str)
        else (f"{memory}M" if not second and _int(memory) is not None else None),
        "cpu": _str(service.get("availableCpu")) if second else None,
        "minInstances": _int(service.get("minInstanceCount" if second else "minInstances")),
        "maxInstances": _int(service.get("maxInstanceCount" if second else "maxInstances")),
        "maxRequestConcurrency": _int(service.get("maxInstanceRequestConcurrency"))
        if second
        else None,
        "ingress": _str(service.get("ingressSettings")),
        "vpcConnector": _str(service.get("vpcConnector")),
        "serviceAccount": _str(service.get("serviceAccountEmail")),
        "environmentVariableCount": _count(service.get("environmentVariables")),
        "secretReferenceCount": _count(service.get("secretEnvironmentVariables"))
        + _count(service.get("secretVolumes")),
        "labels": labels(row),
        "deployment": deployment,
        "build": build,
    }
