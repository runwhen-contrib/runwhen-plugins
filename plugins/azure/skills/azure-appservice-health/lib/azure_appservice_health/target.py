"""What every azure-appservice-health task shares about its target: a web app (a function app is a
web app of kind functionapp) or an App Service plan, read from `ctx.resource`, and the ARM ids built
from it.

The target path is `azure/subscriptions/<id>/resourcegroups/<group>/web-apps/<name>` or
`.../app-service-plans/<name>`. The ids are built, not looked up: a site and a plan have a fixed ARM id
(`Microsoft.Web/sites`, `Microsoft.Web/serverfarms`) and the resource group is part of the target. A
task still reads the object first, so a target that is gone is `not_found` before anything else."""

import re
from types import SimpleNamespace

from .common import GUID
from .errors import RwError

WEB_APP = "azure/web-app"
PLAN = "azure/app-service-plan"
# Web app names: 2-60 characters of letters, digits and hyphens. Plan names are as wide (letters, digits,
# hyphens, underscores). Resource group names: up to 90 characters of letters, digits, `_ . ( ) -`, not
# starting with a hyphen (it would read as an option).
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,59}$")
_GROUP = re.compile(r"^[\w.()][\w.()-]{0,89}$")
_SLOT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,58}$")
# the ARM id of a site or a plan, as ARM returns it (a slot's id has one more `/slots/<name>`)
_ID = re.compile(
    r"^/subscriptions/(?P<sub>[0-9a-fA-F-]{36})/resourceGroups/(?P<group>[\w.()-]{1,90})"
    r"/providers/Microsoft\.Web/(?P<kind>sites|serverfarms)/(?P<name>[A-Za-z0-9_-]{1,60})$",
    re.I,
)


def target(resource, allowed=(WEB_APP, PLAN)):
    """(type, subscription id, resource group, name) from the run's target; any other type is refused.
    The subscription and the resource group are identity fields of the target (its parents in the
    path); the group is lower case, as the identity keeps it."""
    resource = resource or {}
    kind = resource.get("type")
    if kind not in allowed or not resource.get("name"):
        raise RwError(
            "invalid_input",
            f"this task runs on {' or '.join(allowed)}, got {kind!r}",
        )
    subscription = str(resource.get("subscription") or "")
    if not GUID.match(subscription):
        raise RwError("invalid_input", "the target has no subscription id")
    group = str(resource.get("resourcegroup") or "")
    if not _GROUP.match(group):
        raise RwError("invalid_input", "the target has no valid resource group")
    name = str(resource["name"])
    if not _NAME.match(name):
        raise RwError("invalid_input", f"{name!r} is not a web app or App Service plan name")
    return SimpleNamespace(type=kind, subscription=subscription.lower(), group=group, name=name)


def site_id(subscription, group, name):
    return (
        f"/subscriptions/{subscription}/resourceGroups/{group}/providers/Microsoft.Web/sites/{name}"
    )


def plan_id(subscription, group, name):
    return (
        f"/subscriptions/{subscription}/resourceGroups/{group}"
        f"/providers/Microsoft.Web/serverfarms/{name}"
    )


def parse_id(value, kind):
    """(subscription, group, name) of an ARM id that must be a site (`sites`) or a plan
    (`serverfarms`); anything else raises internal: the platform returned something unexpected."""
    found = _ID.match(str(value or ""))
    if not found or found["kind"].lower() != kind:
        raise RwError("internal", f"Azure returned an id that is not a {kind} id")
    return found["sub"].lower(), found["group"], found["name"]


def own_id(t):
    """The ARM id of the target itself."""
    build = site_id if t.type == WEB_APP else plan_id
    return build(t.subscription, t.group, t.name)


def slot_name(value, name="slot"):
    """A slot name as a caller gives it; the production slot is not a slot to name here."""
    text = str(value or "")
    if not _SLOT.match(text):
        raise RwError("invalid_input", f"{name} must be a slot name, got {value!r}")
    return text
