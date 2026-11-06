import time

from gcp_bucket_health.access import deadline_from, run
from gcp_bucket_health.settings import read_settings


def main(ctx):
    deadline = deadline_from(time.monotonic())
    return run(ctx, read_settings(ctx.settings, "public_bucket_threshold"), deadline)
