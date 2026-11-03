from k8s_pvc_health.fetch import load_mounting
from k8s_pvc_health.k8s import run_checked
from k8s_pvc_health.pvc import parse_df, pick_usage_target, usage_view


def main(ctx):
    _claim, pods, mounting = load_mounting(ctx)
    objects = {p["metadata"]["name"]: p for p in pods}
    target = pick_usage_target(mounting, objects)
    if target is None:
        ctx.skip("no running pod mounts this claim")
    pod, container, mount_path = target
    # A forbidden exec, or a container without df, fails this task here.
    result = run_checked(
        ctx,
        [
            "kubectl",
            "-n",
            ctx.resource.namespace,
            "exec",
            pod,
            "-c",
            container,
            "--",
            "df",
            "-Pk",
            mount_path,
        ],
    )
    running = sum(1 for p in mounting if p["phase"] == "Running" and not p["terminating"])
    return {
        "usage": usage_view(
            parse_df(result.stdout),
            pod,
            container,
            mount_path,
            running,
            ctx.settings.usage_threshold_percent,
        )
    }
