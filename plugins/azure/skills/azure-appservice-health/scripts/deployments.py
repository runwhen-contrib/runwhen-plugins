from azure_appservice_health.common import read_settings
from azure_appservice_health.common import window as parse_window
from azure_appservice_health.errors import RwError
from azure_appservice_health.site import (
    app_state,
    deployments,
    read_deployments,
    read_site,
    read_slots,
    slots,
)
from azure_appservice_health.target import WEB_APP, target


def main(ctx, window="7d"):
    st = read_settings(ctx.settings)
    seconds = parse_window(window)
    t = target(ctx.resource, (WEB_APP,))
    state = app_state(read_site(ctx, t))
    out = {
        "app": t.name,
        "productionState": state["state"],
        "windowSeconds": int(seconds),
        "slots": None,
        "deployments": None,
    }
    failed = []
    try:
        rows, complete = read_slots(ctx, t)
        out["slots"] = slots(rows, st.max_items)
        out["slots"]["complete"] = complete
    except RwError as exc:
        failed.append(("slots", exc))
    try:
        rows, complete = read_deployments(ctx, t)
        out["deployments"] = deployments(rows, complete, ctx.now(), seconds, st.max_items)
    except RwError as exc:
        failed.append(("deployments", exc))
    if len(failed) == 2:
        raise failed[0][1]
    for scope, exc in failed:
        ctx.error(exc.code, exc.message, scope=scope)
    return {"deployments": out}
