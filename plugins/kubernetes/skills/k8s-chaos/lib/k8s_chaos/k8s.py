"""kubectl access for this skill's scripts."""

import json
import re

from k8s_chaos.errors import RwError

MESSAGE_LIMIT = 1024

# (code, substrings of lower-cased stderr); first match wins.
_CLASSES = (
    (
        "permission_denied",
        ("forbidden", "cannot get", "cannot list", "cannot watch", "cannot create"),
    ),
    ("dependency", ("the server doesn't have a resource type", "executable file not found")),
    ("not_found", ("notfound", "not found")),
    (
        "unreachable",
        (
            "unable to connect",
            "i/o timeout",
            "connection refused",
            "no such host",
            "x509",
            "tls:",
            "tls handshake",
        ),
    ),
    ("timeout", ("deadline exceeded",)),
    ("rate_limited", ("429", "too many requests")),
)


def classify(returncode, stderr):
    """D64 error code for a failed kubectl call."""
    text = (stderr or "").lower()
    if returncode == 124:
        return "timeout"
    if returncode == 127:
        return "dependency"
    for code, needles in _CLASSES:
        if any(n in text for n in needles):
            return code
    return "internal"


def failure(argv, result):
    """The RwError for a failed command result, with a D64 code from its stderr."""
    stderr = (result.stderr or "").strip()
    # kubectl prints "Warning: ..." lines before the error; the error is what the message is for
    lines = [x for x in stderr.splitlines() if not x.startswith("Warning:")]
    shown = "\n".join(lines).strip() or stderr
    return RwError(
        classify(result.returncode, stderr),
        f"{argv[0]} exited {result.returncode}: {shown[:MESSAGE_LIMIT]}",
    )


def run_unchecked(ctx, argv, **run_kwargs):
    """`ctx.run(argv)` that returns the finished process whatever its exit code.

    The v13 runner raises on a non-zero exit by default; `check=False` keeps
    the return-code path, which this skill classifies itself.
    """
    return ctx.run(argv, check=False, **run_kwargs)


def run_checked(ctx, argv, **run_kwargs):
    """`run_unchecked`, raising RwError with a D64 code when the command failed."""
    result = run_unchecked(ctx, argv, **run_kwargs)
    if result.returncode:
        raise failure(argv, result)
    return result


def kubectl_json(ctx, *args):
    """Run `kubectl <args> -o json` and return the parsed document.

    A failed call (forbidden, not found, unreachable) raises RwError and never
    turns into data here; output that is not JSON is an `internal` error.
    """
    result = run_checked(ctx, ["kubectl", *args, "-o", "json"])
    try:
        return json.loads(result.stdout)
    except ValueError as exc:
        raise RwError("internal", f"kubectl returned output that is not JSON: {exc}") from None


def error_code(exc):
    """The D64 code of a failure raised by run_checked, else None."""
    return getattr(exc, "code", None)


def kubectl_json_scoped(ctx, scope, *args, missing_ok=False):
    """A read of an object the rest of the result can do without: (document, state).

    state is 'ok', 'not_found' (only with `missing_ok`: the object does not
    exist, a fact for the caller) or 'denied' (forbidden: reported with
    `ctx.error` as permission_denied for `scope`, document None). Every other
    failure (unreachable, timeout, missing API, bad output) still raises.
    """
    try:
        return kubectl_json(ctx, *args), "ok"
    except RwError as exc:
        if exc.code == "not_found" and missing_ok:
            return None, "not_found"
        if exc.code == "permission_denied":
            ctx.error(
                "permission_denied",
                f"not allowed to read {scope}: {exc.message}"[:MESSAGE_LIMIT],
                scope=scope,
                hint=f"grant get/list on {scope} through a Role in the target's namespace",
            )
            return None, "denied"
        raise


def kubectl_text(ctx, *args, **run_kwargs):
    """Run `kubectl <args>` and return stdout; a failed call raises RwError.

    For the mutating calls, whose output is a sentence rather than a document.
    """
    return run_checked(ctx, ["kubectl", *args], **run_kwargs).stdout or ""


# kubectl exec: the command inside the container ran and exited N -> kubectl prints this and exits N
_REMOTE_EXIT = re.compile(r"command terminated with exit code (\d+)")
# the container has no such program (no shell, no dd, no rm)
_NO_PROGRAM = (
    "executable file not found",
    "no such file or directory",
    ": not found",
    "not found in $path",
)


def limit_text(value, limit=MESSAGE_LIMIT):
    """(text, truncated) for a free-text field."""
    if value is None:
        return None, False
    value = str(value)
    return value[:limit], len(value) > limit


def exec_failure(argv, result):
    """RwError for a `kubectl exec` that did not run the command, else None.

    kubectl exits with the remote command's own exit code, so a non-zero return
    code alone says nothing about kubectl. Two things do: the "command terminated
    with exit code N" line (the command ran; N is its result, not an error here,
    except 126/127 which mean the program was not found or not executable) and
    anything else on stderr (kubectl could not exec: forbidden, pod gone, no
    shell in the image, API unreachable). A missing program is `dependency`.
    """
    if not result.returncode:
        return None
    stderr = result.stderr or ""
    remote = _REMOTE_EXIT.search(stderr)
    if remote and int(remote.group(1)) not in (126, 127):
        return None
    text = stderr.lower()
    if remote or any(n in text for n in _NO_PROGRAM):
        shown, _ = limit_text(stderr.strip())
        return RwError(
            "dependency",
            (
                "the container has no program to run this action "
                f"(it needs sh and the tool named in the task): {shown}"
            )[:MESSAGE_LIMIT],
        )
    return failure(argv, result)
