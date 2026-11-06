from k8s_flux_health.fetch import set_suspend


def main(ctx):
    return set_suspend(ctx, False)
