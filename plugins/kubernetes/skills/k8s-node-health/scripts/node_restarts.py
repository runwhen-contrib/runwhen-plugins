from datetime import datetime, timezone

from k8s_node_health.common import cap, parse_duration
from k8s_node_health.fetch import load_node_events, load_nodes
from k8s_node_health.nodes import node_events, ready_transitions


def main(ctx, since="1h"):
    parse_duration(since)  # bad input fails before any kubectl call
    nodes = load_nodes(ctx)
    now = datetime.now(timezone.utc)
    events = load_node_events(ctx)
    return {
        "transitions": cap(ready_transitions(nodes, now, since), ctx.settings.max_items),
        "events": None
        if events is None
        else node_events(events, now, since, ctx.settings.max_items),
    }
