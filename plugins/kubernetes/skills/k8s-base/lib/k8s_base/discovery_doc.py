"""Pure functions that turn Kubernetes objects into the pieces of an rw.discovery.v1 sync:
sanitised items, partitions, the open/commit bodies and batches. No kubectl, no network.

What leaves the cluster is decided here. A document is the object minus `status` (split off),
`managedFields`, `resourceVersion` and the last-applied annotation; literal container `env`
values are dropped (references stay: `valueFrom`); exec probe commands keep their first word and
an argument count; a ConfigMap keeps its key names, sizes and hashes but not its values; every
string is scrubbed of credential shapes and cut at `max_string` bytes. Secrets are not synced at
all (the caller never lists them).
"""

import copy
import hashlib
import re

from k8s_base.redact import redact_text

PLATFORM = "kubernetes"
BATCH_ITEMS = 200  # the contract allows 500 per batch; smaller bodies retry cheaply
LAST_APPLIED = "kubectl.kubernetes.io/last-applied-configuration"
OMITTED = "[value not synced]"
CUT = "...[truncated]"
STUB_ANNOTATION = "k8s-base.runwhen.com/stub"
_DROPPED_METADATA = ("managedFields", "resourceVersion", "selfLink")
_SENSITIVE_KEY = re.compile(r"pass|pwd|secret|token|credential|key|cert", re.IGNORECASE)


def cut(text, limit):
    """text cut to at most `limit` UTF-8 bytes (plus the marker when cut)."""
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    return raw[:limit].decode("utf-8", errors="ignore") + CUT


def _walk(node, limit):
    if isinstance(node, str):
        return cut(redact_text(node), limit)
    if isinstance(node, list):
        return [_walk(v, limit) for v in node]
    if not isinstance(node, dict):
        return node
    out = {}
    for key, value in node.items():
        if key == "env" and isinstance(value, list):
            # a literal value may be a credential; a reference (valueFrom) is an edge, keep it
            out[key] = [
                _walk({k: v for k, v in e.items() if k != "value"}, limit)
                if isinstance(e, dict)
                else e
                for e in value
            ]
        elif key == "exec" and isinstance(value, dict) and isinstance(value.get("command"), list):
            argv = value["command"]
            kept = {k: _walk(v, limit) for k, v in value.items() if k != "command"}
            out[key] = {
                **kept,
                "command": [_walk(str(argv[0]), limit)] if argv else [],
                "argCount": len(argv),
            }
        else:
            out[key] = _walk(value, limit)
    return out


def _hashes(values):
    """{key: {size, sha256}} of the decoded values; a key that names a credential gets size only."""
    out = {}
    for key, value in sorted(values.items()):
        text = value if isinstance(value, str) else str(value)
        entry = {"size": len(text.encode("utf-8"))}
        if not _SENSITIVE_KEY.search(key):
            entry["sha256"] = hashlib.sha256(text.encode("utf-8")).hexdigest()
        out[key] = entry
    return out


def sanitize(obj, kind, max_string):
    """(document, status) of a raw object. `status` is {} when the object has none."""
    obj = copy.deepcopy(obj)
    status = obj.pop("status", None)
    meta = obj.get("metadata") or {}
    for key in _DROPPED_METADATA:
        meta.pop(key, None)
    annotations = meta.get("annotations")
    if isinstance(annotations, dict):
        annotations.pop(LAST_APPLIED, None)
        if not annotations:
            del meta["annotations"]
    if kind == "ConfigMap":
        data = obj.get("data")
        if isinstance(data, dict):
            obj["dataHashes"] = _hashes(data)
            obj["data"] = {k: OMITTED for k in data}
        binary = obj.pop("binaryData", None)
        if isinstance(binary, dict):
            obj["binaryDataHashes"] = {
                k: {"size": len(str(v)) * 3 // 4} for k, v in sorted(binary.items())
            }
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


def stub_namespace(name):
    """A namespace the identity may not read but was asked to cover: its children need a parent."""
    return {
        "kind": "Namespace",
        "apiVersion": "v1",
        "metadata": {"name": name, "annotations": {STUB_ANNOTATION: "true"}},
    }


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


def batches(items, size=BATCH_ITEMS):
    return [
        {"seq": i // size + 1, "items": items[i : i + size]} for i in range(0, len(items), size)
    ]


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
