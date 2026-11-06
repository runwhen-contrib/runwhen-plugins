"""Resource names for `deniedResources`: a static table of the built-in kinds and a
fail-closed lookup for everything else. Pure functions; the lookup is a callable the
script supplies (it runs `kubectl api-resources -o wide`).

A denied entry names one resource under any spelling: plural, singular, kind or short
name (`configmaps`, `configmap`, `cm`). The table lets the common names resolve without
a cluster call; a name outside it (a CRD) is resolved once, and an entry that cannot be
resolved fails the command closed rather than denying less than the operator wrote.
"""

from k8s_base.errors import RwError

# (plural, singular = lower-cased kind, short names) of the built-in API resources.
BUILTIN = [
    ("bindings", "binding", ()),
    ("componentstatuses", "componentstatus", ("cs",)),
    ("configmaps", "configmap", ("cm",)),
    ("endpoints", "endpoint", ("ep",)),
    ("events", "event", ("ev",)),
    ("limitranges", "limitrange", ("limits",)),
    ("namespaces", "namespace", ("ns",)),
    ("nodes", "node", ("no",)),
    ("persistentvolumeclaims", "persistentvolumeclaim", ("pvc",)),
    ("persistentvolumes", "persistentvolume", ("pv",)),
    ("pods", "pod", ("po",)),
    ("podtemplates", "podtemplate", ()),
    ("replicationcontrollers", "replicationcontroller", ("rc",)),
    ("resourcequotas", "resourcequota", ("quota",)),
    ("secrets", "secret", ()),
    ("serviceaccounts", "serviceaccount", ("sa",)),
    ("services", "service", ("svc",)),
    ("mutatingwebhookconfigurations", "mutatingwebhookconfiguration", ()),
    ("validatingwebhookconfigurations", "validatingwebhookconfiguration", ()),
    ("validatingadmissionpolicies", "validatingadmissionpolicy", ()),
    ("validatingadmissionpolicybindings", "validatingadmissionpolicybinding", ()),
    ("customresourcedefinitions", "customresourcedefinition", ("crd", "crds")),
    ("apiservices", "apiservice", ()),
    ("controllerrevisions", "controllerrevision", ()),
    ("daemonsets", "daemonset", ("ds",)),
    ("deployments", "deployment", ("deploy",)),
    ("replicasets", "replicaset", ("rs",)),
    ("statefulsets", "statefulset", ("sts",)),
    ("horizontalpodautoscalers", "horizontalpodautoscaler", ("hpa",)),
    ("cronjobs", "cronjob", ("cj",)),
    ("jobs", "job", ()),
    ("certificatesigningrequests", "certificatesigningrequest", ("csr",)),
    ("leases", "lease", ()),
    ("endpointslices", "endpointslice", ()),
    ("flowschemas", "flowschema", ()),
    ("prioritylevelconfigurations", "prioritylevelconfiguration", ()),
    ("ingressclasses", "ingressclass", ()),
    ("ingresses", "ingress", ("ing",)),
    ("networkpolicies", "networkpolicy", ("netpol",)),
    ("runtimeclasses", "runtimeclass", ()),
    ("poddisruptionbudgets", "poddisruptionbudget", ("pdb",)),
    ("podsecuritypolicies", "podsecuritypolicy", ("psp",)),
    ("clusterrolebindings", "clusterrolebinding", ()),
    ("clusterroles", "clusterrole", ()),
    ("rolebindings", "rolebinding", ()),
    ("roles", "role", ()),
    ("priorityclasses", "priorityclass", ("pc",)),
    ("csidrivers", "csidriver", ()),
    ("csinodes", "csinode", ()),
    ("csistoragecapacities", "csistoragecapacity", ()),
    ("storageclasses", "storageclass", ("sc",)),
    ("volumeattachments", "volumeattachment", ()),
    ("volumeattributesclasses", "volumeattributesclass", ("vac",)),
    ("ipaddresses", "ipaddress", ("ip",)),
    ("servicecidrs", "servicecidr", ()),
]

_INDEX = {}
for _plural, _singular, _shorts in BUILTIN:
    _names = frozenset({_plural, _singular, *_shorts})
    for _name in _names:
        _INDEX[_name] = _names


def variants(name):
    """The name plus its likely singular/plural spellings (`es`, `ies`/`y`, `s`): a
    spelling the table does not list still lands on the same resource."""
    out = {name}
    if name.endswith("ies"):
        out.add(name[:-3] + "y")
    if name.endswith("y"):
        out.add(name[:-1] + "ies")
    if name.endswith("es"):
        out.add(name[:-2])
    if name.endswith("s"):
        out.add(name[:-1])
    return {v for v in out if len(v) >= 3 or v == name}


def _bare(entry):
    """A setting entry as a bare lower-cased name: `.version.group` and `/name` stripped."""
    return str(entry).split("/", 1)[0].lower().split(".", 1)[0].strip()


def parse_api_resources(text):
    """Rows of `kubectl api-resources -o wide`: {name, shortnames, apiversion, kind}.
    Columns are read by the header's positions because SHORTNAMES may be empty."""
    lines = text.splitlines()
    if not lines:
        return []
    header = lines[0]
    cols = ["NAME", "SHORTNAMES", "APIVERSION", "NAMESPACED", "KIND"]
    starts = [header.find(c) for c in cols]
    if starts[0] != 0 or any(s < 0 for s in starts) or starts != sorted(starts):
        return []
    end = header.find("VERBS")  # -o wide adds VERBS and CATEGORIES after KIND
    rows = []
    for line in lines[1:]:
        if not line.strip():
            continue
        cells = [
            line[
                s : (starts[i + 1] if i + 1 < len(starts) else (end if end > 0 else len(line)))
            ].strip()
            for i, s in enumerate(starts)
        ]
        name, shorts, version, _namespaced, kind = cells
        rows.append(
            {
                "name": name,
                "shortnames": [x for x in shorts.split(",") if x],
                "apiversion": version,
                "kind": kind,
            }
        )
    return rows


def _row_names(row):
    names = {row["name"].lower(), row["kind"].lower(), *(s.lower() for s in row["shortnames"])}
    names.discard("")
    return names


def expand_denied(entries, lookup=None):
    """Every name that refers to a denied resource, for the `deniedResources` entries.
    Built-in kinds resolve from the table; the others through `lookup()` (rows from
    `parse_api_resources`, called at most once). An entry that resolves nowhere raises
    invalid_input: the task refuses to run rather than deny less than was asked."""
    names = set()
    rows = None
    for entry in entries:
        bare = _bare(entry)
        if not bare:
            continue
        found = None
        for candidate in variants(bare):
            if candidate in _INDEX:
                found = _INDEX[candidate]
                break
        if found is None:
            if rows is None:
                rows = lookup() if lookup else []
            for row in rows:
                row_names = _row_names(row)
                if any(c in row_names for c in variants(bare)):
                    found = row_names
                    break
        if found is None:
            raise RwError(
                "invalid_input",
                f"deniedResources entry '{entry}' does not name a resource this cluster "
                "serves, so it cannot be enforced; fix the setting",
            )
        for name in found:
            names |= variants(name)
    return names
