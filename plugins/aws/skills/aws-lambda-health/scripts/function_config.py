from aws_lambda_health import config as cf
from aws_lambda_health.errors import RwError
from aws_lambda_health.target import lambda_target


def main(ctx):
    target = lambda_target(ctx.resource)
    config = cf.read_config(ctx, target)
    # the two concurrency reads are secondary: forbidden or failing, the configuration stands and
    # the field is null beside an error scoped to it (a null is "not read", never "none set")
    reserved = provisioned = None
    try:
        reserved = cf.read_reserved_concurrency(ctx, target)
    except RwError as exc:
        ctx.error(exc.code, f"reserved concurrency: {exc.message}", scope="reservedConcurrency")
    try:
        provisioned = cf.read_provisioned_concurrency(ctx, target)
    except RwError as exc:
        ctx.error(
            exc.code, f"provisioned concurrency: {exc.message}", scope="provisionedConcurrency"
        )
    return {"function": cf.function_facts(config, target.region, reserved, provisioned)}
