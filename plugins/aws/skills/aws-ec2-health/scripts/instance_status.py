from aws_ec2_health import status as st
from aws_ec2_health.target import ec2_target


def main(ctx):
    target = ec2_target(ctx.resource)
    return {"checks": st.status_facts(st.read_status(ctx, target), target)}
