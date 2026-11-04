"""Narrow kubectl reads: only the fields a task uses leave the cluster.

`kubectl get -o json` returns a whole object, and a workload's or pod's spec carries its container
`env` (and `last-applied-configuration` repeats it), so a recording of that call would hold env
values. A `-o jsonpath` template names exactly the fields wanted instead: one line per field, so
the raw output (what a fixture records) never contains an env entry. `Projection.parse` turns the
lines back into a document shaped like the object, so the pure functions read it unchanged.

Line format, one object after a `@` line: `s:<path>=<string>` (a string field, unquoted),
`j:<path>=<JSON>` (an object, list, number or bool) and, for a pod, `c:<name>=<image>` (a
container, in spec order), `p:<name>=<port>` (a container port of the container above it, name
empty when unnamed) and `r:<container>=<restarts>|<startedAt>` (a container status). A field the
object lacks is an empty value and is left out of the document. Env, args and commands are not
among the fields.
"""

import json

from k8s_jaeger_health.errors import RwError
from k8s_jaeger_health.k8s import run_checked

CONTAINERS = (
    '{range .spec.containers[*]}{"c:"}{.name}{"="}{.image}{"\\n"}'
    '{range .ports[*]}{"p:"}{.name}{"="}{.containerPort}{"\\n"}{end}{end}'
)
RESTARTS = (
    '{range .status.containerStatuses[*]}{"r:"}{.name}{"="}{.restartCount}{"|"}'
    '{.state.running.startedAt}{"\\n"}{end}'
)


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
            parts.append(CONTAINERS)
            parts.append(RESTARTS)
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
            elif current is None or line[:2] not in ("s:", "j:", "c:", "p:", "r:"):
                raise RwError(
                    "internal", "kubectl returned output that is not the projection asked"
                )
            elif line[0] == "c":
                name, _, image = line[2:].partition("=")
                current.setdefault("spec", {}).setdefault("containers", []).append(
                    {"name": name, **({"image": image} if image else {})}
                )
            elif line[0] == "p":
                name, _, number = line[2:].partition("=")
                if not number.isdigit():
                    raise RwError("internal", "kubectl returned a bad container port")
                entry = {"containerPort": int(number), **({"name": name} if name else {})}
                containers = current.setdefault("spec", {}).setdefault("containers", [])
                if not containers:
                    containers.append({})
                containers[-1].setdefault("ports", []).append(entry)
            elif line[0] == "r":
                name, _, rest = line[2:].partition("=")
                count, _, started = rest.partition("|")
                if not count.isdigit():
                    raise RwError("internal", "kubectl returned a bad restart count")
                status = {"name": name, "restartCount": int(count)}
                if started:
                    status["state"] = {"running": {"startedAt": started}}
                current.setdefault("status", {}).setdefault("containerStatuses", []).append(status)
            else:
                path, _, raw = line[2:].partition("=")
                if raw == "":
                    continue
                value = raw
                if line[0] == "j":
                    try:
                        value = json.loads(raw)
                    except ValueError:
                        raise RwError(
                            "internal", f"kubectl returned a bad value for {path}"
                        ) from None
                _set(current, path.split("."), value)
        if many:
            return {"items": objects}
        if len(objects) != 1:
            raise RwError("internal", "kubectl returned no object")
        return objects[0]


def _set(doc, keys, value):
    for key in keys[:-1]:
        doc = doc.setdefault(key, {})
    doc[keys[-1]] = value


# What the tasks read of each kind. No env, no annotations, no command line, no volumes.
WORKLOAD_FIELDS = Projection(
    strings=("kind", "metadata.name", "metadata.namespace"),
    values=("spec.replicas", "spec.selector", "status.readyReplicas"),
)
POD_FIELDS = Projection(
    strings=("metadata.name", "metadata.deletionTimestamp", "status.phase", "status.startTime"),
    values=("status.conditions",),
    containers=True,
)


def kubectl_projected(ctx, projection, *args, many=False):
    """Run `kubectl <args> -o jsonpath=<projection>` and return the parsed document. A failed call
    raises RwError with its D64 code, as kubectl_json does."""
    result = run_checked(ctx, ["kubectl", *args, "-o", f"jsonpath={projection.template(many)}"])
    return projection.parse(result.stdout, many)
