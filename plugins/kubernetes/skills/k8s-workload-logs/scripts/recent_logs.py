from k8s_workload_logs.errors import RwError
from k8s_workload_logs.fetch import fetch_streams, load_owned_pods
from k8s_workload_logs.logs import (
    inspected,
    plan_streams,
    recent_logs,
    render_section,
    unavailable_with_gone,
)
from k8s_workload_logs.workload import cap


def main(ctx, tail_lines=200, container=None):
    if not isinstance(tail_lines, int) or not 1 <= tail_lines <= 5000:
        raise RwError(
            "invalid_input", f"tailLines must be an integer from 1 to 5000, got {tail_lines!r}"
        )
    pods = load_owned_pods(ctx)
    plan = plan_streams(pods, only=container, max_pods=ctx.settings.max_pods)
    fetched, gone = fetch_streams(ctx, plan["streams"], tail=tail_lines)
    sections, index = recent_logs(fetched, ctx.settings.max_lines)
    with ctx.artifacts.open("logs", "text/plain") as out:
        for stream, lines in sections:
            out.write(render_section(stream, lines).encode("utf-8"))
    return {
        "logs": out.ref,
        "streams": cap(index, ctx.settings.max_items),
        "unavailable": cap(unavailable_with_gone(plan, gone), ctx.settings.max_items),
        "inspected": inspected(plan),
    }
