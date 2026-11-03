"""Fetching the target CronJob and the objects the tasks look at (the only I/O besides k8s.py)."""

from k8s_cronjob_health.common import parse_duration
from k8s_cronjob_health.k8s import RwError, kubectl_json, secondary

CRONJOB_TYPE = "kubernetes/cronjob"
PLURAL = "cronjobs"


def target_type(ctx):
    """Qualified type of the target: `ctx.resource.type`, else derived from the
    plural segment before the name at the end of `ctx.resource.path`."""
    rtype = getattr(ctx.resource, "type", None)
    if rtype:
        return rtype
    parts = (getattr(ctx.resource, "path", None) or "").split("/")
    if len(parts) >= 2 and parts[-2] == PLURAL:
        return CRONJOB_TYPE
    return None


def check_duration(value):
    """Reject a bad duration before any kubectl call."""
    try:
        parse_duration(value)
    except ValueError as exc:
        raise RwError("invalid_input", str(exc)) from exc


# Bounds of the numeric settings (the same min/max tasks.yaml declares), enforced here
# too so an out-of-range value fails as invalid_input before any kubectl call.
SETTING_BOUNDS = {"max_items": (1, None), "late_grace_seconds": (0, None)}


def check_settings(ctx, *names):
    for name in names:
        value = getattr(ctx.settings, name)
        low, high = SETTING_BOUNDS[name]
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or value < low
            or (high is not None and value > high)
        ):
            raise RwError(
                "invalid_input",
                f"setting {name} must be between {low} and {high or 'any'}, got {value!r}",
            )


def check_target(ctx):
    rtype = target_type(ctx)
    if rtype != CRONJOB_TYPE:
        raise RwError("invalid_input", f"k8s-cronjob-health does not apply to {rtype!r}")


def load_cronjob(ctx):
    """The target CronJob. A missing one raises (NotFound from kubectl -> not_found)."""
    check_target(ctx)
    return kubectl_json(
        ctx, "-n", ctx.resource.namespace, "get", "cronjobs.batch", ctx.resource.name
    )


def list_jobs(ctx):
    """Every Job in the CronJob's namespace (Jobs name their owner in ownerReferences,
    not by a label the CronJob guarantees). A failure raises."""
    return kubectl_json(ctx, "-n", ctx.resource.namespace, "get", "jobs.batch")["items"]


def list_jobs_secondary(ctx):
    """The same list when it only enriches the result: a failure is recorded with
    `ctx.error` (scope jobs.batch) and None is returned."""
    doc = secondary(ctx, "jobs.batch", "-n", ctx.resource.namespace, "get", "jobs.batch")
    return None if doc is None else doc["items"]


def list_pods_of_jobs_secondary(ctx):
    """Pods that belong to some Job (label `job-name`), or None after a recorded failure."""
    doc = secondary(ctx, "pods", "-n", ctx.resource.namespace, "get", "pods", "-l", "job-name")
    return None if doc is None else doc["items"]


def list_cronjob_events_secondary(ctx, cronjob):
    """Events about the CronJob (all types, matched by its uid), or None after a recorded failure."""
    uid = cronjob["metadata"]["uid"]
    doc = secondary(
        ctx,
        "events",
        "-n",
        ctx.resource.namespace,
        "get",
        "events",
        "--field-selector",
        f"involvedObject.uid={uid}",
    )
    return None if doc is None else doc["items"]
