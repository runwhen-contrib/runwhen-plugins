from k8s_chaos.actions import fill_tmp


def main(ctx, size_mi=100, container=None, observe="10s", seed=None):
    return fill_tmp(ctx, size_mi=size_mi, container=container, observe=observe, seed=seed)
