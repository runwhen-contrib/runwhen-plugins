from aws_lambda_health.config import function_facts, read_config
from aws_lambda_health.target import lambda_target


def main(ctx):
    region, name = lambda_target(ctx.resource)
    config = read_config(ctx, region, name)
    return {"function": function_facts(config, region)}
