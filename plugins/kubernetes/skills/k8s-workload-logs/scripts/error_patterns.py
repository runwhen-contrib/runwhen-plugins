from k8s_workload_logs.fetch import StreamReader, load_owned_pods
from k8s_workload_logs.logs import (
    Budget,
    PatternScanner,
    bound,
    inspected,
    plan_streams,
    scanned_entry,
    sort_rows,
    split_lines,
)
from k8s_workload_logs.settings import read_settings
from k8s_workload_logs.workload import since_seconds


def main(ctx, since="1h"):
    seconds = since_seconds(since)  # bad input fails before any kubectl call
    st = read_settings(ctx.settings)
    scanner = PatternScanner(st.patterns, st.ignore)
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
    rows, scanned = [], []
    for stream, text, size in reader:  # one raw text at a time
        lines = split_lines(text)
        scanned.append(scanned_entry(stream, lines, size, st.max_scan_lines))
        found, complete = scanner.scan(stream, lines, budget)
        rows += found
        if not complete:
            reader.stop_early()
            break
    reader.finish()
    patterns, n_patterns, o_patterns = bound(sort_rows(rows), st.max_items)
    scanned, n_scanned, o_scanned = bound(scanned, st.max_items)
    unavailable, n_unavailable, o_unavailable = bound(
        plan["unavailable"] + reader.unavailable(), st.max_items
    )
    return {
        "patterns": patterns,
        "scanned": scanned,
        "unavailable": unavailable,
        "inspected": inspected(
            plan,
            bytesRead=reader.bytes_read,
            linesIgnored=scanner.ignored,
            total={"patterns": n_patterns, "scanned": n_scanned, "unavailable": n_unavailable},
            omitted={"patterns": o_patterns, "scanned": o_scanned, "unavailable": o_unavailable},
        ),
    }
