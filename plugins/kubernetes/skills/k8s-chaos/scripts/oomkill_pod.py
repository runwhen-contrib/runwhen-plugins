from k8s_chaos.actions import oomkill_pod


def main(ctx, container=None, timeout="30s", seed=None, include_uncontrolled=False):
    return oomkill_pod(
        ctx,
        container=container,
        timeout=timeout,
        seed=seed,
        include_uncontrolled=include_uncontrolled,
    )
