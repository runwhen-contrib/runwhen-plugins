from azure_appservice_health.common import read_settings
from azure_appservice_health.plan import (
    autoscale,
    plan_state,
    plan_target,
    read_autoscale,
    read_plan,
)
from azure_appservice_health.target import PLAN, WEB_APP, target


def main(ctx):
    st = read_settings(ctx.settings)
    plan, _ = plan_target(ctx, target(ctx.resource, (WEB_APP, PLAN)))
    instances = plan_state(read_plan(ctx, plan))["instanceCount"]
    rows, complete = read_autoscale(ctx, plan)
    return {"autoscale": autoscale(rows, complete, plan, instances, st.max_items)}
