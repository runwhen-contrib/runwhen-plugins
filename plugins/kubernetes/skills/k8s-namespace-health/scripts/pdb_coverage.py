from k8s_namespace_health.fetch import check_settings, list_items, list_secondary, load_namespace
from k8s_namespace_health.pdb import pdb_coverage


def main(ctx):
    check_settings(ctx, "max_items")
    namespace = load_namespace(ctx)
    pdbs = list_items(ctx, namespace, "poddisruptionbudgets.policy")
    deployments = list_secondary(ctx, namespace, "deployments.apps")
    statefulsets = list_secondary(ctx, namespace, "statefulsets.apps")
    uncovered, zero, inspected = pdb_coverage(
        deployments, statefulsets, pdbs, ctx.settings.max_items
    )
    return {"uncovered": uncovered, "pdbs": zero, "inspected": inspected}
