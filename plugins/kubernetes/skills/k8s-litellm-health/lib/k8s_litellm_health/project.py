"""Narrow kubectl reads: only the fields a task uses leave the cluster.

`kubectl get -o json` returns a whole object, and a workload's or pod's spec carries its container
`env` (and `last-applied-configuration` repeats it), so a recording of that call would hold env
values. A `-o jsonpath` template names exactly the fields wanted instead: one line per field, so
the raw output (what a fixture records) never contains an env entry. `Projection.parse` turns the
lines back into a document shaped like the object, so the pure functions read it unchanged.

Line format, one object after a `@` line: `s:<path>=<string>` (a string field, unquoted),
`j:<path>=<JSON>` (an object, list, number or bool) and, for a pod, `p:<name>=<port>` (one container
port of a pod or of a workload's pod template, name empty when unnamed). A field the object lacks is an empty value and is left out of the
document.
"""

import json

from k8s_litellm_health.errors import RwError
from k8s_litellm_health.k8s import run_checked

POD_PORTS = ("spec", "containers")
TEMPLATE_PORTS = ("spec", "template", "spec", "containers")


class Projection:
    def __init__(self, strings=(), values=(), ports=None):
        """`ports` is the path to the containers list whose container ports are read (POD_PORTS or
        TEMPLATE_PORTS), or None."""
        self.strings = tuple(strings)
        self.values = tuple(values)
        self.ports = ports

    def template(self, many):
        parts = ['{"@\\n"}']
        for path in self.strings:
            parts.append(f'{{"s:{path}="}}{{.{path}}}{{"\\n"}}')
        for path in self.values:
            parts.append(f'{{"j:{path}="}}{{.{path}}}{{"\\n"}}')
        if self.ports:
            where = ".".join(self.ports)
            parts.append(
                f'{{range .{where}[*].ports[*]}}{{"p:"}}{{.name}}{{"="}}{{.containerPort}}{{"\\n"}}{{end}}'
            )
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
            elif current is None or line[:2] not in ("s:", "j:", "p:"):
                raise RwError(
                    "internal", "kubectl returned output that is not the projection asked"
                )
            elif line[0] == "p":
                name, _, number = line[2:].partition("=")
                if not number.isdigit():
                    raise RwError("internal", "kubectl returned a bad container port")
                entry = {"containerPort": int(number), **({"name": name} if name else {})}
                holder = current
                for key in self.ports[:-1]:
                    holder = holder.setdefault(key, {})
                container = holder.setdefault(self.ports[-1], [{}])[0]
                container.setdefault("ports", []).append(entry)
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
    values=(
        "spec.replicas",
        "spec.selector",
        "spec.template.metadata.labels",
        "status.readyReplicas",
    ),
    ports=TEMPLATE_PORTS,
)
POD_FIELDS = Projection(
    strings=("metadata.name", "metadata.deletionTimestamp", "status.phase"),
    values=("status.conditions",),
    ports=POD_PORTS,
)


def kubectl_projected(ctx, projection, *args, many=False):
    """Run `kubectl <args> -o jsonpath=<projection>` and return the parsed document. A failed call
    raises RwError with its D64 code, as kubectl_json does."""
    result = run_checked(ctx, ["kubectl", *args, "-o", f"jsonpath={projection.template(many)}"])
    return projection.parse(result.stdout, many)
