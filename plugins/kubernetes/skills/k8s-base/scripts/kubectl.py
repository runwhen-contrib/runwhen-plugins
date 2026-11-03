from k8s_base.k8s import run_kubectl
from k8s_base.kubectl_args import parse_command, truncate_output
from k8s_base.settings import read_settings

STDERR_CAP = 1024


def main(ctx, command):
    st = read_settings(ctx.settings)
    # the command is validated, and normalised, before kubectl starts
    argv = parse_command(
        command,
        allowed_verbs=st.allowed_verbs,
        denied_resources=st.denied_resources,
        allowed_flags=st.allowed_flags,
    )
    stdout, stderr = run_kubectl(ctx, argv)
    output, total, truncated = truncate_output(stdout, st.max_output_lines)
    return {
        "output": output,
        "run": {
            "argv": ["kubectl", *argv],
            "lines": total,
            "truncated": truncated,
            "stderr": stderr[:STDERR_CAP],
            "stderrTruncated": len(stderr) > STDERR_CAP,
        },
    }
