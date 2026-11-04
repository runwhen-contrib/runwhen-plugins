import time

from gcp_functions_health import fnlist
from gcp_functions_health import metrics as mx
from gcp_functions_health.errors import RwError
from gcp_functions_health.function import service_name, timeout_seconds
from gcp_functions_health.locate import locate
from gcp_functions_health.monitoring import Reader, access_token
from gcp_functions_health.target import function_target, read_settings, read_since


def main(ctx, since="1h"):
    deadline = mx.deadline_from(time.monotonic())
    seconds = read_since(since)
    st = read_settings(ctx.settings)
    target = function_target(ctx.resource)
    # the function first: Monitoring answers a function that does not exist with no series, which
    # would read as "never ran"; a missing function is not_found here
    row = locate(ctx, target, st.command_timeout)
    _, region, name = fnlist.parse_name(row)
    generation = fnlist.generation(row)
    spec = mx.spec_for(generation)
    series_name = (service_name(row) or name.lower()) if generation == 2 else name
    start, end = mx.window(ctx.now(), seconds)
    token = access_token(ctx, st.command_timeout)
    reader = Reader(ctx, token, target.project, st.command_timeout, deadline)
    results, failures = {}, []
    try:
        for query, metric, aggregation in mx.queries(spec, seconds):
            flt = mx.metric_filter(spec, metric, series_name, region)
            try:
                results[query] = reader.series(mx.iso(start), mx.iso(end), flt, aggregation)
            except RwError as exc:
                results[query] = None
                failures.append((query, exc))
    finally:
        reader.close()
    if len(failures) == len(results):
        raise failures[0][1]  # nothing was read: the failure is the result
    for query, exc in failures:
        ctx.error(exc.code, f"{query}: {exc.message}", scope=query)
    return {
        "metrics": mx.summarize(spec, generation, results, start, end, timeout_seconds(row), st)
    }
