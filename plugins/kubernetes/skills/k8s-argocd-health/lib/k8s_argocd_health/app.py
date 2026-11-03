"""Pure functions behind the k8s-argocd-health tasks.

Everything here takes the parsed Argo CD Application (argoproj.io/v1alpha1) and
returns plain facts: no kubectl, no clock, no environment. Helm values are never
copied: only the names of value files and parameters, and whether inline values
exist.
"""

import re

NESTED_LIMIT = 50  # most entries in a list nested inside an output entry
TEXT_LIMIT = 1024  # most characters in a free-text field or identifier string
HARD_LIMIT = 1000  # ceiling for the maxItems setting, so output schemas can bound every list

# Sort order of the unhealthy-resources list: worst first.
_HEALTH_RANK = {"Degraded": 0, "Missing": 1, "Unknown": 2, "Progressing": 3, "Suspended": 4}

# user:token@ in a URL, wherever it shows up (also inside free text).
_USERINFO = re.compile(r"(://)[^/\s@]*@")


def cap(items, max_items):
    """Bound a list output to max_items entries (never more than HARD_LIMIT)."""
    return list(items)[: min(int(max_items), HARD_LIMIT)]


def nested(items):
    """Bound a list nested inside an output entry to NESTED_LIMIT entries."""
    return list(items)[:NESTED_LIMIT]


def ident(value):
    """An identifier-like value as a string cut to TEXT_LIMIT characters; None stays None."""
    return None if value is None else str(value)[:TEXT_LIMIT]


def trunc(text):
    """(text with URL credentials removed and cut to TEXT_LIMIT characters, whether it was cut)."""
    if text is None:
        return None, False
    text = _USERINFO.sub(r"\1", str(text))
    return (text[:TEXT_LIMIT], True) if len(text) > TEXT_LIMIT else (text, False)


def clean_url(url):
    """A repository URL without credentials, query or fragment, cut to TEXT_LIMIT characters.

    Works on the text, not on a parsed URL: a malformed URL (which a parser
    refuses) must not leak its user:token@ part either.
    """
    if not url:
        return None
    text = str(url)
    if "://" in text:
        text = re.split(r"[?#]", _USERINFO.sub(r"\1", text), maxsplit=1)[0]
    return text[:TEXT_LIMIT]


def _status(app):
    return app.get("status") or {}


def _spec(app):
    return app.get("spec") or {}


def _listing(rows, max_items):
    """{total, truncated, items}: rows cut to max_items, with the count before the cut."""
    rows = list(rows)
    items = cap(rows, max_items)
    return {"total": len(rows), "truncated": len(items) < len(rows), "items": items}


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


def _source_types(app):
    status = _status(app)
    types = status.get("sourceTypes")
    if not types and status.get("sourceType"):
        types = [status["sourceType"]]
    return [str(t) for t in types or []]


def _source_type_at(app, index, count):
    """What Argo CD detected for source `index`: status.sourceTypes is index-aligned
    with the sources; when it is not (different length), the type is unknown, not guessed."""
    types = _source_types(app)
    return ident(types[index]) if count and len(types) == count else None


def source_view(index, source, source_type=None):
    """Where one source comes from: repository, path or chart, revision to track."""
    return {
        "index": index,
        "repoURL": clean_url(source.get("repoURL")),
        "path": ident(source.get("path")),
        "chart": ident(source.get("chart")),
        "targetRevision": ident(source.get("targetRevision")),
        "ref": ident(source.get("ref")),
        "hasHelm": isinstance(source.get("helm"), dict),
        "sourceType": source_type,
    }


def source_views(app, max_items):
    sources = spec_sources(app)
    return _listing(
        (source_view(i, s, _source_type_at(app, i, len(sources))) for i, s in enumerate(sources)),
        min(int(max_items), NESTED_LIMIT),
    )


def _history_source(source):
    """A history entry's source reduced to where it came from (its helm block carries values)."""
    return {
        "repoURL": clean_url(source.get("repoURL")),
        "path": ident(source.get("path")),
        "chart": ident(source.get("chart")),
        "targetRevision": ident(source.get("targetRevision")),
    }


# -------------------------------------------------------- sync-status ----


def application_view(app):
    """Identity, destination and sync policy of the Application."""
    md, spec, status = app.get("metadata") or {}, _spec(app), _status(app)
    dest = spec.get("destination") or {}
    policy = spec.get("syncPolicy") or {}
    automated = policy.get("automated")
    # `automated: {}` is enabled with the defaults; `enabled: false` switches it off.
    auto = isinstance(automated, dict) and automated.get("enabled") is not False
    retry = policy.get("retry") or {}
    return {
        "name": ident(md.get("name")),
        "namespace": ident(md.get("namespace")),
        "project": ident(spec.get("project")),
        "destination": {
            "server": clean_url(dest.get("server")),
            "name": ident(dest.get("name")),
            "namespace": ident(dest.get("namespace")),
        },
        "deletionTimestamp": md.get("deletionTimestamp"),
        "operationInProgress": app.get("operation") is not None,
        "resourceCount": len(status.get("resources") or []),
        "sourceTypes": nested(ident(t) for t in _source_types(app)),
        "syncPolicy": {
            "automated": auto,
            "prune": bool(automated.get("prune")) if auto else None,
            "selfHeal": bool(automated.get("selfHeal")) if auto else None,
            "allowEmpty": bool(automated.get("allowEmpty")) if auto else None,
            "syncOptions": nested(ident(o) for o in policy.get("syncOptions") or []),
            "retryLimit": retry.get("limit"),
        },
    }


