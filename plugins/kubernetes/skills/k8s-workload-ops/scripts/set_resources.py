from k8s_workload_ops.fetch import load_hpas, load_workload, target_kind
from k8s_workload_ops.k8s import kubectl_json
from k8s_workload_ops.workload import (
    NESTED_LIMIT,
    SHORT,
    check_resource_input,
    hpas_targeting,
    resource_plan,
    resources_after,
    target_view,
)


def main(
    ctx, container=None, cpu_request=None, cpu_limit=None, memory_request=None, memory_limit=None
):
    kind = target_kind(ctx)
    wanted = check_resource_input(
        {
            "cpuRequest": cpu_request,
            "cpuLimit": cpu_limit,
            "memoryRequest": memory_request,
            "memoryLimit": memory_limit,
        }
    )
    workload = load_workload(ctx, kind)
    plan = resource_plan(workload, container, wanted)
    dry = bool(ctx.dry_run)
    fields = plan["fields"]
    # An autoscaler measures utilisation against the requests: a fact for the approver. A failed
    # list is reported (hpaPresent null) and does not block the change.
    all_hpas = load_hpas(ctx, True)
    hpas = None if all_hpas is None else hpas_targeting(kind, ctx.resource.name, all_hpas)
    if any(f["differs"] for f in fields):
        args = [
            "-n",
            ctx.resource.namespace,
            "set",
            "resources",
            f"{SHORT[kind]}/{ctx.resource.name}",
            f"--containers={plan['container']}",
        ]
        if plan["requests"]:
            args.append(f"--requests={plan['requests']}")
        if plan["limits"]:
            args.append(f"--limits={plan['limits']}")
        if dry:
            args.append("--dry-run=server")
        # -o json: kubectl prints the object the server holds after the change
        fields = resources_after(kubectl_json(ctx, *args), plan)
    differs = any(f["differs"] for f in fields)
    return {
        "target": target_view(kind, workload),
        "resources": {
            "applied": differs and not dry,
            "changed": differs and not dry,
            "wouldChange": differs and dry,
            "container": plan["container"],
            "init": plan["init"],
            "hpaPresent": None if hpas is None else bool(hpas),
            "hpaNames": [h["name"] for h in (hpas or [])][:NESTED_LIMIT],
            "fields": fields,
        },
    }
