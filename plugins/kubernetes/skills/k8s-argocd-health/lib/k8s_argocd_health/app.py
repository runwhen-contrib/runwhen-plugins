"""Pure functions behind the k8s-argocd-health tasks.

Everything here takes the parsed Argo CD Application (argoproj.io/v1alpha1) and
returns plain facts: no kubectl, no clock, no environment. Helm values are never
copied: only the names of value files and parameters, and whether inline values
exist.
"""

from urllib.parse import urlsplit, urlunsplit

NESTED_LIMIT = 50  # most entries in a list nested inside an output entry
TEXT_LIMIT = 1024  # most characters in a free-text field

# Sort order of the unhealthy-resources list: worst first.
_HEALTH_RANK = {"Degraded": 0, "Missing": 1, "Unknown": 2, "Progressing": 3, "Suspended": 4}


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


def clean_url(url):
    """A repository URL without credentials (user:token@host); anything unparsable is returned as is."""
    if not url:
        return None
    try:
        parts = urlsplit(str(url))
    except ValueError:
        return str(url)
    if parts.scheme and parts.netloc and "@" in parts.netloc and "://" in str(url):
        return urlunsplit(parts._replace(netloc=parts.netloc.rsplit("@", 1)[1]))
    return str(url)


def _status(app):
    return app.get("status") or {}


def _spec(app):
    return app.get("spec") or {}


# ------------------------------------------------------------- sources ----


def spec_sources(app):
    """The Application's source list, multi-source aware.

    `spec.sources` wins when it has entries (Argo CD ignores `spec.source`
    then); otherwise the single `spec.source`, otherwise nothing.
    """
    spec = _spec(app)
    many = spec.get("sources")
    if isinstance(many, list) and many:
        return [s for s in many if isinstance(s, dict)]
    one = spec.get("source")
    return [one] if isinstance(one, dict) else []


def source_view(index, source):
    """Where one source comes from: repository, path or chart, revision to track."""
    return {
        "index": index,
        "repoURL": clean_url(source.get("repoURL")),
        "path": source.get("path"),
        "chart": source.get("chart"),
        "targetRevision": source.get("targetRevision"),
        "ref": source.get("ref"),
        "hasHelm": isinstance(source.get("helm"), dict),
    }


def source_views(app):
    return nested(source_view(i, s) for i, s in enumerate(spec_sources(app)))


def _history_source(source):
    """A history entry's source reduced to where it came from (its helm block carries values)."""
    return {
        "repoURL": clean_url(source.get("repoURL")),
        "path": source.get("path"),
        "chart": source.get("chart"),
        "targetRevision": source.get("targetRevision"),
    }


# -------------------------------------------------------- sync-status ----


def application_view(app):
    """Identity, destination and sync policy of the Application."""
    md, spec, status = app.get("metadata") or {}, _spec(app), _status(app)
    dest = spec.get("destination") or {}
    policy = spec.get("syncPolicy") or {}
    automated = policy.get("automated")
    auto = isinstance(automated, dict)  # `automated: {}` is enabled, with the defaults
    retry = policy.get("retry") or {}
    return {
        "name": md.get("name"),
        "namespace": md.get("namespace"),
        "project": spec.get("project"),
        "destination": {
            "server": dest.get("server"),
            "name": dest.get("name"),
            "namespace": dest.get("namespace"),
        },
        "deletionTimestamp": md.get("deletionTimestamp"),
        "operationInProgress": app.get("operation") is not None,
        "resourceCount": len(status.get("resources") or []),
        "sourceTypes": nested(_source_types(app)),
        "syncPolicy": {
            "automated": auto,
            "prune": bool((automated or {}).get("prune")) if auto else None,
            "selfHeal": bool((automated or {}).get("selfHeal")) if auto else None,
            "allowEmpty": bool((automated or {}).get("allowEmpty")) if auto else None,
            "syncOptions": nested(str(o) for o in policy.get("syncOptions") or []),
            "retryLimit": retry.get("limit"),
        },
    }


def _source_types(app):
    status = _status(app)
    types = status.get("sourceTypes")
    if not types and status.get("sourceType"):
        types = [status["sourceType"]]
    return [str(t) for t in types or []]


def sync_view(app):
    sync = _status(app).get("sync") or {}
    return {
        "status": sync.get("status"),
        "revision": sync.get("revision"),
        "revisions": nested(str(r) for r in sync.get("revisions") or []),
        "reconciledAt": _status(app).get("reconciledAt"),
    }


def health_view(app):
    health = _status(app).get("health") or {}
    message, cut = trunc(health.get("message"))
    return {
        "status": health.get("status"),
        "message": message,
        "truncated": cut,
        "lastTransitionTime": health.get("lastTransitionTime"),
    }


def conditions_view(app, max_items):
    """The Application's conditions (ComparisonError, SyncError, InvalidSpecError, ...)."""
    rows = []
    for c in _status(app).get("conditions") or []:
        message, cut = trunc(c.get("message"))
        rows.append(
            {
                "type": c.get("type"),
                "message": message,
                "truncated": cut,
                "lastTransitionTime": c.get("lastTransitionTime"),
            }
        )
    return cap(rows, max_items)


def _sync_failure(r):
    message, cut = trunc(r.get("message"))
    return {
        "group": r.get("group") or "",
        "kind": r.get("kind"),
        "namespace": r.get("namespace"),
        "name": r.get("name"),
        "status": r.get("status"),
        "hookPhase": r.get("hookPhase"),
        "message": message,
        "truncated": cut,
    }


