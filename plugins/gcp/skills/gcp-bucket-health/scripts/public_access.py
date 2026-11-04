from gcp_bucket_health.access import run
from gcp_bucket_health.settings import read_settings


def main(ctx):
    return run(ctx, read_settings(ctx.settings, "public_bucket_threshold"))
