from aws_lambda_health import metrics as mx
from aws_lambda_health.config import read_config
from aws_lambda_health.errors import RwError
from aws_lambda_health.target import lambda_target, read_settings, read_since


def main(ctx, since="1h"):
    seconds = read_since(since)
    st = read_settings(ctx.settings)
    region, name = lambda_target(ctx.resource)
    # the function itself first: CloudWatch answers a function that does not exist with no
    # datapoints, which would read as "never invoked". A missing function is not_found here.
    config = read_config(ctx, region, name)
    start, end = mx.window(ctx.now(), seconds)
    period = mx.period_for(seconds)
    points, failures = {}, []
    for metric, statistics in mx.METRICS:
        try:
            points[metric] = mx.read_metric(
                ctx, region, name, metric, statistics, start, end, period
            )
        except RwError as exc:
            points[metric] = None
            failures.append((metric, exc))
    if len(failures) == len(mx.METRICS):
        raise failures[0][1]  # nothing was read: the failure is the result
    for metric, exc in failures:
        ctx.error(exc.code, f"{metric}: {exc.message}", scope=metric)
    return {"metrics": mx.summarize(points, start, end, period, config.get("Timeout"), st)}
