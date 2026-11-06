from k8s_base.k8s import run_kubectl
from k8s_base.kubectl_args import LOGS_LIMIT_BYTES, parse_command, truncate_output
from k8s_base.redact import redact_counted
from k8s_base.resources import parse_api_resources
from k8s_base.settings import read_settings

STDERR_CAP = 1024


def main(ctx, command):
    st = read_settings(ctx.settings)

    def api_resources():
        # only for a deniedResources name outside the built-in table (a CRD); fails closed
        stdout, _ = run_kubectl(ctx, ["api-resources", "-o", "wide"])
        return parse_api_resources(stdout)

    # the command is validated, and normalised, before it runs
    argv = parse_command(
        command,
        allowed_verbs=st.allowed_verbs,
        denied_resources=st.denied_resources,
        allowed_flags=st.allowed_flags,
        max_output_lines=st.max_output_lines,
        resolver=api_resources,
    )
    stdout, stderr = run_kubectl(ctx, argv)
    is_logs = argv[0] == "logs"
    # redact before cutting, so a credential block is never left half-visible
    stdout, redactions = redact_counted(stdout)
    stderr, redacted_err = redact_counted(stderr)
    output, total, truncated = truncate_output(stdout, st.max_output_lines, from_end=is_logs)
    if is_logs and len(stdout.encode("utf-8")) >= LOGS_LIMIT_BYTES:
        truncated = True  # kubectl stopped at --limit-bytes: older or newer lines were not read
    return {
        "output": output,
        "run": {
            "argv": ["kubectl", *argv],
            "lines": total,
            "truncated": truncated,
            "redactions": redactions + redacted_err,
            "stderr": stderr[:STDERR_CAP],
            "stderrTruncated": len(stderr) > STDERR_CAP,
        },
    }
