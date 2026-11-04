from aws_ec2_health import instance as inst
from aws_ec2_health.target import ec2_target


def main(ctx):
    target = ec2_target(ctx.resource)
    return {"instance": inst.instance_facts(inst.read_instance(ctx, target), target)}
