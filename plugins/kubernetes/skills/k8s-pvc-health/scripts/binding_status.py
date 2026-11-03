from k8s_pvc_health.clock import reference_time
from k8s_pvc_health.fetch import load_claim
from k8s_pvc_health.k8s import kubectl_json_scoped
from k8s_pvc_health.pvc import (
    claim_events,
    claim_view,
    parse_duration,
    storage_class_view,
    volume_view,
)


def main(ctx, since="1h"):
    parse_duration(since)  # bad input fails before any kubectl call
    ns = ctx.resource.namespace
    claim = load_claim(ctx)
    view = claim_view(claim)

    # Cluster-scoped reads: a namespace-scoped identity is forbidden here and
    # still gets the claim and its events.
    sc, sc_state = (None, "ok")
    if view["storageClassName"]:
        sc, sc_state = kubectl_json_scoped(
            ctx, "storageclasses", "get", "storageclass", view["storageClassName"], missing_ok=True
        )
    pv, pv_state = (None, "ok")
    if view["volumeName"]:
        pv, pv_state = kubectl_json_scoped(
            ctx, "persistentvolumes", "get", "persistentvolume", view["volumeName"], missing_ok=True
        )

    events, events_state = kubectl_json_scoped(
        ctx,
        "events",
        "-n",
        ns,
        "get",
        "events",
        "--field-selector",
        f"involvedObject.kind=PersistentVolumeClaim,involvedObject.name={ctx.resource.name},"
        f"involvedObject.uid={claim['metadata']['uid']}",
    )
    now = reference_time(ctx)
    return {
        "claim": view,
        "storageClass": storage_class_view(view["storageClassName"], sc, sc_state != "denied"),
        "volume": volume_view(claim, pv, pv_state != "denied"),
        "events": None
        if events is None
        else claim_events(events["items"], claim, since, now, ctx.settings.max_items),
    }