def operation_view(app):
    """The last sync operation (status.operationState), or None when none ever ran."""
    op = _status(app).get("operationState")
    if not isinstance(op, dict):
        return None
    result = op.get("syncResult") or {}
    message, cut = trunc(op.get("message"))
    initiated = (op.get("operation") or {}).get("initiatedBy") or {}
    failures = [
        _sync_failure(r)
        for r in result.get("resources") or []
        if r.get("status") == "SyncFailed" or r.get("hookPhase") in ("Failed", "Error")
    ]
    return {
        "phase": op.get("phase"),
        "message": message,
        "truncated": cut,
        "startedAt": op.get("startedAt"),
        "finishedAt": op.get("finishedAt"),
        "retryCount": op.get("retryCount") or 0,
        "revision": result.get("revision"),
        "revisions": nested(str(r) for r in result.get("revisions") or []),
        "automated": bool(initiated.get("automated")),
        "failureCount": len(failures),
        "failures": nested(failures),
    }


# --------------------------------------------------- unhealthy-resources ----


def unhealthy_resources(app, max_items):
    """status.resources entries that are not Healthy or are OutOfSync, worst first.

    An entry with no health (kinds Argo CD has no health check for) counts as
    unhealthy only through being OutOfSync.
    """
    entries = _status(app).get("resources") or []
    rows = []
    unhealthy = out_of_sync = 0
    for r in entries:
        health = r.get("health") or {}
        h_status = health.get("status")
        bad_health = bool(h_status) and h_status != "Healthy"
        drifted = r.get("status") == "OutOfSync"
        if not (bad_health or drifted):
            continue
        unhealthy += bad_health
        out_of_sync += drifted
        message, cut = trunc(health.get("message"))
        rows.append(
            {
                "group": r.get("group") or "",
                "version": r.get("version"),
                "kind": r.get("kind"),
                "namespace": r.get("namespace"),
                "name": r.get("name"),
                "syncStatus": r.get("status"),
                "health": h_status,
                "message": message,
                "truncated": cut,
                "hook": bool(r.get("hook")),
                "requiresPruning": bool(r.get("requiresPruning")),
            }
        )
    rows.sort(
        key=lambda x: (
            _HEALTH_RANK.get(x["health"], 5),  # Healthy, or no health: only OutOfSync listed it
            x["kind"] or "",
            x["namespace"] or "",
            x["name"] or "",
        )
    )
    return {
        "total": len(entries),
        "matched": len(rows),
        "unhealthyCount": unhealthy,
        "outOfSyncCount": out_of_sync,
        "items": cap(rows, max_items),
    }


# ------------------------------------------------------------ helm-source ----


def is_helm_source(source):
    """A source Argo CD renders with Helm: a `helm` block, or a chart from a Helm repository."""
    return isinstance(source.get("helm"), dict) or bool(source.get("chart"))


def helm_source_view(index, source):
    """One Helm source: chart, revision, release name, value file and parameter NAMES (never values)."""
    helm = source.get("helm") or {}
    base = source_view(index, source)
    base.pop("hasHelm")
    return {
        **base,
        "releaseName": helm.get("releaseName"),
        "version": helm.get("version"),
        "namespace": helm.get("namespace"),
        "valueFiles": nested(str(f) for f in helm.get("valueFiles") or []),
        "parameterNames": nested(
            str(p.get("name")) for p in helm.get("parameters") or [] if isinstance(p, dict)
        ),
        "fileParameterNames": nested(
            str(p.get("name")) for p in helm.get("fileParameters") or [] if isinstance(p, dict)
        ),
        "hasInlineValues": bool(helm.get("values") or helm.get("valuesObject")),
        "skipCrds": bool(helm.get("skipCrds")),
        "ignoreMissingValueFiles": bool(helm.get("ignoreMissingValueFiles")),
    }


def helm_view(app):
    """Which of the Application's sources are Helm, and what Argo CD reports about the source type."""
    sources = spec_sources(app)
    helm = [helm_source_view(i, s) for i, s in enumerate(sources) if is_helm_source(s)]
    types = _source_types(app)
    return {
        "isHelm": bool(helm) or "Helm" in types,
        "sourceCount": len(sources),
        "sourceTypes": nested(types),
        "sources": nested(helm),
    }


def _history_id(entry):
    try:
        return int(entry.get("id"))
    except (TypeError, ValueError):
        return -1


def last_deployed(app):
    """The most recent status.history entry (what was last deployed), or None with no history."""
    history = [h for h in _status(app).get("history") or [] if isinstance(h, dict)]
    if not history:
        return {"historyCount": 0, "entry": None}
    # History is appended in order; the highest id is the latest, the list order breaks ties.
    entry = max(enumerate(history), key=lambda ih: (_history_id(ih[1]), ih[0]))[1]
    sources = entry.get("sources") or ([entry["source"]] if entry.get("source") else [])
    initiated = entry.get("initiatedBy") or {}
    return {
        "historyCount": len(history),
        "entry": {
            "id": _history_id(entry) if _history_id(entry) >= 0 else None,
            "revision": entry.get("revision"),
            "revisions": nested(str(r) for r in entry.get("revisions") or []),
            "deployedAt": entry.get("deployedAt"),
            "deployStartedAt": entry.get("deployStartedAt"),
            "automated": bool(initiated.get("automated")),
            "sources": nested(_history_source(s) for s in sources if isinstance(s, dict)),
        },
    }
