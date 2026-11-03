"""Pure functions that turn Kubernetes objects into the pieces of an rw.discovery.v1 sync:
sanitised items, partitions, the open/commit bodies and batches. No kubectl, no network.

What leaves the cluster is decided here, by key name as well as by value text. A document is the
object minus `status` (split off), `managedFields`, `resourceVersion`, the last-applied style
annotations (at any depth, the rest cut at 512 bytes); a string under a key that names a
credential (password, token, apiKey, secretKey, bearer, authorization, session, cookie...) is
replaced by "[redacted]" unless the key is a reference (`secretName`, `tokenSecretRef`,
`passwordFile`); literal container `env` values are dropped (references stay: `valueFrom`);
container `command`/`args` and exec probe commands keep their first word and a count; http probe
header values are redacted; a ConfigMap keeps its key names and sizes plus one hash of the whole
data map; a CRD keeps names, group, scope, versions and a hash of the schema; HelmRelease values,
ArgoCD helm values and Kustomize patches are replaced by {sha256, sizeBytes}; every string is
scrubbed of credential shapes and cut at `max_string` bytes. Secrets are not synced at all (the
caller never lists them).
"""

import copy
import hashlib
import json
import re

from k8s_base.redact import redact_text

PLATFORM = "kubernetes"
BATCH_ITEMS = 200  # the contract allows 500 per batch; smaller bodies retry cheaply
BATCH_BYTES = 4 * 1024 * 1024  # serialized size of one batch's items
LAST_APPLIED = "kubectl.kubernetes.io/last-applied-configuration"
KAPP_ORIGINAL = "kapp.k14s.io/original"
DROPPED_ANNOTATIONS = (LAST_APPLIED, KAPP_ORIGINAL)
ANNOTATION_BYTES = 512
OMITTED = "[value not synced]"
REDACTED = "[redacted]"
CUT = "...[truncated]"
_DROPPED_METADATA = ("managedFields", "resourceVersion", "selfLink")
_SENSITIVE_KEY = re.compile(
    r"pass(word)?|pwd|token|api[-_]?key|secret(key)?|access[-_]?key|private[-_]?key"
    r"|credential|bearer|auth(orization)?|session|cookie",
    re.IGNORECASE,
)
# A key that points at a credential instead of holding one: camelCase (`secretName`,
# `tokenSecretRef`, `passwordFile`, `keyPath`) or separated (`auth-secret-ref`, `token_file`).
# `clientSecret` and `bearerToken` end in no such word and stay sensitive.
_REFERENCE_KEY = re.compile(
    r"(?:[a-z0-9](?:Refs?|Names?|File|Path)|[-_.](?:refs?|names?|file|path))$"
)
_CONTAINER_LISTS = ("containers", "initContainers", "ephemeralContainers")


def sensitive_key(key):
    """True when a string under `key` is replaced by "[redacted]": the key names a credential and
    is not a reference to one."""
    return (
        isinstance(key, str) and bool(_SENSITIVE_KEY.search(key)) and not _REFERENCE_KEY.search(key)
    )


def canonical_hash(value):
    """{sha256, sizeBytes} of the canonical JSON of `value`: change detection without content."""
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )
    return {"sha256": hashlib.sha256(raw).hexdigest(), "sizeBytes": len(raw)}


def cut(text, limit):
    """text cut to at most `limit` UTF-8 bytes (plus the marker when cut)."""
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    return raw[:limit].decode("utf-8", errors="ignore") + CUT


def _argv(value, limit):
    """(first word, count) of a command or args list."""
    return ([_walk(str(value[0]), limit)] if value else []), len(value)


def _annotations(node, limit):
    kept = {k: v for k, v in node.items() if k not in DROPPED_ANNOTATIONS}
    out = {}
    for key, value in kept.items():
        if isinstance(value, str) and sensitive_key(key):
            out[key] = REDACTED
        elif isinstance(value, str):
            out[key] = cut(redact_text(value), min(limit, ANNOTATION_BYTES - len(CUT)))
        else:
            out[key] = _walk(value, limit, key)
    return out


