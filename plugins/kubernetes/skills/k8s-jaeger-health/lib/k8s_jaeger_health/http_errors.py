"""http-errors: HTTP error spans per service and operation, from Jaeger's query API."""

from datetime import datetime, timedelta, timezone

from k8s_jaeger_health.common import MESSAGE_LIMIT, NESTED_LIMIT, cap, iso
from k8s_jaeger_health.errors import RwError
from k8s_jaeger_health.fetch import Budget, chosen_pod, fetch_one
from k8s_jaeger_health.traces import (
    STATUS_KEYS,
    Tally,
    service_names,
    trace_list,
    traces_path,
)

EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
MAX_LIMIT = 1500


def micros(stamp):
    return (stamp - EPOCH) // timedelta(microseconds=1)


def check_codes(codes):
    """The status codes to search for: integers 400..599, at least one. Else invalid_input."""
    if not codes or not all(
        isinstance(c, int) and not isinstance(c, bool) and 400 <= c <= 599 for c in codes
    ):
        raise RwError(
            "invalid_input", f"statusCodes must be integers from 400 to 599, got {codes!r}"
        )
    return sorted(set(codes))


def check_limit(limit):
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIMIT:
        raise RwError("invalid_input", f"limit must be an integer 1..{MAX_LIMIT}, got {limit!r}")
    return limit


def check_names(label, names):
    if names is None:
        names = []
    if not isinstance(names, (list, tuple)) or not all(isinstance(n, str) and n for n in names):
        raise RwError("invalid_input", f"{label} must be a list of service names")
    return list(names)


def select(names, include, exclude):
    """(services to query, requested names Jaeger does not know, excluded names that exist)."""
    known = set(names)
    chosen = [n for n in names if (not include or n in include) and n not in exclude]
    unknown = sorted(n for n in include if n not in known)
    return chosen, unknown, sorted(n for n in exclude if n in known)


def one_service(ctx, workload, pod, scheme, port, service, codes, window, limit, budget):
    """The service's row: every (status key, status code) is one query, and a query that fails
    fails the service (counts from some of the queries would read as no errors for the rest)."""
    start_us, end_us = window
    tally = Tally(service, codes)
    reached = False
    for key in STATUS_KEYS:
        for code in codes:
            what = f"{key}={code} traces of service {service}"
            path = traces_path(service, start_us, end_us, limit, key, code)
            traces = trace_list(fetch_one(ctx, workload, pod, scheme, port, path, budget), what)
            reached = reached or len(traces) >= limit
            tally.add(traces, start_us, end_us)
    server, client, rows = tally.totals()
    return {
        "name": service,
        "queried": True,
        "tracesSeen": len(tally.trace_ids),
        "limitReached": reached,
        "serverErrorSpans": server,
        "clientErrorSpans": client,
        "operationCount": len(rows),
        "operationsTruncated": len(rows) > NESTED_LIMIT,
        "operations": rows[:NESTED_LIMIT],
    }


def unqueried(service):
    return {
        "name": service,
        "queried": False,
        "tracesSeen": None,
        "limitReached": None,
        "serverErrorSpans": None,
        "clientErrorSpans": None,
        "operationCount": None,
        "operationsTruncated": False,
        "operations": [],
    }


def view(ctx, workload, pods, scheme, port, since, now, params, max_items, budget=None):
    """The http-errors view. `params` holds statusCodes, limit, services, excludeServices.

    A service whose queries fail is one error scoped `service/<name>` and a row with null counts
    (also a service not asked because the task's deadline passed); if no service could be read
    the first failure is raised. A Jaeger without services is a fact: `services: []`."""
    budget = budget or Budget()
    codes, limit = params["statusCodes"], params["limit"]
    pod = chosen_pod(workload, pods)
    window = (micros(now - since), micros(now))
    names = service_names(
        fetch_one(ctx, workload, pod, scheme, port, "/api/services", budget), "/api/services"
    )
    chosen, unknown, excluded = select(names, params["services"], params["excludeServices"])
    rows, failures = [], []
    for service in cap(chosen, max_items):
        try:
            rows.append(
                one_service(ctx, workload, pod, scheme, port, service, codes, window, limit, budget)
            )
        except RwError as exc:
            if exc.code == "permission_denied":
                raise
            failures.append((service, exc))
            rows.append(unqueried(service))
    if chosen and len(failures) == len(rows):
        name, exc = failures[0]
        raise RwError(exc.code, exc.message, scope=f"service/{name}") from exc
    for name, exc in failures:
        ctx.error(exc.code, exc.message[:MESSAGE_LIMIT], scope=f"service/{name}")
    # queried services first, the ones with the most server errors on top, then the ones not read
    rows.sort(
        key=lambda r: (
            not r["queried"],
            -(r["serverErrorSpans"] or 0),
            -(r["clientErrorSpans"] or 0),
            r["name"],
        )
    )
    return {
        "workload": {"kind": workload["kind"], "name": workload["metadata"]["name"]},
        "viewedFrom": pod["metadata"]["name"],
        "window": {
            "since": int(since.total_seconds()),
            "start": iso(now - since),
            "end": iso(now),
            "startMicros": window[0],
            "endMicros": window[1],
        },
        "limit": limit,
        "statusCodes": codes,
        "statusKeys": list(STATUS_KEYS),
        "serviceCount": len(names),
        "queriedCount": sum(1 for r in rows if r["queried"]),
        "truncated": len(chosen) > max_items,
        "unknownServices": unknown,
        "excludedServices": excluded,
        "serverErrorSpans": sum(r["serverErrorSpans"] or 0 for r in rows),
        "clientErrorSpans": sum(r["clientErrorSpans"] or 0 for r in rows),
        "services": rows,
    }
