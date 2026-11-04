"""restart, start, stop, swap-slot and scale: the write tasks.

Each reads the object first (a target that is gone is not_found, a slot that does not exist is
not_found), checks its inputs before any write, and in a dry run stops there: Azure has no server-side
dry run for these calls, so a dry run is the read and the checks, with what would be sent reported
and nothing sent. After a write the object is read again and the result says whether it reached the
state the call asks for. Nothing here reads an app setting, a connection string or a credential."""

import re
import subprocess

from .az import MESSAGE_LIMIT, classify_error, headline, parse_duration
from .errors import RwError
from .plan import autoscale, plan_state, read_autoscale, read_plan, scale_bounds
from .site import app_state, read_site, read_slots, slot_row, swap_facts
from .target import PLAN, WEB_APP, slot_name, target

MAX_OPERATION = (
    600.0  # seconds a write may take at most; the task timeouts in tasks.yaml add the reads
)
_SKU = re.compile(r"^[A-Za-z][A-Za-z0-9]{0,15}$")
# the state each power action ends in
EXPECTED = {"restart": "Running", "start": "Running", "stop": "Stopped"}


def operation_timeout(settings):
    """Seconds a write az command may run, from the operationTimeout setting."""
    seconds = parse_duration(getattr(settings, "operation_timeout", "300s"), "operationTimeout")
    if seconds > MAX_OPERATION:
        raise RwError(
            "invalid_input", f"setting operationTimeout must be at most {MAX_OPERATION:.0f}s"
        )
    return seconds


def az_action(ctx, argv, seconds):
    """Run a write `az` command. Its output (none, or the changed object) is not used: the object is read
    again. A failure raises with the D64 code classified from az's ERROR lines, as `az_json` does."""
    try:
        result = ctx.run(["az", *argv, "--output", "json"], timeout=seconds, check=False)
    except subprocess.TimeoutExpired:
        raise RwError(
            "timeout",
            f"az {' '.join(argv[:2])} did not finish within {seconds:.0f}s; "
            "it may still be running in Azure: read the object again before retrying",
        ) from None
    except FileNotFoundError:
        raise RwError("dependency", "az is not installed in this runtime") from None
    if result.returncode != 0:
        stderr = result.stderr or ""
        text = " ".join(headline(stderr).split())[:MESSAGE_LIMIT]
        raise RwError(
            classify_error(stderr, result.returncode), text or f"az exited {result.returncode}"
        )


def _brief(state):
    return {"state": state["state"], "availabilityState": state["availabilityState"]}


def power(ctx, action, settings):
    """restart, start or stop the web app target."""
    seconds = operation_timeout(settings)
    t = target(ctx.resource, (WEB_APP,))
    before = app_state(read_site(ctx, t))
    expected = EXPECTED[action]
    out = {
        "app": t.name,
        "action": action,
        "applied": False,
        "dryRun": bool(ctx.dry_run),
        "expectedState": expected,
        "wouldChange": action == "restart" or before["state"] != expected,
        "before": _brief(before),
        "after": None,
        "reachedExpected": None,
    }
    if ctx.dry_run:
        return out
    az_action(
        ctx,
        ["webapp", action, "-g", t.group, "-n", t.name, "--subscription", t.subscription],
        seconds,
    )
    out["applied"] = True
    try:
        after = app_state(read_site(ctx, t))
    except RwError as exc:
        ctx.error(
            exc.code,
            f"{action} was sent, but the app could not be read afterwards: {exc.message}",
            scope="after",
        )
        return out
    out["after"] = _brief(after)
    out["reachedExpected"] = after["state"] == expected
    return out


def swap(ctx, source, target_slot, settings):
    """swap-slot: swap the source slot with the target slot (default: production) of the web app."""
    seconds = operation_timeout(settings)
    t = target(ctx.resource, (WEB_APP,))
    source = slot_name(source, "sourceSlot")
    if source.lower() == "production":
        raise RwError("invalid_input", "sourceSlot is the slot to swap in, not production")
    if str(target_slot).lower() == "production":
        target_slot = "production"
    else:
        target_slot = slot_name(target_slot, "targetSlot")
    if source.lower() == target_slot.lower():
        raise RwError("invalid_input", "sourceSlot and targetSlot are the same slot")
    site = read_site(ctx, t)
    rows, complete = read_slots(ctx, t)
    slots = {slot_row(r)["name"].lower(): slot_row(r) for r in rows}
    by_name = {slot_row(r)["name"].lower(): r for r in rows}
    for wanted in (source, target_slot):
        if wanted != "production" and wanted.lower() not in slots:
            if not complete:
                raise RwError("internal", f"the slot list is too long to check for {wanted!r}")
            raise RwError("not_found", f"{t.name} has no slot {wanted!r}")
    prod = {**_brief(app_state(site)), **swap_facts(site["properties"])}
    states = {"production": prod}
    states.update(
        {
            name: {**_brief_slot(slots[name]), **swap_facts(by_name[name].get("properties") or {})}
            for name in slots
        }
    )
    out = {
        "app": t.name,
        "sourceSlot": source,
        "targetSlot": target_slot,
        "applied": False,
        "dryRun": bool(ctx.dry_run),
        "before": {"source": states[source.lower()], "target": states[target_slot.lower()]},
        "after": None,
    }
    busy = [
        name
        for name, state in (
            ("source", out["before"]["source"]),
            ("target", out["before"]["target"]),
        )
        if state["swapInProgress"] or state["targetSwapSlot"]
    ]
    if busy:
        # a swap with preview or one still running: sending another swap on top of it is not a request
        # this task can make safely, in a dry run too
        raise RwError(
            "invalid_input",
            f"a slot swap is already under way on the {' and '.join(busy)} slot of {t.name} "
            "(slotSwapStatus or targetSwapSlot is set): wait for it, or finish or reset it in Azure",
        )
    if ctx.dry_run:
        return out
    az_action(
        ctx,
        [
            "webapp", "deployment", "slot", "swap", "-g", t.group, "-n", t.name,
            "--slot", source, "--target-slot", target_slot, "--subscription", t.subscription,
        ],
        seconds,
    )  # fmt: skip
    out["applied"] = True
    try:
        after_rows, _ = read_slots(ctx, t)
        after = {slot_row(r)["name"].lower(): _brief_slot(slot_row(r)) for r in after_rows}
        after["production"] = _brief(app_state(read_site(ctx, t)))
        out["after"] = {
            "source": after.get(source.lower()),
            "target": after.get(target_slot.lower()),
        }
    except RwError as exc:
        ctx.error(
            exc.code,
            f"the swap was sent, but the slots could not be read afterwards: {exc.message}",
            scope="after",
        )
    return out


