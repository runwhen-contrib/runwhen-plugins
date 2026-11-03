from k8s_node_health.fetch import load_nodes, load_pods
from k8s_node_health.resources import overcommit as compute


def main(ctx):
    nodes = load_nodes(ctx)
    pods = load_pods(ctx)
    rows, inspected = compute(nodes, pods, ctx.settings.max_items, ctx.settings.pod_limit_percent)
    return {"nodes": rows, "inspected": inspected}
