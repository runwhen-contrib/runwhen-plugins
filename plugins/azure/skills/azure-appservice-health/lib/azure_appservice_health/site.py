"""The web app: state and availability, configuration facts, slots and deployments.

Every fact is picked out of the ARM object by name. The site object and `config/web` carry more than a
task may return (the publishing user name, app settings and connection strings as null placeholders,
the machine key, storage account mounts), so nothing is copied across wholesale; the whole of what
leaves is in this file."""

import re
from datetime import datetime, timedelta, timezone

from .arm import get, get_list
from .common import clip, listing
from .errors import RwError
from .target import parse_id, site_id

# Kudu's DeployStatus, as the sites/deployments API reports it
DEPLOY_STATUS = {0: "Pending", 1: "Building", 2: "Deploying", 3: "Failed", 4: "Success"}
IN_PROGRESS = ("Pending", "Building", "Deploying")
MESSAGE_LIMIT = 256  # a deployment message is the committer's text: short, and masked


_FRACTION = re.compile(r"(\.\d{6})\d+")


def parse_time(text):
    """An ARM timestamp (`2026-11-03T08:14:00.1234567Z`, seven fractional digits) as an aware UTC
    datetime, or None when it is not one."""
    if not isinstance(text, str) or not text:
        return None
    try:
        moment = datetime.fromisoformat(_FRACTION.sub(r"\1", text.strip().replace("Z", "+00:00")))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def props_of(doc, what):
    props = doc.get("properties") if isinstance(doc, dict) else None
    if not isinstance(props, dict):
        raise RwError("internal", f"Azure returned a {what} without properties")
    return props


def read_site(ctx, t):
    """The site object of a web app target (a not_found propagates). Checked to be the app asked for."""
    doc = get(ctx, site_id(t.subscription, t.group, t.name))
    props_of(doc, "web app")
    sub, group, name = parse_id(doc.get("id"), "sites")
    if name.lower() != t.name.lower() or group.lower() != t.group.lower() or sub != t.subscription:
        raise RwError("internal", f"Azure answered for another app than {t.name}")
    return doc


def kind_of(doc):
    return str(doc.get("kind") or "")


def is_function_app(doc):
    return "functionapp" in kind_of(doc).lower()


def plan_name(props):
    """(plan name, plan resource group) from the site's serverFarmId, or (None, None) when the app
    does not run on an App Service plan (a function app hosted on Container Apps)."""
    farm = props.get("serverFarmId")
    if not farm:
        return None, None
    _, group, name = parse_id(farm, "serverfarms")
    return name, group


def app_state(doc):
    """The `appState` output: what Azure says the app is."""
    props = props_of(doc, "web app")
    state = props.get("state")
    if not isinstance(state, str) or not state:
        # a site without a state is not "running": the platform did not say
        raise RwError("internal", "Azure returned a web app without a state")
    kind = kind_of(doc)
    plan, plan_group = plan_name(props)
    availability = props.get("availabilityState")
    usage = props.get("usageState")
    return {
        "app": str(doc.get("name")),
        "kind": kind,
        "isFunctionApp": is_function_app(doc),
        "isLinux": bool(props.get("reserved")),
        "isContainer": "container" in kind.lower(),
        "state": state,
        "running": state == "Running",
        "enabled": props.get("enabled") if isinstance(props.get("enabled"), bool) else None,
        "availabilityState": availability if isinstance(availability, str) else None,
        "limited": None if not isinstance(availability, str) else availability == "Limited",
        "usageState": usage if isinstance(usage, str) else None,
        "quotaExceeded": None if not isinstance(usage, str) else usage == "Exceeded",
        "suspendedTill": props.get("suspendedTill") or None,
        "defaultHostName": props.get("defaultHostName") or None,
        "lastModifiedTimeUtc": props.get("lastModifiedTimeUtc") or None,
        "location": str(doc.get("location") or "") or None,
        "planName": plan,
        "planResourceGroup": plan_group,
    }


def identity_facts(doc):
    """The managed identity: its type and how many user-assigned identities, never an id."""
    identity = doc.get("identity")
    if not isinstance(identity, dict) or not identity.get("type"):
        return {"type": "None", "systemAssigned": False, "userAssignedCount": 0}
    kind = str(identity["type"])
    users = identity.get("userAssignedIdentities")
    return {
        "type": kind,
        "systemAssigned": "systemassigned" in kind.lower().replace(" ", ""),
        "userAssignedCount": len(users) if isinstance(users, dict) else 0,
    }


def _flag(value):
    return value if isinstance(value, bool) else None


_USERINFO = re.compile(r"^((?:[a-z][a-z0-9+.-]*://)?)[^/@\s]*@(?!sha\d+:)", re.I)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_URL_CREDENTIALS = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^\s/@]+@")


def runtime(value):
    """The runtime stack as `linuxFxVersion` / `windowsFxVersion` spells it, safe to return.

    `NODE|20-lts` and the like are returned as they are. `COMPOSE|` and `KUBE|` are followed by a whole
    multi-container configuration (base64 of a compose file or a Kubernetes manifest, which can hold
    anything): only the prefix is returned. `DOCKER|` is followed by the image reference: it is returned
    with a `user:pass@` in front of the host stripped (a digest, `image@sha256:...`, is not a user)."""
    if not isinstance(value, str) or not value:
        return None
    head, bar, rest = value.partition("|")
    kind = head.upper()
    if kind in ("COMPOSE", "KUBE"):
        return f"{kind}|"
    if kind == "DOCKER":
        image = _USERINFO.sub(lambda found: found.group(1), rest.strip())
        return clip(f"DOCKER|{image}", 256)[0]
    return clip(value, 256)[0]


