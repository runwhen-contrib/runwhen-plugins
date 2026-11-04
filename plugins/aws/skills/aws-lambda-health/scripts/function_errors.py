from aws_lambda_health import logs as lg
from aws_lambda_health.config import log_group, read_config
from aws_lambda_health.errors import RwError
from aws_lambda_health.metrics import window
from aws_lambda_health.target import lambda_target, read_settings, read_since


def main(ctx, since="1h"):
    seconds = read_since(since)
    st = read_settings(ctx.settings)
    target = lambda_target(ctx.resource)
    # the function first: its configuration names its log group, and a function that does not
    # exist is not_found here instead of looking like a log group with no errors
    group = log_group(read_config(ctx, target), target.name)
    pattern = st.error_pattern or lg.DEFAULT_PATTERN
    start, end = window(ctx.now(), seconds)
    try:
        events, truncated, timed_out = lg.read_events(
            ctx, target.region, group, start, end, pattern, st.max_log_events
        )
    except RwError as exc:
        # the function exists; its log group is missing (never invoked, or deleted) or unreadable
        if exc.code not in ("not_found", "permission_denied"):
            raise
        ctx.error(exc.code, exc.message, scope="logs")
        return {
            "logs": lg.log_facts(
                group,
                None if exc.code == "permission_denied" else False,
                start,
                end,
                pattern,
                [],
                False,
            )
        }
    if timed_out:
        # a later page timed out: the matches found so far stand, and the list says it is cut
        ctx.error(
            "timeout",
            f"{timed_out.message}; the {len(events)} matching events read before it are returned",
            scope="logs",
        )
    return {"logs": lg.log_facts(group, True, start, end, pattern, events, truncated)}
