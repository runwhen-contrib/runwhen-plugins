from k8s_pvc_health.fetch import load_mounting_optional
from k8s_pvc_health.k8s import kubectl_json_scoped
from k8s_pvc_health.pvc import attachment_access, cap, mounting_nodes, volume_attachments


def main(ctx):
    claim, mounting = load_mounting_optional(ctx)
    access = attachment_access(claim, mounting)
    attachments = None
    if access["volumeName"]:
        # Cluster-scoped list: forbidden leaves the pods and access facts intact.
        listed, _state = kubectl_json_scoped(
            ctx, "volumeattachments", "get", "volumeattachments.storage.k8s.io"
        )
        if listed is not None:
            nodes = (
                None if mounting is None else [n for part in mounting_nodes(mounting) for n in part]
            )
            attachments = cap(
                volume_attachments(listed["items"], access["volumeName"], nodes),
                ctx.settings.max_items,
            )
    else:
        attachments = []  # not bound: nothing to attach
    return {
        "access": access,
        "pods": None if mounting is None else cap(mounting, ctx.settings.max_items),
        "volumeAttachments": attachments,
    }
