"""health: the liveness, readiness and router health answers of each Artifactory pod and Service."""

import json

from k8s_artifactory_health.common import MESSAGE_LIMIT, NESTED_LIMIT, cap, cap_text, truncate
from k8s_artifactory_health.errors import RwError
from k8s_artifactory_health.fetch import (
    PORT_NAMES,
    Budget,
    ask,
    pod_port,
    no_pod_error,
    pod_ready,
    pod_url,
    queried,
    service_port_for,
    service_url,
)

# (name, path, which port serves it). liveness and readiness are Artifactory's; routerLiveness and
# routerReadiness are the router's own, the ones the chart's probes use (anonymous).
ENDPOINTS = (
    ("liveness", "/artifactory/api/v1/system/liveness", "artifactory"),
    ("readiness", "/artifactory/api/v1/system/readiness", "artifactory"),
    ("router", "/router/api/v1/system/health", "router"),
    ("routerLiveness", "/router/api/v1/system/liveness", "router"),
    ("routerReadiness", "/router/api/v1/system/readiness", "router"),
)
# The endpoints that answer OK (bare, or {"code": "OK"}), not a document.
PROBES = ("liveness", "readiness", "routerLiveness", "routerReadiness")
NOT_SERVED = "not served (booting, wrong port, or not Artifactory)"
AUTH_HINT = (
    "the endpoint wants credentials and the skill sends none (only the kubernetes connection is "
    "used); an Artifactory access token needs a generic connection (misc plugin, M4)"
)
HEALTHY = "HEALTHY"
MAX_QUERIED_SERVICES = 10


def parse_router(body, what):
    """The router health document as {state, serviceCount, unhealthyCount, services, ...}. Anything
    that is not `{"router": {"state": ...}, "services": [...]}` JSON (an HTML page, a different
    product) is an `internal` error, never healthy."""
    try:
        doc = json.loads(body)
    except ValueError:
        doc = None
    router = doc.get("router") if isinstance(doc, dict) else None
    services = doc.get("services") if isinstance(doc, dict) else None
    if not (
        isinstance(router, dict)
        and isinstance(router.get("state"), str)
        and isinstance(services, list)
        and all(isinstance(s, dict) and isinstance(s.get("state"), str) for s in services)
    ):
        raise RwError(
            "internal", f"{what} did not return the router health document: {body[:80]!r}"
        )
    rows = []
    for item in cap(services, NESTED_LIMIT):
        message, was_cut = truncate(item.get("message"))
        rows.append(
            {
                "serviceId": cap_text(item.get("service_id")),
                "nodeId": cap_text(item.get("node_id")),
                "state": item["state"],
                "message": message,
                "messageTruncated": was_cut,
            }
        )
    return {
        "state": router["state"],
        "serviceCount": len(services),
        "unhealthyCount": sum(1 for s in services if s["state"] != HEALTHY),
        "services": rows,
        "servicesTruncated": len(services) > NESTED_LIMIT,
    }


def endpoint_row(path, port, reply=None):
    """The row of one endpoint: nothing answered (reply None) or the reply's facts."""
    return {
        "path": path,
        "port": port,
        "httpStatus": reply.status if reply else None,
        "served": None if reply is None else reply.status != 404,
        "ok": None,
        "authRequired": bool(reply and reply.status in (401, 403)),
        "noEndpoints": bool(reply and reply.reason == "no_endpoints"),
        "reason": None,
        "body": None,
        "bodyTruncated": False,
        "router": None,
    }


def probe_body(body, what):
    """The OK text of a liveness or readiness 200: the bare text `OK`, or a JSON document whose
    `code` is "OK". Anything else (an HTML page, another product, a different code) is an
    `internal` error, never ok."""
    text = body.strip()
    if text == "OK":
        return text
    try:
        doc = json.loads(text)
    except ValueError:
        doc = None
    if isinstance(doc, dict) and doc.get("code") == "OK":
        return text
    raise RwError("internal", f"{what} did not answer OK: {text[:80]!r}")


def read_reply(name, path, port, reply, what, noun="pod"):
    """The endpoint row for a reply. 200 with the expected payload is ok true; 503 is ok false;
    a 404 is served false, and on a pod's liveness or readiness (a Running pod that does not serve
    them) it is also ok false with a reason; 401 and 403 are authRequired; a 200 that is not the
    expected payload is an `internal` error."""
    row = endpoint_row(path, port, reply)
    if reply.status == 200:
        if name == "router":
            row["router"] = parse_router(reply.body, what)
            row["ok"] = row["router"]["state"] == HEALTHY and not row["router"]["unhealthyCount"]
        else:
            row["body"], row["bodyTruncated"] = truncate(probe_body(reply.body, what))
            row["ok"] = True
    elif reply.status == 503:
        row["ok"] = False
    elif reply.status == 404 and noun == "pod" and name in PROBES:
        row["ok"], row["reason"] = False, NOT_SERVED
    return row


class Stats:
    """What the asks so far showed: replies, replies that wanted credentials, failures."""

    def __init__(self):
        self.answered = 0
        self.auth = 0
        self.pod_asked = 0  # asks of a pod's endpoints, answered or not
        self.pod_not_served = 0  # of those, the 404s
        self.failures = []  # (scope, RwError)


