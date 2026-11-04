import time

from aws_ec2_health import instance as inst
from aws_ec2_health import metrics as mx
from aws_ec2_health.target import ec2_target, read_settings, read_since


def main(ctx, since="1h"):
    started = time.monotonic()
    seconds = read_since(since)
    st = read_settings(ctx.settings)
    target = ec2_target(ctx.resource)
    # the instance first: CloudWatch answers an instance that does not exist (or is another
    # account's) with no datapoints, which would read as "idle". A missing instance is not_found
    # here; its type says whether it has CPU credits and its state why a stopped one has no data.
    config = inst.instance_facts(inst.read_instance(ctx, target), target)
    start, end = mx.window(ctx.now(), seconds)
    period = mx.period_for(seconds)
    points, failures = mx.read_all(ctx, target, config["burstable"], start, end, period, started)
    if len(failures) == len(points):
        raise failures[0][1]  # nothing was read: the failure is the result
    for metric, exc in failures:
        ctx.error(exc.code, f"{metric}: {exc.message}", scope=metric)
    return {"metrics": mx.summarize(points, start, end, period, config, st)}
