"""instance-view: what Azure says the virtual machine is doing (`az vm get-instance-view`): power
and provisioning state, the VM agent, the extensions and boot diagnostics.

One call answers all of it, and it is also the lookup: a VM that is gone is not_found. A section
the instance view does not carry (a deallocated VM has no agent report) is null, never a healthy
reading."""

from .az import az_json
from .common import NESTED_LIMIT, clip
from .errors import RwError
from .target import VM, checked_id, target


def dicts(value):
    return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []


def status(entry):
    """One status object of an instance view: code, level, displayStatus, message (cut to 1024
    characters), time."""
    message, cut = clip(entry.get("message"))
    return {
        "code": str(entry.get("code") or ""),
        "level": entry.get("level") or None,
        "displayStatus": entry.get("displayStatus") or None,
        "message": message,
        "time": entry.get("time") or None,
        "truncated": cut,
    }


def state_of(statuses, prefix):
    """The part of the first status code that starts with `prefix` (`PowerState/running` ->
    `running`), or None when there is none."""
    for entry in statuses:
        code = str(entry.get("code") or "")
        if code.startswith(prefix):
            return code[len(prefix) :] or None
    return None


def agent_facts(view):
    agent = view.get("vmAgent")
    if not isinstance(agent, dict):
        return {"reported": False, "version": None, "ready": None, "statuses": []}
    statuses = dicts(agent.get("statuses"))
    display = statuses[0].get("displayStatus") if statuses else None
    return {
        "reported": True,
        "version": agent.get("vmAgentVersion") or None,
        # Azure's own word for a working agent is "Ready"; no status at all is not a ready agent
        "ready": (display == "Ready") if display else None,
        "statuses": [status(s) for s in statuses[:NESTED_LIMIT]],
    }


def extension_facts(view, limit):
    items = dicts(view.get("extensions"))
    out = []
    for ext in items:
        statuses = dicts(ext.get("statuses"))
        first = status(statuses[0]) if statuses else None
        out.append(
            {
                "name": str(ext.get("name") or ""),
                "type": ext.get("type") or None,
                "typeHandlerVersion": ext.get("typeHandlerVersion") or None,
                "code": first["code"] if first else None,
                "level": first["level"] if first else None,
                "displayStatus": first["displayStatus"] if first else None,
                "message": first["message"] if first else "",
                "failed": bool(first)
                and (first["level"] == "Error" or "failed" in first["code"].lower()),
                "truncated": first["truncated"] if first else False,
            }
        )
    return {
        "total": len(out),
        "failed": sum(1 for e in out if e["failed"]),
        "items": out[: min(limit, NESTED_LIMIT)],
        "omitted": max(len(out) - min(limit, NESTED_LIMIT), 0),
    }


def boot_diagnostics(doc, view):
    profile = (doc.get("diagnosticsProfile") or {}).get("bootDiagnostics")
    boot = view.get("bootDiagnostics") if isinstance(view.get("bootDiagnostics"), dict) else {}
    first = boot.get("status")
    return {
        "enabled": profile.get("enabled") if isinstance(profile, dict) else None,
        "consoleScreenshotAvailable": bool(boot.get("consoleScreenshotBlobUri")),
        "serialLogAvailable": bool(boot.get("serialConsoleLogBlobUri")),
        "status": status(first) if isinstance(first, dict) else None,
    }


def read(ctx, limit):
    kind, subscription, group, name = target(ctx.resource, (VM,))
    doc = az_json(
        ctx,
        ["vm", "get-instance-view", "--name", name, "-g", group, "--subscription", subscription],
    )
    checked_id(doc, kind, subscription, group, name)
    view = doc.get("instanceView")
    if not isinstance(view, dict):
        # a VM whose reply carries no instance view is not "nothing wrong": Azure did not say
        raise RwError("internal", "az vm get-instance-view returned no instanceView")
    statuses = dicts(view.get("statuses"))
    power = state_of(statuses, "PowerState/")
    return {
        "vm": name,
        "location": doc.get("location") or None,
        "vmSize": (doc.get("hardwareProfile") or {}).get("vmSize") or None,
        "osType": ((doc.get("storageProfile") or {}).get("osDisk") or {}).get("osType") or None,
        "provisioningState": doc.get("provisioningState") or None,
        "instanceProvisioningState": state_of(statuses, "ProvisioningState/"),
        "powerState": power,
        "running": (power == "running") if power else None,
        "statuses": [status(s) for s in statuses[:NESTED_LIMIT]],
        "computerName": view.get("computerName") or None,
        "osName": view.get("osName") or None,
        "osVersion": view.get("osVersion") or None,
        "platformFaultDomain": view.get("platformFaultDomain"),
        "platformUpdateDomain": view.get("platformUpdateDomain"),
        "agent": agent_facts(view),
        "extensions": extension_facts(view, limit),
        "bootDiagnostics": boot_diagnostics(doc, view),
    }