def _walk(node, limit, key=None):
    if isinstance(node, str):
        if node == OMITTED:
            return node
        return REDACTED if sensitive_key(key) else cut(redact_text(node), limit)
    if isinstance(node, list):
        if key in _CONTAINER_LISTS:
            return [
                _walk_dict(v, limit, container=True) if isinstance(v, dict) else v for v in node
            ]
        if key == "httpHeaders":
            return [
                {k: REDACTED if k == "value" else _walk(v, limit, k) for k, v in h.items()}
                if isinstance(h, dict)
                else _walk(h, limit, key)
                for h in node
            ]
        return [_walk(v, limit, key) for v in node]
    if not isinstance(node, dict):
        return node
    if key == "annotations":
        return _annotations(node, limit)
    return _walk_dict(node, limit, container=False)


def _walk_dict(node, limit, container):
    out = {}
    for name, value in node.items():
        if container and name in ("command", "args") and isinstance(value, list):
            first, count = _argv(value, limit)
            out[name] = first
            out[f"{name}Count"] = count
        elif name == "env" and isinstance(value, list):
            # a literal value may be a credential; a reference (valueFrom) is an edge, keep it
            out[name] = [
                _walk({k: v for k, v in e.items() if k != "value"}, limit)
                if isinstance(e, dict)
                else e
                for e in value
            ]
        elif name == "env" and isinstance(value, dict):
            out[name] = {
                k: _walk(v, limit, k) if isinstance(v, dict) else OMITTED for k, v in value.items()
            }
        elif name == "exec" and isinstance(value, dict) and isinstance(value.get("command"), list):
            kept = {k: _walk(v, limit, k) for k, v in value.items() if k != "command"}
            first, count = _argv(value["command"], limit)
            out[name] = {**kept, "command": first, "argCount": count}
        elif name == "annotations" and isinstance(value, dict):
            kept = _walk(value, limit, name)
            if kept or not value:  # an object whose only annotations were dropped loses the key
                out[name] = kept
        else:
            out[name] = _walk(value, limit, name)
    return out