def mask(text):
    """A free text with e-mail addresses and the credentials in URLs (`https://user:token@host`) masked.
    A commit message is the committer's text, not ours: it can hold either."""
    text = _URL_CREDENTIALS.sub(r"\1[credentials]@", text)
    return _EMAIL.sub("[email]", text)


def swap_facts(props):
    """Whether a slot swap is under way on this site object or slot: `slotSwapStatus` is set while one
    runs, `targetSwapSlot` names the slot it is applied to (a swap with preview)."""
    return {
        "swapInProgress": bool(props.get("slotSwapStatus")),
        "targetSwapSlot": props.get("targetSwapSlot") or None,
    }


def _text(value, limit=256):
    return clip(value, limit)[0] if isinstance(value, str) and value else None


def configuration(doc, config):
    """The `configuration` output. `config` is the `config/web` object, or None when it could not
    be read (then its fields are null and the task reports the error)."""
    props = props_of(doc, "web app")
    cfg = config.get("properties") if isinstance(config, dict) else None
    cfg = cfg if isinstance(cfg, dict) else {}
    have = isinstance(config, dict)
    fx = runtime(cfg.get("linuxFxVersion")) or runtime(cfg.get("windowsFxVersion"))
    return {
        "app": str(doc.get("name")),
        "kind": kind_of(doc),
        "isFunctionApp": is_function_app(doc),
        "httpsOnly": _flag(props.get("httpsOnly")),
        "clientCertEnabled": _flag(props.get("clientCertEnabled")),
        "clientCertMode": props.get("clientCertMode") or None,
        "publicNetworkAccess": props.get("publicNetworkAccess") or None,
        "vnetIntegrated": bool(props.get("virtualNetworkSubnetId")),
        "identity": identity_facts(doc),
        "configRead": have,
        "alwaysOn": _flag(cfg.get("alwaysOn")),
        "minTlsVersion": cfg.get("minTlsVersion") or None,
        "scmMinTlsVersion": cfg.get("scmMinTlsVersion") or None,
        "ftpsState": cfg.get("ftpsState") or None,
        "http20Enabled": _flag(cfg.get("http20Enabled")),
        "healthCheckPath": _text(cfg.get("healthCheckPath")),
        "runtime": fx,
        "numberOfWorkers": cfg.get("numberOfWorkers")
        if isinstance(cfg.get("numberOfWorkers"), int)
        else None,
        "webSocketsEnabled": _flag(cfg.get("webSocketsEnabled")),
        "remoteDebuggingEnabled": _flag(cfg.get("remoteDebuggingEnabled")),
        "autoHealEnabled": _flag(cfg.get("autoHealEnabled")),
    }


def _props(row):
    return row.get("properties") if isinstance(row.get("properties"), dict) else {}


def slot_row(row):
    props = _props(row)
    # ARM names a slot "<app>/<slot>"
    return {
        "name": str(row.get("name") or "").split("/")[-1],
        "state": props.get("state") if isinstance(props.get("state"), str) else None,
        "availabilityState": props.get("availabilityState") or None,
        "enabled": _flag(props.get("enabled")),
        "defaultHostName": props.get("defaultHostName") or None,
        "lastModifiedTimeUtc": props.get("lastModifiedTimeUtc") or None,
    }


def slots(rows, limit):
    items = sorted((slot_row(r) for r in rows), key=lambda s: s["name"].lower())
    return listing(items, limit)


def read_slots(ctx, t):
    """The slot rows of the app and whether the whole list was read."""
    return get_list(ctx, site_id(t.subscription, t.group, t.name) + "/slots")


def deployment_row(row):
    props = _props(row)
    code = props.get("status")
    status = DEPLOY_STATUS.get(code, f"Unknown({code})") if isinstance(code, int) else None
    raw = props.get("message")
    message, cut = clip(mask(raw) if isinstance(raw, str) else raw, MESSAGE_LIMIT)
    return {
        "id": str(props.get("id") or row.get("name") or "")[:64],
        "status": status,
        "active": _flag(props.get("active")),
        "deployer": _text(props.get("deployer"), 128),
        "message": message or None,
        "truncated": cut,
        "startTime": props.get("start_time") or None,
        "endTime": props.get("end_time") or None,
    }


def read_deployments(ctx, t):
    return get_list(ctx, site_id(t.subscription, t.group, t.name) + "/deployments")


def deployments(rows, complete, now, seconds, limit):
    """The `deployments` part of the output: the records that started within the window, newest
    first. `latest` is the newest record of the whole list, window or not: the last deploy is a fact
    worth having even when it is old."""
    floor = datetime.min.replace(tzinfo=timezone.utc)
    parsed = sorted(
        ((parse_time(_props(r).get("start_time")) or floor, deployment_row(r)) for r in rows),
        key=lambda pair: pair[0],
        reverse=True,
    )
    edge = now - timedelta(seconds=seconds)
    inside = [d for moment, d in parsed if moment >= edge]
    out = listing(inside, limit)
    out["listed"] = len(parsed)
    out["complete"] = complete
    out["latest"] = parsed[0][1] if parsed else None
    out["failedInWindow"] = sum(1 for d in inside if d["status"] == "Failed")
    out["inProgress"] = sum(1 for d in inside if d["status"] in IN_PROGRESS)
    out["activeDeployment"] = next((d for _, d in parsed if d["active"]), None)
    return out
