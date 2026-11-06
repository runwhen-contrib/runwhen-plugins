from k8s_node_health.clock import reference_time
from k8s_node_health.fetch import load_nodes
from k8s_node_health.nodes import node_conditions


def main(ctx):
    nodes = load_nodes(ctx)
    views, counts = node_conditions(nodes, reference_time(ctx), ctx.settings.max_items)
    return {"nodes": views, "counts": counts}