def _config_map(obj):
    """Key names and sizes, plus ONE hash over the whole data map (change detection)."""
    data = obj.get("data")
    binary = obj.get("binaryData")
    whole = {}
    if isinstance(data, dict):
        whole["data"] = data
        obj["dataSizes"] = {
            k: len((v if isinstance(v, str) else str(v)).encode("utf-8"))
            for k, v in sorted(data.items())
        }
        obj["data"] = {k: OMITTED for k in data}
    obj.pop("binaryData", None)
    if isinstance(binary, dict):
        whole["binaryData"] = binary
        obj["binaryDataSizes"] = {k: len(str(v)) * 3 // 4 for k, v in sorted(binary.items())}
    if whole:
        obj["dataHash"] = canonical_hash(whole)["sha256"]


def _crd(obj):
    """names, group, scope, versions (name, served, storage) and a sha256 of the schemas: the
    openAPIV3Schema itself is never synced (hundreds of KB per CRD)."""
    spec = obj.get("spec")
    if not isinstance(spec, dict):
        return
    versions = [v for v in spec.get("versions") or [] if isinstance(v, dict)]
    schemas = sorted(
        ((v.get("name", ""), v.get("schema")) for v in versions), key=lambda pair: str(pair[0])
    )
    obj["spec"] = {
        "group": spec.get("group"),
        "scope": spec.get("scope"),
        "names": spec.get("names"),
        "versions": [
            {"name": v.get("name"), "served": v.get("served"), "storage": v.get("storage")}
            for v in versions
        ],
        "schemaSha256": canonical_hash({"versions": schemas, "validation": spec.get("validation")})[
            "sha256"
        ],
    }


def _hash_keys(node, *keys):
    if isinstance(node, dict):
        for key in keys:
            if key in node:
                node[key] = canonical_hash(node[key])


def _hash_inline_values(obj, kind):
    """Values and patches are free-form and routinely hold credentials: size and hash only."""
    spec = obj.get("spec")
    if not isinstance(spec, dict):
        return
    if kind == "HelmRelease":
        _hash_keys(spec, "values", "valuesObject")
        for entry in spec.get("valuesFrom") or []:
            if isinstance(entry, dict):
                for extra in [k for k in entry if k not in _VALUES_FROM_REFS]:
                    entry[extra] = canonical_hash(entry[extra])
    elif kind == "Application":
        sources = list(spec.get("sources") or [])
        if isinstance(spec.get("source"), dict):
            sources.append(spec["source"])
        for source in sources:
            helm = source.get("helm") if isinstance(source, dict) else None
            if isinstance(helm, dict):
                _hash_keys(helm, "values", "valuesObject")
                for param in helm.get("parameters") or []:
                    _hash_keys(param, "value")
    elif kind == "Kustomization":
        _hash_keys(spec, "patches", "patchesStrategicMerge", "patchesJson6902")
        _hash_keys(spec.get("postBuild"), "substitute")


_VALUES_FROM_REFS = ("kind", "name", "valuesKey", "targetPath", "optional")


def sanitize(obj, kind, max_string):
    """(document, status) of a raw object. `status` is {} when the object has none."""
    obj = copy.deepcopy(obj)
    status = obj.pop("status", None)
    meta = obj.get("metadata") or {}
    for key in _DROPPED_METADATA:
        meta.pop(key, None)
    if kind == "ConfigMap":
        _config_map(obj)
    elif kind == "CustomResourceDefinition":
        _crd(obj)
    else:
        _hash_inline_values(obj, kind)
    document = _walk(obj, max_string)
    status = _walk(status, max_string) if isinstance(status, dict) else {}
    return document, status


def build_item(chain, obj, kind, max_string):
    """One sync item for `obj` at `chain` ([(type, name), ...], the root first)."""
    document, status = sanitize(obj, kind, max_string)
    meta = document.get("metadata") or {}
    labels = {k: v for k, v in (meta.get("labels") or {}).items() if isinstance(v, str)}
    item = {
        "identity": {"platform": PLATFORM, "chain": [{"type": t, "name": n} for t, n in chain]},
        "labels": labels,
        "document": document,
    }
    uid = (obj.get("metadata") or {}).get("uid")
    if uid:
        item["providerUid"] = str(uid)[:255]
    name = (obj.get("metadata") or {}).get("name")
    if name:
        item["displayName"] = str(name)[:255]
    if status:
        item["status"] = status
    return item


def owned_by_cronjob(obj):
    """A Job a CronJob spawned is recreated every run, not a resource of its own."""
    refs = (obj.get("metadata") or {}).get("ownerReferences") or []
    return any(isinstance(r, dict) and r.get("kind") == "CronJob" for r in refs)


def partition(type_name, status, parent_path=None, count=None, reason=None):
    entry = {"type": type_name, "status": status}
    if parent_path is not None:
        entry["parentPath"] = parent_path
    if status == "complete" and count is not None:
        entry["count"] = count
    if reason:
        entry["reason"] = reason[:4096]
    return entry


def batches(items, size=BATCH_ITEMS, max_bytes=BATCH_BYTES):
    """Items in order, in batches of at most `size` items and `max_bytes` serialized bytes (a
    single item larger than that travels alone)."""
    out = []
    current = []
    used = 0
    for item in items:
        cost = len(json.dumps(item, separators=(",", ":")).encode("utf-8")) + 1
        if current and (len(current) >= size or used + cost > max_bytes):
            out.append({"seq": len(out) + 1, "items": current})
            current, used = [], 0
        current.append(item)
        used += cost
    if current:
        out.append({"seq": len(out) + 1, "items": current})
    return out


def summarize(counts, partitions, errors, duration_ms, root_key=None):
    """The rw.discovery.summary.v1 value, also the commit's `summary`."""
    totals = {"complete": 0, "failed": 0, "forbidden": 0, "excluded": 0}
    for p in partitions:
        totals[p["status"]] += 1
    summary = {
        "counts": {k: v for k, v in sorted(counts.items()) if v},
        "partitions": totals,
        "durationMs": duration_ms,
    }
    if errors:
        summary["errors"] = errors
    if root_key:
        summary["root"] = {"canonicalKey": {"name": "kubeSystemUid", "value": root_key}}
    return summary


def document(scope_path, items, partitions, summary, task_run=None):
    """The whole recorded sync: open + item batches + commit (rw.discovery.v1)."""
    opened = {"source": "k8s-base/discover", "scopePath": scope_path, "mode": "full"}
    if task_run:
        opened["taskRun"] = task_run
    return {
        "apiVersion": "rw.discovery.v1",
        "open": opened,
        "batches": batches(items),
        "commit": {"partitions": partitions, "summary": summary},
    }
