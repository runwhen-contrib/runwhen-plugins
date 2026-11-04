"""run-os-check: one fixed, read-only diagnostic script run inside a Linux VM through the VM agent
(`az vm run-command invoke --command-id RunShellScript`).

This is remote command execution on a customer VM, so it is a write task: the script is chosen
from SCRIPTS by name (disk, memory, uptime, patches) and is never built from input. The instance
view is read first (it is also the lookup): a VM that is not Linux, not running or whose agent is not
ready is skipped with the reason instead of waiting for a run command that cannot answer. A dry run
does everything but the invocation."""

from types import SimpleNamespace

from . import guest
from .az import az_json, command_timeout
from .common import number
from .errors import RwError
from .guest_scripts import CHECKS, SCRIPTS, sha256
from .instance_view import agent_facts, dicts, state_of
from .target import VM, checked_id, target

COMMAND_ID = "RunShellScript"


class GuestCtx:
    """The task's context with the run-command timeout in place of commandTimeout: `az_json` reads
    only `settings.command_timeout` and `run`, and an invocation takes longer than a read."""

    def __init__(self, ctx, seconds):
        self._ctx = ctx
        self.settings = SimpleNamespace(command_timeout=f"{seconds}s")
        self.run = ctx.run


def read_timeout(settings):
    """Seconds an invocation may take, from the osCheckTimeout setting (a number of seconds)."""
    return int(number(settings, "osCheckTimeout", "os_check_timeout", 10, 900))


def check_name(check):
    if check not in CHECKS:
        raise RwError("invalid_input", f"check must be one of {', '.join(CHECKS)}, got {check!r}")
    return check


def run(ctx, check, th, limit):
    check = check_name(check)
    seconds = read_timeout(ctx.settings)
    command_timeout(ctx.settings)  # commandTimeout: a duration, checked before any az call
    kind, subscription, group, name = target(ctx.resource, (VM,))
    doc = az_json(
        ctx,
        ["vm", "get-instance-view", "--name", name, "-g", group, "--subscription", subscription],
    )
    checked_id(doc, kind, subscription, group, name)
    view = doc.get("instanceView")
    if not isinstance(view, dict):
        raise RwError("internal", "az vm get-instance-view returned no instanceView")
    os_type = ((doc.get("storageProfile") or {}).get("osDisk") or {}).get("osType")
    power = state_of(dicts(view.get("statuses")), "PowerState/")
    agent = agent_facts(view)
    why = None
    if os_type != "Linux":
        why = f"the {check} check runs a shell script and supports Linux VMs; this VM is {os_type or 'of unknown OS type'}"
    elif power != "running":
        why = f"the VM is not running (power state {power or 'unknown'}): a run command cannot be delivered"
    elif agent["ready"] is False:
        why = "the VM agent is not ready: a run command cannot be delivered"
    if why:
        ctx.skip(why)  # raises: the run ends `skipped` with the reason
    plan = {
        "vm": name,
        "check": check,
        "osType": os_type,
        "powerState": power,
        "scriptSha256": sha256(check),
        "scriptLines": len(SCRIPTS[check].splitlines()),
    }
    if ctx.dry_run:
        return {**plan, "executed": False, "dryRun": True, "invocation": None, "stderr": "",
                "truncated": False, "disk": None, "memory": None, "uptime": None, "patches": None}  # fmt: skip
    reply = az_json(
        GuestCtx(ctx, seconds),
        [
            "vm", "run-command", "invoke",
            "--name", name, "-g", group,
            "--command-id", COMMAND_ID,
            "--scripts", SCRIPTS[check],
            "--subscription", subscription,
        ],
    )  # fmt: skip
    code, display, level, stdout, stderr = guest.invocation(reply)
    if not code.startswith("ProvisioningState/succeeded"):
        text, _ = guest.stderr_text(stderr or stdout)
        raise RwError("internal", f"the run command did not succeed ({display or code}): {text}")
    lines, rc = guest.block(check, stdout)
    facts = {"disk": None, "memory": None, "uptime": None, "patches": None}
    if check == "disk":
        facts["disk"] = guest.disk(lines, rc, th.disk_used_percent, limit)
    elif check == "memory":
        facts["memory"] = guest.memory(lines, rc, th.memory_used_percent)
    elif check == "uptime":
        facts["uptime"] = guest.uptime(lines, rc, th.uptime_days)
    else:
        facts["patches"] = guest.patches(lines, rc)
    text, truncated = guest.stderr_text(stderr)
    return {
        **plan,
        "executed": True,
        "dryRun": False,
        "invocation": {"code": code, "displayStatus": display, "level": level},
        "stderr": text,
        "truncated": truncated,
        **facts,
    }
