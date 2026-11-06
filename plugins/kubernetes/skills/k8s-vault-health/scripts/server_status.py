from k8s_vault_health.check import server_status


def main(ctx, scheme="http", port=8200):
    statefulsets, pods, counts = server_status(ctx, scheme, port, ctx.settings.max_items)
    return {"statefulsets": statefulsets, "pods": pods, "counts": counts}
