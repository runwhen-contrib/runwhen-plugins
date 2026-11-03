"""Pure functions behind the k8s-certmanager-health tasks.

Everything here takes already-parsed Kubernetes JSON and returns plain facts:
no kubectl, no clock, no environment. Scripts fetch, call these, and return.
"""

from datetime import datetime, timezone

NESTED_LIMIT = 50  # most entries in a list nested inside an output entry
TEXT_LIMIT = 1024  # most characters in a free-text field
DAY = 86400.0

CERT_MANAGER_GROUP = "cert-manager.io"
REVISION_ANNOTATION = "cert-manager.io/certificate-revision"
REQUEST_CONDITIONS = {
    "ready": "Ready",
    "approved": "Approved",
    "denied": "Denied",
    "invalidRequest": "InvalidRequest",
}

# ---------------------------------------------------------------- time ----


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


# ---------------------------------------------------------- conditions ----


def condition_view(cond):
    """One status condition as plain facts; the message is cut at 1024 characters."""
    message, cut = trunc(cond.get("message"))
    reason, reason_cut = trunc(cond.get("reason"))
    return {
        "type": cond.get("type"),
        "status": cond.get("status"),
        "reason": reason,
        "message": message,
        "truncated": cut or reason_cut,
        "lastTransitionTime": cond.get("lastTransitionTime"),
        "observedGeneration": cond.get("observedGeneration"),
    }


def condition_of(obj, ctype):
    """The condition view of type `ctype` in obj.status.conditions, or None when it has none."""
    for cond in (obj.get("status") or {}).get("conditions") or []:
        if cond.get("type") == ctype:
            return condition_view(cond)
    return None


def is_true(cond_view):
    """True / False for a condition's status, None when the condition is absent or Unknown."""
    if cond_view is None or cond_view["status"] not in ("True", "False"):
        return None
    return cond_view["status"] == "True"


# --------------------------------------------------------- certificate ----


def certificate_view(cert, now, warning_days):
    """Readiness, validity window and spec facts of a Certificate.

    `expiresInDays` is the time left until status.notAfter (negative once passed);
    `expiringSoon` is true when that is within `warning_days` or already past, and
    `expired` says which. A Certificate that has not issued yet has no notAfter, so
    these are null (unknown), not false.
    """
    spec = cert.get("spec") or {}
    status = cert.get("status") or {}
    meta = cert["metadata"]
    not_after = parse_time(status.get("notAfter"))
    renewal = parse_time(status.get("renewalTime"))
    left = (not_after - now).total_seconds() if not_after else None
    dns = list(spec.get("dnsNames") or [])
    sans = sum(
        len(spec.get(k) or []) for k in ("dnsNames", "ipAddresses", "uris", "emailAddresses")
    )
    conditions = [condition_view(c) for c in status.get("conditions") or []]
    return {
        "name": meta["name"],
        "namespace": meta.get("namespace"),
        "generation": meta.get("generation"),
        "ready": condition_of(cert, "Ready"),
        "issuing": condition_of(cert, "Issuing"),
        "conditions": nested(conditions),
        "notBefore": status.get("notBefore"),
        "notAfter": status.get("notAfter"),
        "renewalTime": status.get("renewalTime"),
        "renewalTimePassed": None if renewal is None else renewal <= now,
        "revision": status.get("revision"),
        "lastFailureTime": status.get("lastFailureTime"),
        "expiresInDays": None if left is None else round(left / DAY, 2),
        "expired": None if left is None else left <= 0,
        "expiringSoon": None if left is None else left <= float(warning_days) * DAY,
        "expiryWarningDays": float(warning_days),
        "commonName": spec.get("commonName"),
        "dnsNameCount": len(dns),
        "sanCount": sans,
        "dnsNames": nested(dns),
        "secretName": spec.get("secretName"),
        "duration": spec.get("duration"),
        "renewBefore": spec.get("renewBefore"),
    }


def issuer_ref(cert):
    """(name, kind, group) of spec.issuerRef with cert-manager's defaults (Issuer, cert-manager.io)."""
    ref = (cert.get("spec") or {}).get("issuerRef") or {}
    return ref.get("name"), ref.get("kind") or "Issuer", ref.get("group") or CERT_MANAGER_GROUP


def issuer_lookup(cert):
    """('issuers' | 'clusterissuers' | None, name): which object to read for the Certificate's issuerRef.

    None for an issuer of another group (an external issuer such as AWS PCA), which this
    skill does not look up.
    """
    name, kind, group = issuer_ref(cert)
    if group != CERT_MANAGER_GROUP or not name:
        return None, name
    if kind == "Issuer":
        return "issuers", name
    if kind == "ClusterIssuer":
        return "clusterissuers", name
    return None, name


def issuer_view(cert, obj, state):
    """Whether the Certificate's issuer exists and is Ready.

    state: 'ok' (obj read), 'not_found' (does not exist), 'denied' (could not be read),
    'external' (not a cert-manager Issuer/ClusterIssuer: not looked up). `exists` and
    `ready` are null when not known.
    """
    name, kind, group = issuer_ref(cert)
    ready = condition_of(obj, "Ready") if obj else None
    return {
        "name": name,
        "kind": kind,
        "group": group,
        "scope": {"Issuer": "namespace", "ClusterIssuer": "cluster"}.get(kind)
        if group == CERT_MANAGER_GROUP
        else None,
        "lookedUp": state in ("ok", "not_found"),
        "exists": {"ok": True, "not_found": False}.get(state),
        "ready": is_true(ready),
        "readyCondition": ready,
    }


