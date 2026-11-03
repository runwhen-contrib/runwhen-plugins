from k8s_postgres_operator_health.operator import SETTINGS_SQL, settings_view
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
    pod, _ = find_primary(ctx, kind, "psql")
    doc = psql_document(ctx, kind, pod, SETTINGS_SQL, "settings")
    return {"settings": settings_view(doc, queried(kind, pod), ctx.settings.max_items)}
