from k8s_workload_logs.fetch import fetch_streams, load_owned_pods
from k8s_workload_logs.logs import (
    inspected,
    plan_streams,
    scanned_index,
    split_lines,
    stacktraces,
    unavailable_with_gone,
)
from k8s_workload_logs.workload import cap, since_seconds


def main(ctx, since="1h"):
    seconds = since_seconds(since)  # bad input fails before any kubectl call
    pods = load_owned_pods(ctx)
    plan = plan_streams(
        pods, exclude=ctx.settings.exclude_containers, max_pods=ctx.settings.max_pods
    )
    scan = ctx.settings.max_scan_lines
    fetched, gone = fetch_streams(ctx, plan["streams"], tail=scan, since=seconds)
    return {
        "traces": cap(
            stacktraces([(s, split_lines(t)) for s, t in fetched]), ctx.settings.max_items
        ),
        "scanned": cap(scanned_index(fetched, scan), ctx.settings.max_items),
        "unavailable": cap(unavailable_with_gone(plan, gone), ctx.settings.max_items),
        "inspected": inspected(plan),
    }
