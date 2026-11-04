from k8s_seaweedfs_health.s3check import DEFAULT_BUCKET, run


def main(ctx, release="", bucket=DEFAULT_BUCKET):
    return run(ctx, release, bucket)
