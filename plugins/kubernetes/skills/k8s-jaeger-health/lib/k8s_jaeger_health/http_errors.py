"""http-errors: HTTP error spans per service and operation, from Jaeger's query API."""

from datetime import datetime, timedelta, timezone

from k8s_jaeger_health.common import NESTED_LIMIT, cap, iso
from k8s_jaeger_health.errors import RwError
from k8s_jaeger_health.fetch import Budget, chosen_pod, fetch_one, query_pod, storage_basis
from k8s_jaeger_health.traces import (
    ERROR_TAG,
    STATUS_KEYS,
    Tally,
    service_names,
    trace_list,
    traces_path,
)

EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
MAX_LIMIT = 500


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


class ServiceRun:
    """What the queries for one service have found so far: the tally, the queries finished, and
    the first failure (which stops that service's remaining queries, never the others')."""

    def __init__(self, service, planned):
        self.service = service
        self.tally = Tally(service)
        self.planned = planned
        self.done = 0
        self.reached = False
        self.first_pass_done = False
        self.failure = None

    @property
    def complete(self):
        return self.failure is None and self.done == self.planned


def ask(ctx, workload, pod, scheme, port, run, window, limit, key, value, budget):
    """One tag query for a service: its traces go into the run's tally. A failure other than
    `permission_denied` (which fails the task) is kept on the run and stops its queries; returns
    whether the query finished."""
    start_us, end_us = window
    what = f"{key}={value} traces of service {run.service}"
    path = traces_path(run.service, start_us, end_us, limit, key, value)
    try:
        found = trace_list(fetch_one(ctx, workload, pod, scheme, port, path, budget), what)
    except RwError as exc:
        if exc.code == "permission_denied":
            raise
        run.failure = exc
        return False
    run.done += 1
    run.reached = run.reached or len(found) >= limit
    run.tally.add(found, start_us, end_us)
    return True


def queried_row(run):
    server, client, outbound, rows = run.tally.totals()
    return {
        "name": run.service,
        "queried": True,
        "complete": run.complete,
        "queriesDone": run.done,
        "queriesPlanned": run.planned,
        "tracesSeen": len(run.tally.trace_ids),
        "limitReached": run.reached,
        "serverErrorSpans": server,
        "clientErrorSpans": client,
        "outboundErrorSpans": outbound,
        "operationCount": len(rows),
        "operationsTruncated": len(rows) > NESTED_LIMIT,
        "operations": rows[:NESTED_LIMIT],
    }


def unqueried(run):
    return {
        "name": run.service,
        "queried": False,
        "complete": False,
        "queriesDone": 0,
        "queriesPlanned": run.planned,
        "tracesSeen": None,
        "limitReached": None,
        "serverErrorSpans": None,
        "clientErrorSpans": None,
        "outboundErrorSpans": None,
        "operationCount": None,
        "operationsTruncated": False,
        "operations": [],
    }


def view(ctx, workload, pods, scheme, port, since, now, params, max_items, budget=None):
    """The http-errors view. `params` holds statusCodes, limit, services, excludeServices.

    Two passes over the services (up to `max_items` of them), so that the time budget is spent
    on coverage first. Pass 1 asks every service for its traces with `error=true` (OpenTelemetry
    marks a 5xx server span as an error whatever the code is). Pass 2 asks, service by service,
    for the traces with each status code in `statusCodes` (a 4xx is not an error to OpenTelemetry)
    on both status attributes. A service that runs out of budget or fails part-way keeps the
    queries it finished and is `complete: false`; one whose pass 1 failed is `queried: false`
    with null counts. Each failure is one error scoped `service/<name>`; if no service could be
    read the first failure is raised. A Jaeger without services is a fact: `services: []`."""
    budget = budget or Budget()
    codes, limit = params["statusCodes"], params["limit"]
    pod = chosen_pod(workload, pods)
    window = (micros(now - since), micros(now))
    names = service_names(
        fetch_one(ctx, workload, pod, scheme, port, "/api/services", budget), "/api/services"
    )
    chosen, unknown, excluded = select(names, params["services"], params["excludeServices"])
    planned = 1 + len(codes) * len(STATUS_KEYS)
    runs = [ServiceRun(name, planned) for name in cap(chosen, max_items)]
    for run in runs:  # pass 1: error=true
        ask(ctx, workload, pod, scheme, port, run, window, limit, *ERROR_TAG, budget)
        run.first_pass_done = run.done == 1
    for run in runs:  # pass 2: the exact status codes
        if not run.first_pass_done:
            continue
        for key in STATUS_KEYS:
            for code in codes:
                if run.failure is None:
                    ask(ctx, workload, pod, scheme, port, run, window, limit, key, code, budget)
    rows = [queried_row(r) if r.first_pass_done else unqueried(r) for r in runs]
    failed = [r for r in runs if r.failure is not None]
    if runs and not any(r.first_pass_done for r in runs):
        first = failed[0]
        raise RwError(first.failure.code, first.failure.message, scope=f"service/{first.service}")
    for run in failed:
        ctx.error(run.failure.code, run.failure.message, scope=f"service/{run.service}")
    # queried services first, the ones with the most server errors on top, then the ones not read
    rows.sort(
        key=lambda r: (
            not r["queried"],
            -(r["serverErrorSpans"] or 0),
            -(r["clientErrorSpans"] or 0),
            -(r["outboundErrorSpans"] or 0),
            r["name"],
        )
    )
    queried = sum(1 for r in rows if r["queried"])
    replicas = (workload.get("spec") or {}).get("replicas")
    sharded, basis = storage_basis(workload, pods, pod)
    return {
        "workload": {
            "kind": workload["kind"],
            "name": workload["metadata"]["name"],
            "replicas": replicas,
        },
        "viewedFrom": pod["metadata"]["name"],
        "queryPod": query_pod(pod),
        "storageMayBeSharded": sharded,
        "storageBasis": basis,
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
        "queriedCount": queried,
        "completeCount": sum(1 for r in rows if r["complete"]),
        "servicesNotQueried": len(chosen) - queried,
        "truncated": len(chosen) > max_items,
        "limitReached": any(r["limitReached"] for r in rows),
        "unknownServices": unknown,
        "excludedServices": excluded,
        "serverErrorSpans": sum(r["serverErrorSpans"] or 0 for r in rows),
        "clientErrorSpans": sum(r["clientErrorSpans"] or 0 for r in rows),
        "outboundErrorSpans": sum(r["outboundErrorSpans"] or 0 for r in rows),
        "services": rows,
    }
