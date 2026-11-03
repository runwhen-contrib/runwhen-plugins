from k8s_pvc_health.fetch import load_mounting
from k8s_pvc_health.k8s import kubectl_json_scoped
from k8s_pvc_health.pvc import attachment_access, cap, volume_attachments


def main(ctx):
    claim, _pods, mounting = load_mounting(ctx)
    access = attachment_access(claim, mounting)
    attachments = None
    if access["volumeName"]:
        # Cluster-scoped list: forbidden leaves the pods and access facts intact.
        listed, _state = kubectl_json_scoped(
            ctx, "volumeattachments", "get", "volumeattachments.storage.k8s.io"
        )
        if listed is not None:
            attachments = cap(
                volume_attachments(listed["items"], access["volumeName"], access["nodes"]),
                ctx.settings.max_items,
            )
    else:
        attachments = []  # not bound: nothing to attach
    return {
        "access": access,
        "pods": cap(mounting, ctx.settings.max_items),
        "volumeAttachments": attachments,
    }
