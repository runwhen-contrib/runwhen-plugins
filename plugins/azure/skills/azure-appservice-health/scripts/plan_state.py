from azure_appservice_health.common import read_settings
from azure_appservice_health.plan import plan_state, plan_target, read_plan
from azure_appservice_health.target import PLAN, WEB_APP, target


def main(ctx):
    read_settings(ctx.settings)
    plan, _ = plan_target(ctx, target(ctx.resource, (WEB_APP, PLAN)))
    return {"planState": plan_state(read_plan(ctx, plan))}