def _brief_slot(slot):
    return {"state": slot["state"], "availabilityState": slot["availabilityState"]}


def _plan_view(state):
    return {
        "skuName": state["sku"]["name"],
        "tier": state["sku"]["tier"],
        "instanceCount": state["instanceCount"],
        "planStatus": state["planStatus"],
    }


def scale(ctx, instances, sku, settings, override_autoscale=False):
    """scale: set the plan's instance count and/or SKU to the values asked for.

    The plan's autoscale settings are read first, in a dry run and in the real run: an enabled setting
    that targets the plan changes the instance count again after this task, within its own bounds, so
    a request for a count outside them is refused (invalid_input) unless `override_autoscale` says the
    caller knows. The bounds are the widest envelope of the enabled settings' profiles."""
    seconds = operation_timeout(settings)
    t = target(ctx.resource, (PLAN,))
    if instances is None and not sku:
        raise RwError("invalid_input", "give instances, sku or both: there is nothing to change")
    if instances is not None and (
        isinstance(instances, bool) or not isinstance(instances, int) or instances < 1
    ):
        raise RwError(
            "invalid_input", f"instances must be a whole number from 1, got {instances!r}"
        )
    if sku is not None and not _SKU.match(str(sku)):
        raise RwError(
            "invalid_input", f"sku must be an App Service SKU name such as B1 or P1v3, got {sku!r}"
        )
    before = plan_state(read_plan(ctx, t))
    ceiling = before["maximumNumberOfWorkers"]
    if instances is not None and ceiling is not None and instances > ceiling and sku is None:
        raise RwError(
            "invalid_input", f"instances {instances} is above the {ceiling} this SKU allows"
        )
    rows, complete = read_autoscale(ctx, t)  # a failure here fails the task: no write is made blind
    bounds = scale_bounds(autoscale(rows, complete, t, before["instanceCount"], 10000))
    low, high = bounds["minimum"], bounds["maximum"]
    if (
        instances is not None
        and bounds["enabled"] is True
        and not override_autoscale
        and ((low is not None and instances < low) or (high is not None and instances > high))
    ):
        raise RwError(
            "invalid_input",
            f"instances {instances} is outside the {low} to {high} an enabled autoscale setting keeps "
            f"{t.name} within: it would scale the plan back. Change the setting, or give "
            "overrideAutoscale: true to scale anyway",
        )
    want_sku = str(sku) if sku else None
    out = {
        "plan": t.name,
        "applied": False,
        "dryRun": bool(ctx.dry_run),
        "requested": {"instances": instances, "sku": want_sku},
        "numberOfSites": before["numberOfSites"],
        "noChange": (instances is None or instances == before["instanceCount"])
        and (want_sku is None or want_sku.lower() == str(before["sku"]["name"]).lower()),
        "autoscale": bounds,
        "autoscaleOverridden": bool(
            override_autoscale
            and instances is not None
            and bounds["enabled"] is True
            and ((low is not None and instances < low) or (high is not None and instances > high))
        ),
        "before": _plan_view(before),
        "after": None,
        "reachedRequested": None,
    }
    if ctx.dry_run:
        return out
    argv = [
        "appservice",
        "plan",
        "update",
        "-g",
        t.group,
        "-n",
        t.name,
        "--subscription",
        t.subscription,
    ]
    if instances is not None:
        argv += ["--number-of-workers", str(instances)]
    if want_sku:
        argv += ["--sku", want_sku]
    az_action(ctx, argv, seconds)
    out["applied"] = True
    try:
        after = plan_state(read_plan(ctx, t))
    except RwError as exc:
        ctx.error(
            exc.code,
            f"the scale was sent, but the plan could not be read afterwards: {exc.message}",
            scope="after",
        )
        return out
    out["after"] = _plan_view(after)
    out["reachedRequested"] = (instances is None or after["instanceCount"] == instances) and (
        want_sku is None or want_sku.lower() == str(after["sku"]["name"]).lower()
    )
    return out
