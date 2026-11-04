from k8s_litellm_health import service
from k8s_litellm_health.fetch import load_workload


def main(ctx, service_port=service.DEFAULT_SERVICE_PORT):
    expected = service.check_service_port(service_port)  # bad input fails before any kubectl call
    workload = load_workload(ctx)
    return {"service": service.view(ctx, workload, expected, ctx.settings.max_items)}
