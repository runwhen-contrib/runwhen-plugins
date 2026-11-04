"""version-support: the cluster's Kubernetes version against the minor versions AKS offers in its
region (`az aks get-versions`) and the upgrades AKS offers the cluster (`az aks get-upgrades`).

AKS publishes the versions it supports now; it does not publish an end-of-support date in the API.
The task reports where the cluster's minor stands in that list, never a date it does not have."""

import re

from .az import az_json
from .cluster import LOCATION, resolve, section
from .common import NESTED_LIMIT
from .errors import RwError

_VERSION = re.compile(r"^(\d+)\.(\d+)(?:\.(\d+))?")


def key(version):
    """(major, minor, patch) of a version string; None for text that is not a version."""
    found = _VERSION.match(version) if isinstance(version, str) else None
    return tuple(int(p or 0) for p in found.groups()) if found else None


def minor_of(version):
    found = _VERSION.match(version) if isinstance(version, str) else None
    return f"{found[1]}.{found[2]}" if found else None


def offered(reply):
    """The minor versions of `az aks get-versions`: [{version, isDefault, isPreview, plans, patches}],
    newest first. An answer without a list is an error, never 'no versions are supported'."""
    values = reply.get("values") if isinstance(reply, dict) else None
    if not isinstance(values, list) or not values:
        raise RwError("internal", "az aks get-versions returned no versions")
    minors = []
    for row in values:
        row = row if isinstance(row, dict) else {}
        version = row.get("version")
        if key(version) is None:
            raise RwError(
                "internal", "az aks get-versions returned a version that is not a version"
            )
        patches = row.get("patchVersions")
        plans = section(row, "capabilities").get("supportPlan")
        minors.append(
            {
                "version": version,
                "isDefault": row.get("isDefault") is True,
                "isPreview": row.get("isPreview") is True,
                "supportPlans": sorted(str(p) for p in plans) if isinstance(plans, list) else [],
                "patches": sorted(patches, key=key) if isinstance(patches, dict) else [],
            }
        )
    return sorted(minors, key=lambda m: key(m["version"]), reverse=True)


def versions_facts(minors, current):
    """Where the cluster's version stands among the offered minors."""
    minor = minor_of(current)
    mine = next((m for m in minors if m["version"] == minor), None)
    stable = [m for m in minors if not m["isPreview"]]
    newer = [m for m in stable if minor is not None and key(m["version"]) > key(minor)]
    return {
        "minorSupported": mine is not None,
        "minorIsPreview": None if mine is None else mine["isPreview"],
        "minorSupportPlans": [] if mine is None else mine["supportPlans"],
        "latestPatchOfMinor": mine["patches"][-1] if mine and mine["patches"] else None,
        # the exact version is still one AKS offers (a retired patch is not listed any more)
        "patchOffered": None if mine is None else current in mine["patches"],
        "defaultMinor": next((m["version"] for m in minors if m["isDefault"]), None),
        "latestStableMinor": stable[0]["version"] if stable else None,
        "minorsBehind": len(newer),
        "supportedMinors": [
            {k: m[k] for k in ("version", "isDefault", "isPreview", "supportPlans")}
            for m in minors[:NESTED_LIMIT]
        ],
    }


def upgrade_facts(reply):
    """The control plane's upgrade options from `az aks get-upgrades`."""
    profile = reply.get("controlPlaneProfile") if isinstance(reply, dict) else None
    if not isinstance(profile, dict):
        raise RwError("internal", "az aks get-upgrades returned no control plane profile")
    rows = profile.get("upgrades")
    options = []
    for row in rows if isinstance(rows, list) else []:
        row = row if isinstance(row, dict) else {}
        if key(row.get("kubernetesVersion")) is None:
            raise RwError(
                "internal", "az aks get-upgrades returned a version that is not a version"
            )
        options.append((row["kubernetesVersion"], row.get("isPreview") is True))
    options.sort(key=lambda o: key(o[0]))
    stable = [v for v, preview in options if not preview]
    return {
        "available": bool(options),
        "versions": [{"version": v, "isPreview": p} for v, p in options[:NESTED_LIMIT]],
        "versionsOmitted": max(len(options) - NESTED_LIMIT, 0),
        "latest": options[-1][0] if options else None,
        "latestStable": stable[-1] if stable else None,
    }


def read(ctx):
    subscription, group, name, _, doc = resolve(ctx)
    location = doc.get("location")
    if not isinstance(location, str) or not LOCATION.match(location):
        raise RwError("internal", "az aks show returned no location for the cluster")
    desired = (
        doc.get("kubernetesVersion") if isinstance(doc.get("kubernetesVersion"), str) else None
    )
    current = doc.get("currentKubernetesVersion")
    current = current if isinstance(current, str) and current else desired
    if key(current) is None:
        raise RwError("internal", "az aks show returned no Kubernetes version for the cluster")
    out = {
        "cluster": name,
        "location": location,
        "kubernetesVersion": desired,
        "currentKubernetesVersion": current,
        "minor": minor_of(current),
        "supportPlan": doc.get("supportPlan") if isinstance(doc.get("supportPlan"), str) else None,
        "minorSupported": None,
        "minorIsPreview": None,
        "minorSupportPlans": [],
        "latestPatchOfMinor": None,
        "patchOffered": None,
        "defaultMinor": None,
        "latestStableMinor": None,
        "minorsBehind": None,
        "supportedMinors": [],
        "upgrades": None,
    }
    failed = []
    try:
        out.update(
            versions_facts(
                offered(
                    az_json(
                        ctx,
                        [
                            "aks",
                            "get-versions",
                            "--location",
                            location,
                            "--subscription",
                            subscription,
                        ],
                    )
                ),  # fmt: skip
                current,
            )
        )
    except RwError as exc:
        failed.append(("versions", exc))
    try:
        out["upgrades"] = upgrade_facts(
            az_json(
                ctx,
                [
                    "aks",
                    "get-upgrades",
                    "--name",
                    name,
                    "-g",
                    group,
                    "--subscription",
                    subscription,
                ],
            )  # fmt: skip
        )
    except RwError as exc:
        failed.append(("upgrades", exc))
    if len(failed) == 2:
        raise failed[0][1]
    for scope, exc in failed:
        ctx.error(exc.code, exc.message, scope=scope)
    return out
