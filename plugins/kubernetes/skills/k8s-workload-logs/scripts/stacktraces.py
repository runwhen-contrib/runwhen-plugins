from k8s_workload_logs.fetch import StreamReader, load_owned_pods
from k8s_workload_logs.logs import (
    Budget,
    bound,
    inspected,
    plan_streams,
    scanned_entry,
    split_lines,
)
from k8s_workload_logs.settings import read_settings
from k8s_workload_logs.traces import TraceCollector
from k8s_workload_logs.workload import since_seconds


def main(ctx, since="1h"):
    seconds = since_seconds(since)  # bad input fails before any kubectl call
    st = read_settings(ctx.settings)
    pods = load_owned_pods(ctx)
    plan = plan_streams(pods, exclude=st.exclude_containers, max_pods=st.max_pods)
    budget = Budget(st.scan_budget_seconds)
    reader = StreamReader(
        ctx,
        plan["streams"],
        tail=st.max_scan_lines,
        since=seconds,
        budget=budget,
        max_bytes=st.max_bytes_total,
    )
    collector, scanned = TraceCollector(), []
    for stream, text, size in reader:  # one raw text at a time
        lines = split_lines(text)
        scanned.append(scanned_entry(stream, lines, size, st.max_scan_lines))
        if not collector.add(stream, lines, budget):
            reader.stop_early()
            break
    reader.finish()
    traces, n_traces, o_traces = bound(collector.result(), st.max_items)
    scanned, n_scanned, o_scanned = bound(scanned, st.max_items)
    unavailable, n_unavailable, o_unavailable = bound(
        plan["unavailable"] + reader.unavailable(), st.max_items
    )
    return {
        "traces": traces,
        "scanned": scanned,
        "unavailable": unavailable,
        "inspected": inspected(
            plan,
            bytesRead=reader.bytes_read,
            tracesUngrouped=collector.ungrouped,
            total={"traces": n_traces, "scanned": n_scanned, "unavailable": n_unavailable},
            omitted={"traces": o_traces, "scanned": o_scanned, "unavailable": o_unavailable},
        ),
    }
