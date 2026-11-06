"""Pure functions behind the k8s-ingress-health tasks.

Everything here takes already-parsed Kubernetes JSON and returns plain facts:
no kubectl, no clock, no environment. Scripts fetch, call these, and return.
"""

import json
import re
from datetime import datetime, timedelta, timezone

from k8s_ingress_health.errors import RwError

_DURATION = re.compile(r"(\d+)\s*(ms|[smhd])")
_UNIT_MS = {"ms": 1, "s": 1000, "m": 60_000, "h": 3_600_000, "d": 86_400_000}

NESTED_LIMIT = 50  # most entries in a list nested inside an output entry
TEXT_LIMIT = 1024  # most characters in a free-text field
MAX_SERVICES = 100  # most distinct backend Services looked up

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


def cut(text):
    """text cut to TEXT_LIMIT characters (a name or short field, no flag). None stays None."""
    return None if text is None else str(text)[:TEXT_LIMIT]


def wrap(items, max_items, **extra):
    """A list output cut at max_items: {items, total, truncated} plus any extra facts."""
    items = list(items)
    rows = items[: int(max_items)]
    return {"items": rows, "total": len(items), "truncated": len(items) > len(rows), **extra}


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
        "host": cut(host),
        "path": cut(path),
        "pathType": cut(path_type),
        "kind": "service" if backend.get("service") else "resource",
        "service": None,
        "resource": None,
    }
    svc = backend.get("service")
    if svc:
        port = svc.get("port") or {}
        ref["service"] = {
            "name": cut(svc.get("name")),
            "portNumber": port.get("number"),
            "portName": cut(port.get("name")),
        }
    else:
        res = backend.get("resource") or {}
        ref["resource"] = {
            "apiGroup": cut(res.get("apiGroup")),
            "kind": cut(res.get("kind")),
            "name": cut(res.get("name")),
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


def slices_for_port(slices, service_port):
    """The EndpointSlices that carry one Service port.

    A Service port with a name is carried by slices whose `ports[]` has that name.
    An unnamed one (a Service with a single port) is carried by slices with an
    unnamed port whose number is the port's targetPort, when that is a number. A
    slice with no ports carries none: the pods behind it do not expose the port.
    """
    name = service_port.get("name")
    target = service_port.get("targetPort", service_port.get("port"))
    try:
        want = int(target)
    except (TypeError, ValueError):
        want = None  # a container port name: not resolvable from the Service

    def carries(slice_port):
        if name:
            return slice_port.get("name") == name
        return not slice_port.get("name") and (want is None or slice_port.get("port") == want)

    return [sl for sl in slices if any(carries(p) for p in sl.get("ports") or [])]


def endpoint_counts(slices):
    """Endpoint counts across the given EndpointSlices.

    A pod listed by several slices is counted once, and is ready if it is ready in
    any of them. An unset `ready` or `serving` condition counts as true (the API's
    rule); `terminating` counts only an explicit true. `terminating` endpoints are
    usually also not ready. `slices` is the number of slices counted.
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
            cond = ep.get("conditions") or {}
            cur = seen.setdefault(ident, [False, False, False])
            cur[0] |= cond.get("ready") is not False
            cur[1] |= cond.get("serving") is not False
            cur[2] |= cond.get("terminating") is True
    ready = sum(1 for c in seen.values() if c[0])
    return {
        "ready": ready,
        "notReady": len(seen) - ready,
        "serving": sum(1 for c in seen.values() if c[1]),
        "terminating": sum(1 for c in seen.values() if c[2]),
        "slices": len(slices),
    }


def backend_entries(ingress, services, slices_by_service):
    """One entry per backend ref with what the cluster says about its Service.

    `services` maps a Service name to its object, or to None when it does not
    exist; a name missing from the map was not looked up (forbidden, or past the
    lookup cap), so every fact derived from the Service is None, not False.
    `slices_by_service` maps a name to its EndpointSlices and is missing a name
    when they could not be read. Endpoints are counted per backend port; they are
    None when the port does not exist on the Service (nothing to count).
    """
    out = []
    for ref in backend_refs(ingress):
        svc_ref = ref["service"]
        entry = dict(ref)
        entry.update(
            serviceExists=None,
            serviceType=None,
            hasSelector=None,
            publishNotReady=None,
            portExists=None,
            targetPort=None,
            protocol=None,
            endpoints=None,
        )
        if svc_ref and svc_ref["name"] in services:
            svc = services[svc_ref["name"]]
            entry["serviceExists"] = svc is not None
            if svc is not None:
                spec = svc.get("spec") or {}
                entry["serviceType"] = spec.get("type") or "ClusterIP"
                entry["hasSelector"] = bool(spec.get("selector"))
                entry["publishNotReady"] = bool(spec.get("publishNotReadyAddresses"))
                sp = match_port(svc, svc_ref["portNumber"], svc_ref["portName"])
                slices = slices_by_service.get(svc_ref["name"])
                if sp is not None:
                    entry["portExists"] = True
                    target = sp.get("targetPort", sp.get("port"))
                    entry["targetPort"] = cut(target) if isinstance(target, str) else target
                    entry["protocol"] = sp.get("protocol")
                    if slices is not None:
                        entry["endpoints"] = endpoint_counts(slices_for_port(slices, sp))
                elif entry["serviceType"] == "ExternalName" and not spec.get("ports"):
                    # no ports to match: not authoritative either way, and no endpoints expected
                    if slices is not None:
                        entry["endpoints"] = endpoint_counts(slices)
                else:
                    entry["portExists"] = False
        out.append(entry)
    return out


def tls_view(ingress, max_items):
    """The Ingress's TLS hosts and the Secret each names. The Secret is never read."""
    rows = []
    for t in (ingress.get("spec") or {}).get("tls") or []:
        hosts = t.get("hosts") or []
        rows.append(
            {
                "hosts": [cut(h) for h in nested(hosts)],
                "secretName": cut(t.get("secretName")),
                "truncated": len(hosts) > NESTED_LIMIT,
            }
        )
    return wrap(rows, max_items)


# A GKE container-native (NEG) backend name: k8s1-<cluster hash>-<namespace>-<service>-<port>-<hash>.
_NEG = re.compile(r"^k8s1-[0-9a-f]{8}-(?P<mid>.+)-(?P<port>\d+)-[0-9a-f]{8}$")


def parse_neg_name(name, namespace):
    """(service, port) out of a NEG-style backend name, None for what does not parse.

    The name does not say where the namespace ends and the Service begins, so the
    Service is read only when the middle starts with the Ingress's own namespace;
    GKE cuts long names, so it may be a prefix of the real Service name. The port
    is the Service port. Instance-group backends (k8s-be-<nodePort>--<hash>) give
    neither.
    """
    m = _NEG.match(name)
    if not m:
        return None, None
    mid, prefix = m["mid"], f"{namespace}-" if namespace else None
    service = (
        mid[len(prefix) :] if prefix and mid.startswith(prefix) and len(mid) > len(prefix) else None
    )
    return (cut(service) if service else None), int(m["port"])


def gke_backends(ingress, max_items):
    """The GKE `ingress.kubernetes.io/backends` annotation as per-backend health.

    The annotation is a JSON object of backend name -> HEALTHY | UNHEALTHY |
    UNKNOWN, written by the GKE controller. Returns (view, problem): `problem` is
    a message when the annotation is present but is not that shape, in which
    case `backends` is None (unknown), not empty. An absent annotation is
    present: False with backends [] (nothing was reported, which is not health).
    """
    meta = ingress.get("metadata") or {}
    raw = (meta.get("annotations") or {}).get(GKE_BACKENDS_ANNOTATION)
    view = {"annotation": GKE_BACKENDS_ANNOTATION, "present": raw is not None}
    if raw is None:
        return dict(view, backends=[], total=0, truncated=False, notHealthy=0), None
    try:
        doc = json.loads(raw)
        if not isinstance(doc, dict):
            raise ValueError(f"expected a JSON object, got {type(doc).__name__}")
    except ValueError as exc:
        return (
            dict(view, backends=None, total=None, truncated=None, notHealthy=None),
            f"{GKE_BACKENDS_ANNOTATION}: {exc}"[:TEXT_LIMIT],
        )
    rows = []
    for name, h in sorted(doc.items()):
        service, port = parse_neg_name(name, meta.get("namespace"))
        rows.append(
            {
                "name": cut(name),
                "health": cut(h),
                "healthy": str(h).upper() == "HEALTHY",
                "service": service,
                "port": port,
            }
        )
    # Unhealthy backends first, so a cut at maxItems keeps the ones that matter.
    rows.sort(key=lambda r: r["healthy"])
    out = wrap(rows, max_items)
    return dict(
        view,
        backends=out["items"],
        total=out["total"],
        truncated=out["truncated"],
        notHealthy=sum(1 for r in rows if not r["healthy"]),
    ), None


_GKE_LB = {
    "forwardingRule": "ingress.kubernetes.io/forwarding-rule",
    "httpsForwardingRule": "ingress.kubernetes.io/https-forwarding-rule",
    "urlMap": "ingress.kubernetes.io/url-map",
    "targetProxy": "ingress.kubernetes.io/target-proxy",
    "httpsTargetProxy": "ingress.kubernetes.io/https-target-proxy",
    "staticIp": "ingress.kubernetes.io/static-ip",
}


def gke_load_balancer(ingress):
    """The names of the GCP load balancer objects the GKE controller recorded on the Ingress.

    Plain annotation values, so a caller can point the gcp plugin at them; this
    skill never talks to GCP. `present` is true when any of them is set.
    """
    annotations = (ingress.get("metadata") or {}).get("annotations") or {}
    view = {field: cut(annotations.get(key)) for field, key in _GKE_LB.items()}
    return {"present": any(v is not None for v in view.values()), **view}


# ------------------------------------------------------------- address ----


def _all_addresses(status):
    return [
        {"ip": cut(a.get("ip")), "hostname": cut(a.get("hostname"))}
        for a in ((status or {}).get("loadBalancer") or {}).get("ingress") or []
    ]


def _address_keys(addrs):
    return {a["ip"] or a["hostname"] for a in addrs if a["ip"] or a["hostname"]}


def ingress_view(ingress):
    addrs = _all_addresses(ingress.get("status"))
    meta = ingress.get("metadata") or {}
    return {
        "name": cut(meta.get("name")),
        "namespace": cut(meta.get("namespace")),
        "ingressClassName": cut((ingress.get("spec") or {}).get("ingressClassName")),
        "classAnnotation": cut((meta.get("annotations") or {}).get(CLASS_ANNOTATION)),
        "addresses": nested(addrs),
        "addressesTruncated": len(addrs) > NESTED_LIMIT,
        "assigned": bool(_address_keys(addrs)),
    }


def class_source(ingress):
    """(source, name) of the class an Ingress asks for: spec.ingressClassName, else the
    legacy kubernetes.io/ingress.class annotation, else ('none', None)."""
    name = (ingress.get("spec") or {}).get("ingressClassName")
    if name:
        return "spec", name
    legacy = ((ingress.get("metadata") or {}).get("annotations") or {}).get(CLASS_ANNOTATION)
    if legacy:
        return "annotation", legacy
    return "none", None


def class_view(source, name, ic, ic_state, classes):
    """What is known about the Ingress's class.

    source `spec`: the named IngressClass was read (`ic`, `ic_state` ok | not_found |
    denied). source `annotation`: the legacy value is a controller's class string, not
    an IngressClass object, so nothing is looked up. source `none`: no class is
    named and `classes` (every IngressClass, None when the list was forbidden) says
    which are the default; exactly one default is the effective class. `lookedUp` is
    true when a named class was read; with `exists` null and a source other than
    `none` it means the read was forbidden.
    """
    view = {
        "name": cut(name),
        "classSource": source,
        "lookedUp": False,
        "exists": None,
        "controller": None,
        "isDefault": None,
        "defaultClasses": None,
        "effectiveClass": cut(name),
        "multipleDefaults": None,
    }
    if source == "spec" and ic_state in ("ok", "not_found"):
        view["lookedUp"] = True
        view["exists"] = ic_state == "ok"
        if ic_state == "ok":
            annotations = (ic.get("metadata") or {}).get("annotations") or {}
            view["controller"] = cut((ic.get("spec") or {}).get("controller"))
            view["isDefault"] = annotations.get(DEFAULT_CLASS_ANNOTATION) == "true"
    elif source == "none" and classes is not None:
        defaults = sorted(
            c["metadata"]["name"]
            for c in classes
            if ((c.get("metadata") or {}).get("annotations") or {}).get(DEFAULT_CLASS_ANNOTATION)
            == "true"
        )
        view["defaultClasses"] = [cut(d) for d in nested(defaults)]
        view["multipleDefaults"] = len(defaults) > 1
        view["effectiveClass"] = cut(defaults[0]) if len(defaults) == 1 else None
    return view


def service_addresses(ingress, services, max_items, **extra):
    """Each backend Service's type and LoadBalancer address, against the Ingress's own.

    `differsFromIngress` is the classic conflict check: a Service of type
    LoadBalancer that has its own address, which is not one of the Ingress's. It
    is None when it cannot be said (not a LoadBalancer Service, the Service is
    missing or was not looked up, or either side has no address yet).
    """
    ing_addrs = _address_keys(_all_addresses(ingress.get("status")))
    rows = []
    for name in service_names(backend_refs(ingress)):
        row = {
            "service": name,
            "exists": None,
            "type": None,
            "addresses": [],
            "addressesTruncated": False,
            "differsFromIngress": None,
        }
        if name in services:
            svc = services[name]
            row["exists"] = svc is not None
            if svc is not None:
                row["type"] = (svc.get("spec") or {}).get("type") or "ClusterIP"
                addrs = _all_addresses(svc.get("status"))
                row["addresses"] = nested(addrs)
                row["addressesTruncated"] = len(addrs) > NESTED_LIMIT
                svc_addrs = _address_keys(addrs)
                if row["type"] == "LoadBalancer" and svc_addrs and ing_addrs:
                    row["differsFromIngress"] = not (svc_addrs & ing_addrs)
        rows.append(row)
    return wrap(rows, max_items, **extra)


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
    `count` sums the events' own counters, which count over the event's whole
    lifetime, not only the window.
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
                "kind": cut(key[0]),
                "name": cut(key[1]),
                "type": ev.get("type"),
                "reason": cut(ev.get("reason")),
                "count": 0,
                "firstSeen": first,
                "lastSeen": last,
                "message": ev.get("message"),
                "truncated": False,
                "reportingComponent": cut(
                    ev.get("reportingComponent") or (ev.get("source") or {}).get("component")
                ),
            }
        g["count"] += count
        if first and (g["firstSeen"] is None or first < g["firstSeen"]):
            g["firstSeen"] = first
        if last >= g["lastSeen"]:
            g["lastSeen"] = last
            g["message"] = ev.get("message")
    rows = sorted(groups.values(), key=lambda g: (g["lastSeen"], g["reason"] or ""), reverse=True)
    out = wrap(rows, max_items)
    for g in out["items"]:
        g["firstSeen"] = fmt_time(g["firstSeen"])
        g["lastSeen"] = fmt_time(g["lastSeen"])
        g["message"], g["truncated"] = trunc(g["message"])
    return out