def sync_view(app):
    sync = _status(app).get("sync") or {}
    return {
        "status": ident(sync.get("status")),
        "revision": ident(sync.get("revision")),
        "revisions": nested(ident(r) for r in sync.get("revisions") or []),
        "reconciledAt": _status(app).get("reconciledAt"),
    }


def health_view(app):
    health = _status(app).get("health") or {}
    message, cut = trunc(health.get("message"))
    return {
        "status": ident(health.get("status")),
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
                "type": ident(c.get("type")),
                "message": message,
                "truncated": cut,
                "lastTransitionTime": c.get("lastTransitionTime"),
            }
        )
    return _listing(rows, max_items)


def _sync_failure(r):
    message, cut = trunc(r.get("message"))
    return {
        "group": ident(r.get("group")) or "",
        "kind": ident(r.get("kind")),
        "namespace": ident(r.get("namespace")),
        "name": ident(r.get("name")),
        "status": ident(r.get("status")),
        "hookPhase": ident(r.get("hookPhase")),
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
        "phase": ident(op.get("phase")),
        "message": message,
        "truncated": cut,
        "startedAt": op.get("startedAt"),
        "finishedAt": op.get("finishedAt"),
        "retryCount": op.get("retryCount") or 0,
        "revision": ident(result.get("revision")),
        "revisions": nested(ident(r) for r in result.get("revisions") or []),
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
                "group": ident(r.get("group")) or "",
                "version": ident(r.get("version")),
                "kind": ident(r.get("kind")),
                "namespace": ident(r.get("namespace")),
                "name": ident(r.get("name")),
                "syncStatus": ident(r.get("status")),
                "health": ident(h_status),
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
    items = cap(rows, max_items)
    return {
        "total": len(entries),
        "matched": len(rows),
        "truncated": len(items) < len(rows),
        "unhealthyCount": unhealthy,
        "outOfSyncCount": out_of_sync,
        "items": items,
    }


# ------------------------------------------------------------ helm-source ----


def is_helm_source(source, source_type=None):
    """A source Argo CD renders with Helm: a `helm` block, a chart from a Helm repository,
    or a path Argo CD detected as Helm (status.sourceTypes)."""
    return (
        isinstance(source.get("helm"), dict) or bool(source.get("chart")) or source_type == "Helm"
    )


def helm_source_view(index, source, source_type=None):
    """One Helm source: chart, revision, release name, value file and parameter NAMES (never values)."""
    helm = source.get("helm") or {}
    base = source_view(index, source, source_type)
    base.pop("hasHelm")
    value_files = [clean_url(f) for f in helm.get("valueFiles") or []]
    params = [p for p in helm.get("parameters") or [] if isinstance(p, dict)]
    file_params = [p for p in helm.get("fileParameters") or [] if isinstance(p, dict)]
    return {
        **base,
        "releaseName": ident(helm.get("releaseName")),
        "version": ident(helm.get("version")),
        "namespace": ident(helm.get("namespace")),
        "valueFiles": nested(value_files),
        "valueFileCount": len(value_files),
        "parameterNames": nested(ident(p.get("name")) for p in params),
        "parameterCount": len(params),
        "fileParameterNames": nested(ident(p.get("name")) for p in file_params),
        "hasInlineValues": bool(helm.get("values") or helm.get("valuesObject")),
        "skipCrds": bool(helm.get("skipCrds")),
        "ignoreMissingValueFiles": bool(helm.get("ignoreMissingValueFiles")),
    }


def helm_view(app):
    """Which of the Application's sources are Helm, and what Argo CD reports about the source type."""
    sources = spec_sources(app)
    types = [_source_type_at(app, i, len(sources)) for i in range(len(sources))]
    helm = [
        helm_source_view(i, s, types[i])
        for i, s in enumerate(sources)
        if is_helm_source(s, types[i])
    ]
    all_types = _source_types(app)
    return {
        "isHelm": bool(helm) or "Helm" in all_types,
        "sourceCount": len(sources),
        "sourceTypes": nested(ident(t) for t in all_types),
        "sources": nested(helm),
    }


def _history_id(entry):
    try:
        return int(entry.get("id"))
    except (TypeError, ValueError):
        return -1


def last_deployed(app):
    """The most recent status.history entry (what was last deployed), or None with no history.

    `historyCount` is the number of entries kept, not of syncs ever done:
    Argo CD keeps only spec.revisionHistoryLimit (default 10).
    """
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
            "revision": ident(entry.get("revision")),
            "revisions": nested(ident(r) for r in entry.get("revisions") or []),
            "deployedAt": entry.get("deployedAt"),
            "deployStartedAt": entry.get("deployStartedAt"),
            "automated": bool(initiated.get("automated")),
            "sources": nested(_history_source(s) for s in sources if isinstance(s, dict)),
        },
    }
