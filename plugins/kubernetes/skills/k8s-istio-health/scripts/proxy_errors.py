from k8s_istio_health.common import Budget, bound, check_int_setting, since_seconds
from k8s_istio_health.fetch import load_deployment, owned_pods
from k8s_istio_health.k8s import kubectl_text
from k8s_istio_health.errors import RwError
from k8s_istio_health.proxy import (
    LIMIT_BYTES,
    log_file_missing,
    order_pods,
    scan_pod,
    sort_results,
    split_lines,
)
from k8s_istio_health.sidecar import SIDECAR, find_sidecar, sidecar_status, waiting_reason

LOG_CALL_TIMEOUT = 20  # seconds for one `kubectl logs`


def main(ctx, since="1h"):
    seconds = since_seconds(since)  # bad input fails before any kubectl call
    max_items = check_int_setting("maxItems", ctx.settings.max_items, 1, 10000)
    max_pods = check_int_setting("maxPods", ctx.settings.max_pods, 1, 500)
    scan_lines = check_int_setting("maxScanLines", ctx.settings.max_scan_lines, 1, 100000)
    budget = Budget(
        check_int_setting("scanBudgetSeconds", ctx.settings.scan_budget_seconds, 1, 600)
    )
    dep = load_deployment(ctx)
    pods = owned_pods(ctx, dep)

    unavailable, readable = [], []
    for pod in order_pods(pods):
        container, native = find_sidecar(pod)
        name = pod["metadata"]["name"]
        if container is None:
            unavailable.append(
                {"pod": name, "container": SIDECAR, "reason": "pod has no istio-proxy container"}
            )
            continue
        status = sidecar_status(pod, native)
        if status is None:  # the kubelet has reported nothing for it yet: no log to ask for
            unavailable.append(
                {"pod": name, "container": SIDECAR, "reason": "container has not started"}
            )
            continue
        waiting = waiting_reason(status)
        if waiting:
            unavailable.append(
                {"pod": name, "container": SIDECAR, "reason": f"container is waiting: {waiting}"}
            )
            continue
        readable.append((name, native))
    attempted = readable[:max_pods]

    rows, failures, out_of_time = [], [], 0
    for index, (name, native) in enumerate(attempted):
        if budget.expired():
            out_of_time = len(attempted) - index
            break
        try:
            text = kubectl_text(
                ctx,
                "-n",
                ctx.resource.namespace,
                "logs",
                name,
                "-c",
                SIDECAR,
                f"--since={seconds}s",
                f"--tail={scan_lines}",
                f"--limit-bytes={LIMIT_BYTES}",
                timeout=LOG_CALL_TIMEOUT,
            )
        except RwError as exc:
            failures.append((name, exc))
            continue
        if log_file_missing(text):
            unavailable.append(
                {
                    "pod": name,
                    "container": SIDECAR,
                    "reason": "container log file no longer exists on the node",
                }
            )
            continue
        size = len(text.encode("utf-8", "replace"))
        rows.append(scan_pod(name, SIDECAR, native, split_lines(text), size, scan_lines))
    if failures and not rows and not out_of_time and len(failures) == len(attempted):
        raise failures[0][1]  # nothing could be read: fail with the cause, never an empty list
    for name, exc in failures:
        ctx.error(
            exc.code, f"kubectl logs failed for {name}/{SIDECAR}: {exc.message}"[:1024], scope=name
        )

    if out_of_time:
        ctx.error(
            "timeout",
            f"scan budget exhausted: {out_of_time} pod(s) not read; counts are partial",
            scope="scan",
        )
    proxies, n_proxies, o_proxies = bound(sort_results(rows), max_items)
    unavailable, n_unavailable, o_unavailable = bound(unavailable, max_items)
    return {
        "proxies": proxies,
        "unavailable": unavailable,
        "inspected": {
            "pods": len(pods),
            "podsRead": len(rows),
            "podsNotRead": len(readable) - len(attempted) + out_of_time,
            "sinceSeconds": seconds,
            "total": {"proxies": n_proxies, "unavailable": n_unavailable},
            "omitted": {"proxies": o_proxies, "unavailable": o_unavailable},
        },
    }
