from k8s_node_health.fetch import load_node_metrics, load_nodes
from k8s_node_health.resources import utilization


def main(ctx):
    nodes = load_nodes(ctx)
    metrics = load_node_metrics(ctx)
    rows, inspected = utilization(
        nodes, metrics, ctx.settings.utilization_percent, ctx.settings.max_items
    )
    return {"nodes": rows, "inspected": inspected}
