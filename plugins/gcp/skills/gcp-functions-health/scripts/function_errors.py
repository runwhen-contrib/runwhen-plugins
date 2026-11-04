from gcp_functions_health import fnlist
from gcp_functions_health import logs as lg
from gcp_functions_health.errors import RwError
from gcp_functions_health.function import service_name
from gcp_functions_health.locate import locate
from gcp_functions_health.target import function_target, read_settings, read_since


def main(ctx, since="1h"):
    seconds = read_since(since)
    st = read_settings(ctx.settings)
    target = function_target(ctx.resource)
    # the function first: it names the log resource (its generation decides which), and a function
    # that does not exist is not_found here instead of looking like a log with no errors
    row = locate(ctx, target, st.command_timeout)
    _, region, name = fnlist.parse_name(row)
    generation = fnlist.generation(row)
    if generation is None:
        raise RwError("internal", "the function's generation is unknown: its log cannot be chosen")
    flt = lg.log_filter(generation, name, region, service_name(row))
    start, end = lg.window(ctx.now(), seconds)
    rows = lg.read_entries(
        ctx, target.project, flt, seconds, st.max_log_entries, st.command_timeout
    )
    return {"logs": lg.log_facts(flt, start, end, rows, st.max_log_entries)}
