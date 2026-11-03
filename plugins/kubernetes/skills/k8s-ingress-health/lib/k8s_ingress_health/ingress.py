"""Pure functions behind the k8s-ingress-health tasks.

Everything here takes already-parsed Kubernetes JSON and returns plain facts:
no kubectl, no clock, no environment. Scripts fetch, call these, and return.
"""

import json
import re
from datetime import datetime, timedelta, timezone

from k8s_ingress_health.errors import RwError

_DURATION = re.compile(r"(\d+)\s*(ms|[smhdw])")
_UNIT_MS = {"ms": 1, "s": 1000, "m": 60_000, "h": 3_600_000, "d": 86_400_000, "w": 604_800_000}

NESTED_LIMIT = 50  # most entries in a list nested inside an output entry
TEXT_LIMIT = 1024  # most characters in a free-text field

GKE_BACKENDS_ANNOTATION = "ingress.kubernetes.io/backends"
CLASS_ANNOTATION = "kubernetes.io/ingress.class"
DEFAULT_CLASS_ANNOTATION = "ingressclass.kubernetes.io/is-default-class"

# ---------------------------------------------------------------- time ----


def parse_duration(text):
    """'500ms', '90s', '30m', '1h', '1h30m', '2d' -> timedelta. Anything else raises RwError(invalid_input)."""
    if isinstance(text, timedelta):
        return text
    s = str(text).strip().lower()
    parts = _DURATION.findall(s)
    if not parts or "".join(f"{n}{u}" for n, u in parts) != s.replace(" ", ""):
        raise RwError("invalid_input", f"not a duration: {text!r}")
    return timedelta(milliseconds=sum(int(n) * _UNIT_MS[u] for n, u in parts))


