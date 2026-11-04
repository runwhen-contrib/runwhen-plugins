import time

from gcp_functions_health.build import build_ref, read_build
from gcp_functions_health.errors import RwError
from gcp_functions_health.fnlist import parse_name
from gcp_functions_health.function import function_facts
from gcp_functions_health.locate import locate
from gcp_functions_health.target import RESERVE, TASK_BUDGET, function_target, read_settings


def main(ctx):
    deadline = time.monotonic() + TASK_BUDGET - RESERVE
    st = read_settings(ctx.settings)
    target = function_target(ctx.resource)
    row = locate(ctx, target, st.command_timeout, deadline)
    ref = build_ref(row, parse_name(row)[1])
    build = deployment = None
    if ref:
        deployment = {"buildId": ref[0], "buildLocation": ref[1]}
        # the build is secondary: the function's state stands when it cannot be read (it may have
        # expired, or this identity may not read Cloud Build); build is then null beside a scoped error
        try:
            build = read_build(ctx, target.project, ref, st.command_timeout, deadline)
        except RwError as exc:
            ctx.error(exc.code, f"build {ref[0]}: {exc.message}", scope="build")
    return {"function": function_facts(row, build, deployment)}
