from k8s_workload_health.fetch import load_workload
from k8s_workload_health.workload import cap, probe_config


def main(ctx):
    _kind, workload = load_workload(ctx)
    return {"probes": cap(probe_config(workload["spec"]["template"]), ctx.settings.max_items)}
