from k8s_workload_health.fetch import load_workload
from k8s_workload_health.k8s import kubectl_json
from k8s_workload_health.workload import cap, hpa_status


def main(ctx):
    kind, workload = load_workload(ctx)
    hpas = kubectl_json(
        ctx, "-n", ctx.resource.namespace, "get", "horizontalpodautoscalers.v2.autoscaling"
    )["items"]
    found = hpa_status(hpas, kind, ctx.resource.name, workload["spec"]["template"])
    return {"hpas": cap(found, ctx.settings.max_items)}
