"""instance-status: the status checks and scheduled events of one instance
(describe-instance-status --include-all-instances).

`--include-all-instances` makes EC2 answer for an instance that is not running too (its checks are
`not-applicable`); without it a stopped instance is missing from the answer, which would read as
"nothing wrong". An answer with no entry for the instance is therefore an error here, never an
empty result.

A check that EC2 could not evaluate (`insufficient-data`, `initializing`, `not-applicable`) is
unknown, not passing: the booleans below are true for `impaired`, false for `ok` and null for the
rest."""

from .awscli import aws_paged
from .errors import RwError
from .text import MAX_LIST, clip

STATUS_QUERY = (
    "{InstanceStatuses: InstanceStatuses[].{InstanceId: InstanceId, "
    "AvailabilityZone: AvailabilityZone, State: InstanceState.Name, "
    "StateCode: InstanceState.Code, SystemStatus: SystemStatus, InstanceStatus: InstanceStatus, "
    "AttachedEbsStatus: AttachedEbsStatus, "
    "Events: Events[].{InstanceEventId: InstanceEventId, Code: Code, Description: Description, "
    "NotBefore: NotBefore, NotAfter: NotAfter, NotBeforeDeadline: NotBeforeDeadline}}, "
    "NextToken: NextToken}"
)


def read_status(ctx, target):
    """The raw narrowed status entry of the instance `target`. A missing instance raises
    not_found, a forbidden read permission_denied; a list that is not read to its end or has no
    entry for the instance is an error."""
    rows, truncated = aws_paged(
        ctx,
        [
            "ec2",
            "describe-instance-status",
            "--instance-ids",
            target.instance_id,
            "--include-all-instances",
            "--query",
            STATUS_QUERY,
        ],
        region=target.region,
        items_key="InstanceStatuses",
    )
    rows = [r for r in rows if isinstance(r, dict) and r.get("InstanceId") == target.instance_id]
    if truncated or not rows:
        raise RwError(
            "internal",
            f"ec2 describe-instance-status returned no status for {target.instance_id}",
        )
    return rows[0]


def summary_facts(summary):
    """{status, details[]} of a check summary, or None when EC2 did not return the check."""
    if not isinstance(summary, dict):
        return None
    details = [
        {
            "name": d.get("Name"),
            "status": d.get("Status"),
            "impairedSince": d.get("ImpairedSince"),
        }
        for d in (summary.get("Details") or [])
        if isinstance(d, dict)
    ][:MAX_LIST]
    return {"status": summary.get("Status"), "details": details}


def impaired(summary):
    """True when the check says impaired, False when it says ok, None for anything else
    (insufficient-data, initializing, not-applicable, not returned): unknown is not passing."""
    if summary is None:
        return None
    return {"impaired": True, "ok": False}.get(summary["status"])


def event_facts(event):
    """One scheduled event. EC2 marks a finished one in its description: `[Completed]` or
    `[Canceled]`."""
    description, cut = clip(event.get("Description") or "")
    completed = description.startswith("[Completed]")
    canceled = description.startswith("[Canceled]")
    return {
        "id": event.get("InstanceEventId"),
        "code": event.get("Code"),
        "description": description or None,
        "notBefore": event.get("NotBefore"),
        "notAfter": event.get("NotAfter"),
        "notBeforeDeadline": event.get("NotBeforeDeadline"),
        "completed": completed,
        "canceled": canceled,
        "pending": not completed and not canceled,
        "truncated": cut,
    }


def status_facts(entry, target):
    """The `checks` output: the instance state, the system, instance and EBS checks, the
    scheduled events and the booleans they give."""
    system = summary_facts(entry.get("SystemStatus"))
    instance = summary_facts(entry.get("InstanceStatus"))
    ebs = summary_facts(entry.get("AttachedEbsStatus"))
    events = [event_facts(e) for e in (entry.get("Events") or []) if isinstance(e, dict)]
    return {
        "id": target.instance_id,
        "region": target.region,
        "availabilityZone": entry.get("AvailabilityZone"),
        "state": {"name": entry.get("State"), "code": entry.get("StateCode")},
        "checksApply": entry.get("State") == "running",
        "systemStatus": system,
        "instanceStatus": instance,
        "attachedEbsStatus": ebs,
        "systemStatusImpaired": impaired(system),
        "instanceStatusImpaired": impaired(instance),
        "attachedEbsStatusImpaired": impaired(ebs),
        "events": events[:MAX_LIST],
        "eventCount": len(events),
        "pendingEventCount": sum(1 for e in events if e["pending"]),
        "hasPendingEvents": any(e["pending"] for e in events),
    }