# ------------------------------------------------------------ requests ----


def revision_of(req):
    raw = ((req.get("metadata") or {}).get("annotations") or {}).get(REVISION_ANNOTATION)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def owned_by(obj, owner_kind, owner_uid, owner_name):
    """Whether obj has an ownerReference to the object (by uid; by name only when the reference has no uid)."""
    for ref in (obj.get("metadata") or {}).get("ownerReferences") or []:
        if ref.get("kind") != owner_kind:
            continue
        if ref.get("uid"):
            if ref["uid"] == owner_uid:
                return True
        elif ref.get("name") == owner_name:
            return True
    return False


def request_view(req):
    """A CertificateRequest: its Ready/Approved/Denied/InvalidRequest conditions, revision and failure time."""
    meta = req["metadata"]
    status = req.get("status") or {}
    name, kind, group = issuer_ref(req)
    row = {
        "name": meta["name"],
        "creationTimestamp": meta.get("creationTimestamp"),
        "revision": revision_of(req),
        "failureTime": status.get("failureTime"),
        "issuerRef": {"name": name, "kind": kind, "group": group},
    }
    for key, ctype in REQUEST_CONDITIONS.items():
        row[key] = condition_of(req, ctype)
    return row


def certificate_requests(items, cert, max_items):
    """The CertificateRequests the Certificate owns, newest revision first, cut to max_items.

    Returns (rows, owners): owners maps every returned request's name to its uid, revision and
    creationTimestamp (for matching and ordering its Orders).
    """
    uid, name = cert["metadata"].get("uid"), cert["metadata"]["name"]
    mine = [r for r in items if owned_by(r, "Certificate", uid, name)]
    mine.sort(
        key=lambda r: (
            revision_of(r) if revision_of(r) is not None else -1,
            r["metadata"].get("creationTimestamp") or "",
        ),
        reverse=True,
    )
    kept = cap(mine, max_items)
    return (
        [request_view(r) for r in kept],
        {
            r["metadata"]["name"]: {
                "uid": r["metadata"].get("uid"),
                "revision": revision_of(r),
                "creationTimestamp": r["metadata"].get("creationTimestamp"),
            }
            for r in kept
        },
    )


# -------------------------------------------------------------- orders ----


def order_view(order, request_name):
    """An ACME Order: its state (pending, valid, ready, invalid, expired, errored) and failure reason."""
    status = order.get("status") or {}
    reason, cut = trunc(status.get("reason"))
    return {
        "name": order["metadata"]["name"],
        "request": request_name,
        "state": status.get("state"),
        "reason": reason,
        "truncated": cut,
        "failureTime": status.get("failureTime"),
        "authorizationCount": len(status.get("authorizations") or []),
    }


def orders_for(items, owners, max_items):
    """Orders owned by the returned requests, newest request first, cut to max_items.

    `owners` is certificate_requests()'s second result. Orders are ordered by their request's
    (revision, creationTimestamp), the way the requests are, never by name. Returns
    (rows, uid_by_order_name); the uids are for matching Challenges.
    """
    by_uid = {o["uid"]: (name, o) for name, o in owners.items() if o.get("uid")}
    found = []
    for o in items:
        for ref in (o.get("metadata") or {}).get("ownerReferences") or []:
            if ref.get("kind") == "CertificateRequest" and ref.get("uid") in by_uid:
                req_name, req = by_uid[ref["uid"]]
                rev = req["revision"] if req["revision"] is not None else -1
                key = (rev, req["creationTimestamp"] or "", o["metadata"]["name"])
                found.append((key, order_view(o, req_name), o["metadata"].get("uid")))
                break
    found.sort(key=lambda t: t[0], reverse=True)
    kept = found[: int(max_items)]
    return [row for _k, row, _u in kept], {row["name"]: uid for _k, row, uid in kept}


# ---------------------------------------------------------- challenges ----


def challenge_view(ch, order_name):
    """An ACME Challenge: its type, domain and state; the reason is where a failed validation says why."""
    spec = ch.get("spec") or {}
    status = ch.get("status") or {}
    reason, cut = trunc(status.get("reason"))
    return {
        "name": ch["metadata"]["name"],
        "order": order_name,
        "type": spec.get("type"),
        "dnsName": spec.get("dnsName"),
        "wildcard": spec.get("wildcard"),
        "state": status.get("state"),
        "presented": status.get("presented"),
        "processing": status.get("processing"),
        "reason": reason,
        "truncated": cut,
    }


def challenges_for(items, order_uids, max_items):
    """Challenges owned by the returned Orders ({order name: uid}), cut to max_items."""
    by_uid = {uid: name for name, uid in order_uids.items() if uid}
    rows = []
    for ch in items:
        for ref in (ch.get("metadata") or {}).get("ownerReferences") or []:
            if ref.get("kind") == "Order" and ref.get("uid") in by_uid:
                rows.append(challenge_view(ch, by_uid[ref["uid"]]))
                break
    rows.sort(key=lambda r: (r["order"], r["name"]), reverse=True)
    return cap(rows, max_items)
