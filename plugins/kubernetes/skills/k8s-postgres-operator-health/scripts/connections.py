from k8s_postgres_operator_health.operator import CONNECTIONS_SQL, connections_view
from k8s_postgres_operator_health.fetch import (
    find_primary,
    load_cluster,
    psql_document,
    queried,
    target_kind,
)


def main(ctx):
    kind = target_kind(ctx)
    load_cluster(ctx, kind)  # the target must exist
    pod, candidates = find_primary(ctx, kind, "psql")
    doc = psql_document(ctx, kind, pod, CONNECTIONS_SQL, "connections")
    return {
        "connections": connections_view(
            doc, queried(kind, pod), candidates, ctx.settings.connection_utilization_percent
        )
    }
