"""Narrow kubectl reads: only the fields a task uses leave the cluster.

`kubectl get -o json` returns a whole object, and a workload's or pod's spec carries its container
`env` (and `last-applied-configuration` repeats it), so a recording of that call would hold env
values. A `-o jsonpath` template names exactly the fields wanted instead: one line per field, so
the raw output (what a fixture records) never contains an env entry. `Projection.parse` turns the
lines back into a document shaped like the object, so the pure functions read it unchanged.

Line format, one object after a `@` line: `s:<path>=<string>` (a string field, unquoted),
`j:<path>=<JSON>` (an object, list, number or bool) and, for a pod, `k:<container>=<JSON ports>`
(a container's name and declared ports, nothing else of it). A field the object lacks is an empty
value and is left out of the document.
"""

import json

from k8s_jenkins_health.common import MESSAGE_LIMIT
from k8s_jenkins_health.errors import RwError
from k8s_jenkins_health.k8s import run_checked


class Projection:
    def __init__(self, strings=(), values=(), containers=False):
        self.strings = tuple(strings)
        self.values = tuple(values)
        self.containers = containers

    def template(self, many):
        parts = ['{"@\\n"}']
        for path in self.strings:
            parts.append(f'{{"s:{path}="}}{{.{path}}}{{"\\n"}}')
        for path in self.values:
            parts.append(f'{{"j:{path}="}}{{.{path}}}{{"\\n"}}')
        if self.containers:
            parts.append('{range .spec.containers[*]}{"k:"}{.name}{"="}{.ports}{"\\n"}{end}')
        body = "".join(parts)
        return "{range .items[*]}" + body + "{end}" if many else body

    def parse(self, text, many):
        """The document (one object, or {"items": [...]} when many) from the template's output."""
        objects, current = [], None
        for line in text.split("\n"):
            if line == "@":
                current = {}
                objects.append(current)
            elif not line:
                continue
            elif current is None or line[:2] not in ("s:", "j:", "k:"):
                raise RwError(
                    "internal", "kubectl returned output that is not the projection asked"
                )
            elif line[0] == "k":
                name, _, raw = line[2:].partition("=")
                container = {"name": name}
                if raw:
                    container["ports"] = _loads(raw, f"the ports of container {name}")
                current.setdefault("spec", {}).setdefault("containers", []).append(container)
            else:
                path, _, raw = line[2:].partition("=")
                if raw == "":
                    continue
                value = _loads(raw, path) if line[0] == "j" else raw
                _set(current, path.split("."), value)
        if many:
            return {"items": objects}
        if len(objects) != 1:
            raise RwError("internal", "kubectl returned no object")
        return objects[0]


def _loads(raw, what):
    try:
        return json.loads(raw)
    except ValueError:
        raise RwError("internal", f"kubectl returned a bad value for {what}") from None


def _set(doc, keys, value):
    for key in keys[:-1]:
        doc = doc.setdefault(key, {})
    doc[keys[-1]] = value


# What the tasks read of each kind. No env, no annotations, no command line, no volumes.
WORKLOAD_FIELDS = Projection(
    strings=("kind", "metadata.name", "metadata.namespace"),
    values=(
        "spec.replicas",
        "spec.selector",
        "spec.template.metadata.labels",
        "status.readyReplicas",
    ),
)
POD_FIELDS = Projection(
    strings=("metadata.name", "metadata.deletionTimestamp", "status.phase"),
    values=("metadata.labels", "status.conditions"),
    containers=True,
)


def kubectl_projected(ctx, projection, *args, many=False):
    """Run `kubectl <args> -o jsonpath=<projection>` and return the parsed document. A failed call
    raises RwError with its D64 code, as kubectl_json does."""
    result = run_checked(ctx, ["kubectl", *args, "-o", f"jsonpath={projection.template(many)}"])
    return projection.parse(result.stdout, many)


def kubectl_json_scoped_projected(ctx, scope, projection, *args, many=False, hint=None):
    """A projected read the rest of the result can do without: the document, or None when it is
    forbidden (reported with `ctx.error` as permission_denied for `scope`). Every other failure
    (not found, unreachable, timeout, bad output) still raises."""
    try:
        return kubectl_projected(ctx, projection, *args, many=many)
    except RwError as exc:
        if exc.code != "permission_denied":
            raise
        ctx.error(
            "permission_denied",
            f"not allowed to read {scope}: {exc.message}"[:MESSAGE_LIMIT],
            scope=scope,
            **({"hint": hint} if hint else {}),
        )
        return None
