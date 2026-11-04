from gcp_bucket_health.config import run
from gcp_bucket_health.settings import read_settings


def main(ctx):
    return run(ctx, read_settings(ctx.settings))
