from k8s_chaos.actions import mangle_service


def main(ctx, mode="selector", revert_after="60s", service=None):
    return mangle_service(ctx, mode=mode, revert_after=revert_after, service=service)
