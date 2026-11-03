from k8s_vault_health.check import csi_status


def main(ctx, since="1h"):
    daemonsets, pods, events = csi_status(ctx, since, ctx.settings.max_items)
    return {"daemonsets": daemonsets, "pods": pods, "events": events}
