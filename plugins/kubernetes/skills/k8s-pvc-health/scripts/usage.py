from k8s_pvc_health.errors import RwError
from k8s_pvc_health.fetch import load_mounting
from k8s_pvc_health.k8s import run_checked
from k8s_pvc_health.pvc import parse_df, pick_usage_target, usage_view


def main(ctx):
    claim, pods, mounting = load_mounting(ctx)
    if (claim.get("spec") or {}).get("volumeMode") == "Block":
        ctx.skip("block-mode claim has no filesystem to measure")
    objects = {p["metadata"]["name"]: p for p in pods}
    target = pick_usage_target(mounting, objects)
    if target is None:
        ctx.skip("no running pod mounts this claim")
    pod, container, mount_path = target
    # A forbidden exec, or a container without df, fails this task here.
    argv = [
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
    ]
    try:
        result = run_checked(ctx, argv)
    except RwError as exc:
        if exc.code == "not_found":
            # not_found means "the claim is missing"; the claim was just read.
            raise RwError("internal", "pod or container went away before exec") from None
        raise
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
