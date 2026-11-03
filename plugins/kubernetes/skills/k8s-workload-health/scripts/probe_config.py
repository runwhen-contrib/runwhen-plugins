from fetch import load_workload
from workload import cap, probe_config


def main(ctx):
    _kind, workload = load_workload(ctx)
    return {"probes": cap(probe_config(workload["spec"]["template"]), ctx.settings.max_items)}
