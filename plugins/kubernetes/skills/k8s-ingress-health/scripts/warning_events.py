from datetime import datetime, timezone

from k8s_ingress_health.fetch import fetch_services, load_ingress
from k8s_ingress_health.ingress import (
    backend_refs,
    parse_duration,
    service_names,
    warning_events,
)
from k8s_ingress_health.k8s import kubectl_json


def main(ctx, since="1h"):
    parse_duration(since)  # bad input fails before any kubectl call
    ingress = load_ingress(ctx)
    names = service_names(backend_refs(ingress))
    services = fetch_services(ctx, names)

    # The Ingress by uid; a Service by uid when it exists, else by name (an event
    # about a Service that was deleted is still worth seeing).
    subjects = [("Ingress", ingress["metadata"]["name"], ingress["metadata"].get("uid"))]
    for name in names:
        svc = services.get(name)
        subjects.append(("Service", name, ((svc or {}).get("metadata") or {}).get("uid")))

    events = kubectl_json(
        ctx, "-n", ctx.resource.namespace, "get", "events", "--field-selector", "type=Warning"
    )
    now = datetime.now(timezone.utc)
    return {
        "events": warning_events(events["items"], subjects, since, now, ctx.settings.max_items),
        "inspected": {
            "ingress": ingress["metadata"]["name"],
            "services": names,
            "servicesLookedUp": len(services) == len(names),
        },
    }