def parse_time(value):
    """RFC 3339 string (as the Kubernetes API writes it) -> aware datetime, or None."""
    if not value:
        return None
    s = str(value)
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def fmt_time(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


def cap(items, max_items):
    """Bound a list output to max_items entries."""
    return list(items)[: int(max_items)]


def nested(items):
    """Bound a list nested inside an output entry to NESTED_LIMIT entries."""
    return list(items)[:NESTED_LIMIT]


def trunc(text):
    """(text cut to TEXT_LIMIT characters, whether it was cut). None stays None, not cut."""
    if text is None:
        return None, False
    text = str(text)
    return (text[:TEXT_LIMIT], True) if len(text) > TEXT_LIMIT else (text, False)


# ------------------------------------------------------------ backends ----


def backend_refs(ingress):
    """Every backend the Ingress routes to: the default backend, then each rule path in order.

    Each ref says where it came from (`source`, `host`, `path`, `pathType`) and what it
    points at: a Service (`kind: service`, name and port by number or by name) or a
    typed resource (`kind: resource`, which the controller resolves, not Kubernetes).
    """
    spec = ingress.get("spec") or {}
    refs = []
    if spec.get("defaultBackend"):
        refs.append(_ref("default", None, None, None, spec["defaultBackend"]))
    for rule in spec.get("rules") or []:
        for path in (rule.get("http") or {}).get("paths") or []:
            refs.append(
                _ref(
                    "rule",
                    rule.get("host"),
                    path.get("path"),
                    path.get("pathType"),
                    path.get("backend") or {},
                )
            )
    return refs


def _ref(source, host, path, path_type, backend):
    ref = {
        "source": source,
        "host": host,
        "path": path,
        "pathType": path_type,
        "kind": "service" if backend.get("service") else "resource",
        "service": None,
        "resource": None,
    }
    svc = backend.get("service")
    if svc:
        port = svc.get("port") or {}
        ref["service"] = {
            "name": svc.get("name"),
            "portNumber": port.get("number"),
            "portName": port.get("name"),
        }
    else:
        res = backend.get("resource") or {}
        ref["resource"] = {
            "apiGroup": res.get("apiGroup"),
            "kind": res.get("kind"),
            "name": res.get("name"),
        }
    return ref


def service_names(refs):
    """Distinct backend Service names, in the order the Ingress lists them."""
    seen = []
    for ref in refs:
        name = (ref["service"] or {}).get("name")
        if name and name not in seen:
            seen.append(name)
    return seen


def match_port(service, port_number, port_name):
    """The Service port a backend port refers to (by number or by name), else None."""
    for sp in (service.get("spec") or {}).get("ports") or []:
        if port_number is not None and sp.get("port") == port_number:
            return sp
        if port_name and sp.get("name") == port_name:
            return sp
    return None


def endpoint_counts(slices):
    """Ready and not-ready endpoints across a Service's EndpointSlices.

    An endpoint whose `ready` condition is unset counts as ready (the API's
    rule). A pod listed by dual-stack slices is counted once. `slices` is the
    number of EndpointSlices that were read.
    """
    seen = {}
    for sl in slices:
        for ep in sl.get("endpoints") or []:
            ref = ep.get("targetRef") or {}
            ident = (
                (ref.get("kind"), ref.get("namespace"), ref["name"])
                if ref.get("name")
                else tuple(ep.get("addresses") or ())
            )
            if ident not in seen:
                seen[ident] = (ep.get("conditions") or {}).get("ready") is not False
    ready = sum(1 for r in seen.values() if r)
    return {"ready": ready, "notReady": len(seen) - ready, "slices": len(slices)}


def backend_entries(ingress, services, endpoints):
    """One entry per backend ref with what the cluster says about its Service.

    `services` maps a Service name to its object, or to None when it does not
    exist; a name missing from the map was not looked up (forbidden), so every
    fact derived from the Service is None, not False. `endpoints` maps a name to
    endpoint_counts() or is missing when the slices could not be read.
    """
    out = []
    for ref in backend_refs(ingress):
        svc_ref = ref["service"]
        entry = dict(ref)
        entry.update(
            serviceExists=None,
            serviceType=None,
            portExists=None,
            targetPort=None,
            protocol=None,
            endpoints=None,
        )
        if svc_ref and svc_ref["name"] in services:
            svc = services[svc_ref["name"]]
            entry["serviceExists"] = svc is not None
            if svc is not None:
                entry["serviceType"] = (svc.get("spec") or {}).get("type") or "ClusterIP"
                sp = match_port(svc, svc_ref["portNumber"], svc_ref["portName"])
                entry["portExists"] = sp is not None
                if sp is not None:
                    entry["targetPort"] = sp.get("targetPort", sp.get("port"))
                    entry["protocol"] = sp.get("protocol")
                entry["endpoints"] = endpoints.get(svc_ref["name"])
        out.append(entry)
    return out


def tls_view(ingress, max_items):
    """The Ingress's TLS hosts and the Secret each names. The Secret is never read."""
    rows = [
        {"hosts": nested(t.get("hosts") or []), "secretName": t.get("secretName")}
        for t in (ingress.get("spec") or {}).get("tls") or []
    ]
    return cap(rows, max_items)


def gke_backends(ingress, max_items):
    """The GKE `ingress.kubernetes.io/backends` annotation as per-backend health.

    The annotation is a JSON object of backend name -> HEALTHY | UNHEALTHY |
    UNKNOWN, written by the GKE controller. Returns (view, problem): `problem` is
    a message when the annotation is present but is not that shape, in which
    case `backends` is None (unknown), not empty. An absent annotation is
    present: False with backends [] (nothing was reported, which is not health).
    """
    raw = ((ingress.get("metadata") or {}).get("annotations") or {}).get(GKE_BACKENDS_ANNOTATION)
    view = {"annotation": GKE_BACKENDS_ANNOTATION, "present": raw is not None}
    if raw is None:
        return dict(view, backends=[], notHealthy=0), None
    try:
        doc = json.loads(raw)
        if not isinstance(doc, dict):
            raise ValueError(f"expected a JSON object, got {type(doc).__name__}")
    except ValueError as exc:
        return dict(view, backends=None, notHealthy=None), f"{GKE_BACKENDS_ANNOTATION}: {exc}"
    rows = [
        {"name": name, "health": str(h), "healthy": str(h).upper() == "HEALTHY"}
        for name, h in sorted(doc.items())
    ]
    # Unhealthy backends first, so a cut at maxItems keeps the ones that matter.
    rows.sort(key=lambda r: r["healthy"])
    return dict(
        view, backends=cap(rows, max_items), notHealthy=sum(1 for r in rows if not r["healthy"])
    ), None


# ------------------------------------------------------------- address ----


def lb_addresses(status):
    """[{ip, hostname}] from a status.loadBalancer.ingress list (Ingress or Service)."""
    rows = [
        {"ip": a.get("ip"), "hostname": a.get("hostname")}
        for a in ((status or {}).get("loadBalancer") or {}).get("ingress") or []
    ]
    return nested(rows)


def _address_keys(addrs):
    return {a["ip"] or a["hostname"] for a in addrs if a["ip"] or a["hostname"]}


def ingress_view(ingress):
    addrs = lb_addresses(ingress.get("status"))
    meta = ingress.get("metadata") or {}
    return {
        "name": meta.get("name"),
        "namespace": meta.get("namespace"),
        "ingressClassName": (ingress.get("spec") or {}).get("ingressClassName"),
        "classAnnotation": (meta.get("annotations") or {}).get(CLASS_ANNOTATION),
        "addresses": addrs,
        "assigned": bool(_address_keys(addrs)),
    }


def class_view(name, ic, looked):
    """The IngressClass the Ingress names. `looked` is False when the read was forbidden.

    exists is None when it was not looked up (no class named, or forbidden); only
    False means the class is missing.
    """
    if not name:
        return {"name": None, "exists": None, "controller": None, "isDefault": None}
    if not looked:
        return {"name": name, "exists": None, "controller": None, "isDefault": None}
    if ic is None:
        return {"name": name, "exists": False, "controller": None, "isDefault": None}
    annotations = (ic.get("metadata") or {}).get("annotations") or {}
    return {
        "name": name,
        "exists": True,
        "controller": (ic.get("spec") or {}).get("controller"),
        "isDefault": annotations.get(DEFAULT_CLASS_ANNOTATION) == "true",
    }


def service_addresses(ingress, services, max_items):
    """Each backend Service's type and LoadBalancer address, against the Ingress's own.

    `differsFromIngress` is the classic conflict check: a Service of type
    LoadBalancer that has its own address, which is not one of the Ingress's. It
    is None when it cannot be said (not a LoadBalancer Service, the Service is
    missing or was not looked up, or either side has no address yet).
    """
    ing_addrs = _address_keys(lb_addresses(ingress.get("status")))
    rows = []
    for name in service_names(backend_refs(ingress)):
        row = {
            "service": name,
            "exists": None,
            "type": None,
            "addresses": [],
            "differsFromIngress": None,
        }
        if name in services:
            svc = services[name]
            row["exists"] = svc is not None
            if svc is not None:
                row["type"] = (svc.get("spec") or {}).get("type") or "ClusterIP"
                row["addresses"] = lb_addresses(svc.get("status"))
                svc_addrs = _address_keys(row["addresses"])
                if row["type"] == "LoadBalancer" and svc_addrs and ing_addrs:
                    row["differsFromIngress"] = not (svc_addrs & ing_addrs)
        rows.append(row)
    return cap(rows, max_items)


# -------------------------------------------------------------- events ----


def _event_times(ev):
    series = ev.get("series") or {}
    meta = ev.get("metadata", {})
    last = (
        parse_time(series.get("lastObservedTime"))
        or parse_time(ev.get("lastTimestamp"))
        or parse_time(ev.get("eventTime"))
        or parse_time(ev.get("firstTimestamp"))
        or parse_time(meta.get("creationTimestamp"))
    )
    first = (
        parse_time(ev.get("firstTimestamp"))
        or parse_time(ev.get("eventTime"))
        or parse_time(meta.get("creationTimestamp"))
        or last
    )
    return first, last


def warning_events(events, subjects, since, now, max_items):
    """Warning events about the given objects whose last occurrence is within `since`.

    `subjects` is a list of (kind, name, uid); uid is None when the object is not
    known (a Service that does not exist is matched by name). An event whose
    involvedObject carries a different uid belongs to an earlier object of the
    same name and is left out. Grouped by object, type and reason, newest first.
    `count` sums the events' own lifetime counters.
    """
    cutoff = now - parse_duration(since)
    wanted = {(kind, name): uid for kind, name, uid in subjects}
    groups = {}
    for ev in events:
        if ev.get("type") != "Warning":
            continue
        obj = ev.get("involvedObject") or {}
        key = (obj.get("kind"), obj.get("name"))
        if key not in wanted:
            continue
        uid = wanted[key]
        if uid and obj.get("uid") and obj["uid"] != uid:
            continue
        first, last = _event_times(ev)
        if last is None or last < cutoff:
            continue
        series = ev.get("series") or {}
        count = series.get("count") or ev.get("count") or 1
        gkey = (*key, ev.get("type"), ev.get("reason"))
        g = groups.get(gkey)
        if g is None:
            groups[gkey] = g = {
                "kind": key[0],
                "name": key[1],
                "type": ev.get("type"),
                "reason": ev.get("reason"),
                "count": 0,
                "firstSeen": first,
                "lastSeen": last,
                "message": ev.get("message"),
                "truncated": False,
                "reportingComponent": ev.get("reportingComponent")
                or (ev.get("source") or {}).get("component"),
            }
        g["count"] += count
        if first and (g["firstSeen"] is None or first < g["firstSeen"]):
            g["firstSeen"] = first
        if last >= g["lastSeen"]:
            g["lastSeen"] = last
            g["message"] = ev.get("message")
    rows = sorted(groups.values(), key=lambda g: (g["lastSeen"], g["reason"] or ""), reverse=True)
    rows = cap(rows, max_items)
    for g in rows:
        g["firstSeen"] = fmt_time(g["firstSeen"])
        g["lastSeen"] = fmt_time(g["lastSeen"])
        g["message"], g["truncated"] = trunc(g["message"])
    return rows
