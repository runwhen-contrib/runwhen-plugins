from k8s_workload_logs.fetch import fetch_streams, load_owned_pods
from k8s_workload_logs.logs import (
    compile_patterns,
    error_patterns,
    inspected,
    plan_streams,
    scanned_index,
    split_lines,
    unavailable_with_gone,
)
from k8s_workload_logs.workload import cap, since_seconds


def main(ctx, since="1h"):
    seconds = since_seconds(since)  # bad input fails before any kubectl call
    compile_patterns(ctx.settings.patterns)
    compile_patterns({f"ignore[{i}]": rx for i, rx in enumerate(ctx.settings.ignore)})
    pods = load_owned_pods(ctx)
    plan = plan_streams(
        pods, exclude=ctx.settings.exclude_containers, max_pods=ctx.settings.max_pods
    )
    scan = ctx.settings.max_scan_lines
    fetched, gone = fetch_streams(ctx, plan["streams"], tail=scan, since=seconds)
    rows, ignored = error_patterns(
        [(s, split_lines(t)) for s, t in fetched], ctx.settings.patterns, ctx.settings.ignore
    )
    return {
        "patterns": cap(rows, ctx.settings.max_items),
        "scanned": cap(scanned_index(fetched, scan), ctx.settings.max_items),
        "unavailable": cap(unavailable_with_gone(plan, gone), ctx.settings.max_items),
        "inspected": {**inspected(plan), "linesIgnored": ignored},
    }
