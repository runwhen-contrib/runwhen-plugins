from k8s_chaos.actions import delete_pods


def main(ctx, count=1, wait_for_recovery=True, timeout="120s", seed=None):
    return delete_pods(
        ctx, count=count, wait_for_recovery=wait_for_recovery, timeout=timeout, seed=seed
    )
