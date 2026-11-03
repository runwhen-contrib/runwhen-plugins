"""What the two tasks do: fetch through fetch.py, shape the facts with vault.py and csi.py."""

from k8s_vault_health.clock import reference_time
from k8s_vault_health.csi import daemonset_status, pod_views, warning_events
from k8s_vault_health.errors import RwError
from k8s_vault_health.fetch import (
    error_entry,
    load_pods,
    load_warning_events,
    load_workloads,
    target,
    vault_health,
)
from k8s_vault_health.vault import (
    askable,
    counts,
    health_facts,
    pod_entry,
    sort_entries,
    statefulset_view,
)
from k8s_vault_health.workload import (
    DAEMONSET,
    STATEFULSET,
    VAULT_CSI_LABEL,
    VAULT_SERVER_LABEL,
    cap,
    parse_duration,
)


def server_status(ctx, scheme, port, max_items):
    """(statefulsets, pods, counts). Every Running pod of every Vault StatefulSet is asked for
    /v1/sys/health through the pod proxy; one that cannot be asked is listed with its error code
    (the result is partial). If Vault could not be asked at all, the first failure is raised."""
    kind, resource = target(ctx)
    ns, workloads = load_workloads(ctx, kind, resource, STATEFULSET, VAULT_SERVER_LABEL)
    views, entries, failures = [], [], []
    for sts in workloads:
        views.append(statefulset_view(sts))
        for pod in load_pods(ctx, ns, sts):
            entry = pod_entry(sts["metadata"]["name"], pod)
            entries.append(entry)
            if not askable(pod):
                continue
            try:
                entry["health"] = health_facts(vault_health(ctx, ns, entry["pod"], scheme, port))
                entry["queried"] = True
            except RwError as exc:
                entry["error"] = error_entry(exc)
                failures.append((entry["pod"], exc))
    attempted = [e for e in entries if askable_entry(e)]
    if attempted and not any(e["queried"] for e in attempted):
        raise failures[0][1]
    for name, exc in failures:
        entry = error_entry(exc)
        ctx.error(
            entry["code"],
            f"pod {name}: {entry['message']}",
            scope=f"pod/{name}",
            hint=_hint(entry["code"]),
        )
    ordered = sort_entries(entries)
    return cap(views, max_items), cap(ordered, max_items), counts(entries)


def askable_entry(entry):
    """A pod entry that was asked or failed when asked (not one skipped as not Running)."""
    return entry["queried"] or entry["error"] is not None


def _hint(code):
    if code == "permission_denied":
        return "grant get on pods/proxy in the Vault namespace (the pod proxy subresource)"
    if code == "invalid_input":
        return "set the scheme and port inputs to what the Vault listener serves"
    return None


def csi_status(ctx, since, max_items):
    """(daemonsets, pods, events) of the Vault CSI provider DaemonSet(s)."""
    parse_duration(since)  # bad input fails before any kubectl call
    kind, resource = target(ctx)
    ns, workloads = load_workloads(ctx, kind, resource, DAEMONSET, VAULT_CSI_LABEL)
    views, pod_list, all_pods = [], [], []
    for ds in workloads:
        pods = load_pods(ctx, ns, ds)
        all_pods.extend(pods)
        views.append(daemonset_status(ds))
        pod_list.extend(pod_views(ds["metadata"]["name"], pods))
    events, _ok = load_warning_events(ctx, ns)
    rows = warning_events(events, workloads, all_pods, since, reference_time(ctx), max_items)
    pod_list.sort(key=lambda p: (p["ready"], -p["restarts"], p["pod"]))
    return cap(views, max_items), cap(pod_list, max_items), rows
