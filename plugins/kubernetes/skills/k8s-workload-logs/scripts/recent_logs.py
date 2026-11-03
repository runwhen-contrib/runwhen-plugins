from k8s_workload_logs.errors import RwError
from k8s_workload_logs.fetch import StreamReader, load_owned_pods
from k8s_workload_logs.logs import (
    Budget,
    bound,
    inspected,
    plan_streams,
    recent_logs,
    reduce_stream,
    render_section,
)
from k8s_workload_logs.settings import read_settings


def main(ctx, tail_lines=200, container=None):
    if (
        isinstance(tail_lines, bool)
        or not isinstance(tail_lines, int)
        or not 1 <= tail_lines <= 5000
    ):
        raise RwError(
            "invalid_input", f"tailLines must be an integer from 1 to 5000, got {tail_lines!r}"
        )
    st = read_settings(ctx.settings)
    pods = load_owned_pods(ctx)
    plan = plan_streams(pods, only=container, max_pods=st.max_pods)
    reader = StreamReader(
        ctx,
        plan["streams"],
        tail=tail_lines,
        budget=Budget(st.scan_budget_seconds),
        max_bytes=st.max_bytes_total,
    )
    reduced = [reduce_stream(s, text, size) for s, text, size in reader]  # one raw text at a time
    reader.finish()
    sections, index = recent_logs(reduced, st.max_lines)
    with ctx.artifacts.open("logs", "text/plain") as out:
        for stream, lines in sections:
            out.write(render_section(stream, lines).encode("utf-8"))
    streams, n_streams, o_streams = bound(index, st.max_items)
    unavailable, n_unavailable, o_unavailable = bound(
        plan["unavailable"] + reader.unavailable(), st.max_items
    )
    return {
        "logs": out.ref,
        "streams": streams,
        "unavailable": unavailable,
        "inspected": inspected(
            plan,
            bytesRead=reader.bytes_read,
            total={"streams": n_streams, "unavailable": n_unavailable},
            omitted={"streams": o_streams, "unavailable": o_unavailable},
        ),
    }