def probe(ctx, noun, name, ports, url_for, scheme, budget, stats):
    """{endpoint name: row or None} for one pod or Service. `ports` maps "artifactory" and "router"
    to (port to ask on, port to report) or None when the target does not expose it (that endpoint is
    None). An endpoint that fails (unreachable, timeout, a payload that is not the expected one) is
    kept in `stats.failures`, with a row that says nothing answered; an RBAC refusal raises."""
    rows = {}
    for endpoint, path, kind in ENDPOINTS:
        if ports[kind] is None:
            rows[endpoint] = None
            continue
        ask_port, shown = ports[kind]
        what = f"{path} on {noun}/{name}"
        stats.pod_asked += noun == "pod"
        try:
            reply = ask(ctx, url_for(scheme, ask_port, path), budget, path)
            rows[endpoint] = read_reply(endpoint, path, shown, reply, what, noun)
        except RwError as exc:
            if exc.code == "permission_denied":
                raise
            stats.failures.append((f"{noun}/{name}/{endpoint}", exc))
            rows[endpoint] = endpoint_row(path, shown)
            continue
        stats.answered += 1
        stats.pod_not_served += noun == "pod" and reply.status == 404
        if reply.status in (401, 403):
            stats.auth += 1
            ctx.error(
                "permission_denied",
                f"{what} answered HTTP {reply.status}: it needs credentials",
                scope=f"{noun}/{name}/{endpoint}",
                hint=AUTH_HINT,
            )
    return rows


def pod_row(pod, ports, probed, rows=None):
    rows = rows or {name: None for name, _, _ in ENDPOINTS}
    return {
        "name": pod["metadata"]["name"],
        "phase": (pod.get("status") or {}).get("phase"),
        "podReady": pod_ready(pod),
        "queried": probed,
        "artifactoryPort": ports["artifactory"][1],
        "routerPort": ports["router"][1],
        **rows,
    }


def service_row(service, probed, rows=None):
    rows = rows or {name: None for name, _, _ in ENDPOINTS}
    return {
        "name": service["metadata"]["name"],
        "type": (service.get("spec") or {}).get("type"),
        "queried": probed,
        **rows,
    }


def view(
    ctx, workload, pods, services, services_read, scheme, port, router_port, limit, budget=None
):
    """The health view. Pods that are not Running are listed and not asked. The Services that
    select the pods are asked on the Service port that targets the pod's Artifactory and router
    ports; a Service that exposes neither port has no endpoints. A failing endpoint is one error
    scoped to it (`ctx.error`); if nothing answered at all the first failure is raised, and if
    everything that answered wanted credentials the task is skipped."""
    budget, stats = budget or Budget(), Stats()
    asked = {p["metadata"]["name"] for p in queried(pods)}
    pod_rows, port_numbers = [], None
    for pod in cap(pods, limit):
        ports = {
            kind: pod_port(pod, kind, port if kind == "artifactory" else router_port)
            for kind in ("artifactory", "router")
        }
        shown = {kind: (number, number) for kind, (number, _) in ports.items()}
        port_numbers = port_numbers or ports
        if pod["metadata"]["name"] not in asked:
            pod_rows.append(pod_row(pod, shown, False))
            continue
        rows = probe(
            ctx, "pod", pod["metadata"]["name"], shown,
            lambda s, p_, path, pod=pod: pod_url(workload, pod, s, p_, path), scheme, budget, stats,
        )  # fmt: skip
        pod_rows.append(pod_row(pod, shown, True, rows))
    if port_numbers is None:  # no pod to name the ports: the defaults, and the chart's names
        port_numbers = {
            kind: pod_port({}, kind, port if kind == "artifactory" else router_port)
            for kind in ("artifactory", "router")
        }
    service_rows = []
    for index, service in enumerate(cap(services, limit)):
        ports = {}
        for kind in ("artifactory", "router"):
            number, name = port_numbers[kind]
            names = (name,) if name else ()
            if name is None and (port if kind == "artifactory" else router_port) is None:
                names = PORT_NAMES[kind]  # the pod does not name it: the chart's names
            ports[kind] = service_port_for(service, number, names)
        if index >= MAX_QUERIED_SERVICES:
            service_rows.append(service_row(service, False))
            continue
        try:
            rows = probe(
                ctx, "service", service["metadata"]["name"], ports,
                lambda s, p_, path, service=service: service_url(workload, service, s, p_, path),
                scheme, budget, stats,
            )  # fmt: skip
        except RwError:  # RBAC refusal of services/proxy: the pod facts stand without it
            ctx.error(
                "permission_denied",
                "not allowed to get services/proxy: the Services were not asked",
                scope="services/proxy",
            )
            service_rows += [service_row(s, False) for s in cap(services, limit)[index:]]
            break
        service_rows.append(service_row(service, True, rows))
    if asked and not stats.answered:
        scope, exc = stats.failures[0]
        raise RwError(exc.code, exc.message, scope=scope) from exc
    if stats.pod_asked and stats.pod_not_served == stats.pod_asked:
        raise RwError("not_found", "no Artifactory endpoint answered; check the port input")
    if stats.answered and stats.auth == stats.answered:
        ctx.skip(
            "every endpoint asked answered 401 or 403: Artifactory requires credentials "
            "here. " + AUTH_HINT
        )
    for scope, exc in stats.failures:
        ctx.error(exc.code, exc.message[:MESSAGE_LIMIT], scope=scope)
    no_pod_error(ctx, workload, asked)
    spec, status = workload.get("spec") or {}, workload.get("status") or {}
    rows = pod_rows + service_rows
    return {
        "workload": {
            "kind": workload["kind"],
            "name": workload["metadata"]["name"],
            "replicas": spec.get("replicas"),
            "readyReplicas": status.get("readyReplicas"),
        },
        "podCount": len(pods),
        "serviceCount": len(services),
        "servicesRead": services_read,
        "truncated": len(pods) > limit or len(services) > limit,
        "notOkCount": sum(
            1 for r in rows for n, _, _ in ENDPOINTS if r[n] is not None and r[n]["ok"] is False
        ),
        "pods": pod_rows,
        "services": service_rows,
    }
