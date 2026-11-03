from k8s_istio_health.common import bound, check_int_setting, since_seconds
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
        waiting = waiting_reason(sidecar_status(pod, native))
        if waiting:
            unavailable.append(
                {"pod": name, "container": SIDECAR, "reason": f"container is waiting: {waiting}"}
            )
            continue
        readable.append((name, native))
    attempted = readable[:max_pods]

    rows, failures = [], []
    for name, native in attempted:
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
    if failures and not rows and len(failures) == len(attempted):
        raise failures[0][1]  # nothing could be read: fail with the cause, never an empty list
    for name, exc in failures:
        ctx.error(
            exc.code, f"kubectl logs failed for {name}/{SIDECAR}: {exc.message}"[:1024], scope=name
        )

    proxies, n_proxies, o_proxies = bound(sort_results(rows), max_items)
    unavailable, n_unavailable, o_unavailable = bound(unavailable, max_items)
    return {
        "proxies": proxies,
        "unavailable": unavailable,
        "inspected": {
            "pods": len(pods),
            "podsRead": len(rows),
            "podsNotRead": len(readable) - len(attempted),
            "sinceSeconds": seconds,
            "total": {"proxies": n_proxies, "unavailable": n_unavailable},
            "omitted": {"proxies": o_proxies, "unavailable": o_unavailable},
        },
    }
