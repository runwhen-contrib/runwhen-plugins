from k8s_workload_ops.fetch import load_hpas, load_workload, target_kind
from k8s_workload_ops.k8s import kubectl_text
from k8s_workload_ops.workload import (
    SHORT,
    check_scale_input,
    hpas_targeting,
    scale_plan,
    target_view,
)


def main(ctx, replicas, override_hpa=False):
    kind = target_kind(ctx)
    check_scale_input(kind, replicas, ctx.settings.allow_scale_to_zero)
    ns = ctx.resource.namespace
    workload = load_workload(ctx, kind)
    all_hpas = load_hpas(ctx, override_hpa)
    hpas = None if all_hpas is None else hpas_targeting(kind, ctx.resource.name, all_hpas)
    plan = scale_plan(kind, workload, replicas, hpas, override_hpa)
    dry = bool(ctx.dry_run)
    if plan["changed"]:
        scale = ["-n", ns, "scale", f"{SHORT[kind]}/{ctx.resource.name}", f"--replicas={replicas}"]
        kubectl_text(ctx, *scale, *(["--dry-run=server"] if dry else []))
    return {
        "target": target_view(kind, workload),
        "scale": {"applied": plan["changed"] and not dry, **plan},
    }
