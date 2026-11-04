"""Reading Cloud Functions with gcloud: `functions list` and `functions describe`.

Shared by the gcp skills that read functions (gcp-base for discovery, gcp-functions-health for the
tasks): each carries a verbatim copy in its own `lib/<skill_snake>/`, importing only `.errors` and
`.gcloud`; the plugin unit test test_gcp_copies.py fails when a copy drifts from gcp-base's.

What the pinned gcloud (592.0.0) does, read from its source in the runtime image:

* `functions list` has no `--gen2`; its `--v2` forces the v2 format. Without it, 2nd gen
  functions come from the v2 API (filter environment="GEN_2") in the v2 format and 1st gen ones
  from the v1 API in the v1 format, so the two shapes sit in one list: 2nd gen has `environment:
  GEN_2`, `state` and `serviceConfig`; 1st gen has `status` and no `environment`. The default
  `--regions` is `-`: every location.
* `functions describe` takes the generation from the function itself when `--gen2` is not given
  (the v2 API first; GEN_2 prints the v2 format, otherwise the v1 one). `--gen2` is hidden there
  and not needed.
* a region the list could not reach is not an error: gcloud exits 0 and warns on stderr
  ("The following regions were fully or partially unreachable for query: ..."). `run_gcloud` in
  `gcloud.py` drops stderr on success, so the calls here keep it: a list that skipped regions is
  returned with their names and the caller marks its read incomplete.

Every call carries `--billing-project=<the project>` itself (gcloud.py derives it from a project
or bucket target; a function target is neither) and runs with the ambient credentials neutralised
(`gcloud_env`)."""

import json
import re
import subprocess
import time

from .errors import RwError
from .gcloud import (
    DEFAULT_COMMAND_TIMEOUT,
    classify_error,
    error_text,
    gcloud_env,
    parse_duration,
    valid_project,
)

try:  # pragma: no cover - present in every SDK that has ctx.run
    from runwhen_capability.errors import OutputTooLargeError
except ImportError:  # pragma: no cover

    class OutputTooLargeError(ValueError):
        pass


# Function ids: 1st gen allows letters, digits, `-` and `_`; 2nd gen lowercase letters, digits and
# `-`; at most 63 characters, starting with a letter. The id goes into argv and into filters.
FUNCTION_ID = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,62}")
REGION = re.compile(r"[a-z][a-z0-9-]{2,30}")
NAME = re.compile(r"projects/([^/]+)/locations/([^/]+)/functions/([^/]+)")
_UNREACHABLE = re.compile(r"unreachable for query:\s*(.*?)(?:This could be|\n|$)", re.S)
MAX_UNREACHABLE = 50


def valid_function_id(name):
    return isinstance(name, str) and bool(FUNCTION_ID.fullmatch(name))


def valid_region(name):
    return isinstance(name, str) and bool(REGION.fullmatch(name))


MIN_REMAINING = 2.0  # seconds a call needs to be worth starting


def clamp(seconds, deadline, clock=time.monotonic):
    """`seconds` cut to what is left before the monotonic `deadline` (None: no deadline). A call
    that would start with (almost) nothing left is a `timeout`, not a call that cannot finish."""
    if deadline is None:
        return seconds
    remaining = deadline - clock()
    if remaining < MIN_REMAINING:
        raise RwError("timeout", "the task's time budget ran out before this call")
    return min(seconds, remaining)


def run_capture(ctx, args, project, *, timeout=None, deadline=None, json_output=True):
    """(stdout, stderr) of `gcloud <args> --billing-project=<project> [--format=json] --quiet`;
    a non-zero exit raises RwError with the code classified from stderr (the same rules as
    `gcloud.run_gcloud`). The call may take `timeout` seconds (default 60s) or what is left before
    the monotonic `deadline`, whichever is less."""
    if not valid_project(project):
        raise RwError("invalid_input", "the target's project is not a valid project id")
    argv = ["gcloud", *args, f"--billing-project={project}"]
    if json_output:
        argv.append("--format=json")
    argv.append("--quiet")
    seconds = clamp(
        timeout if timeout is not None else parse_duration(DEFAULT_COMMAND_TIMEOUT), deadline
    )
    try:
        result = ctx.run(argv, timeout=seconds, env=gcloud_env(ctx), check=False)
    except subprocess.TimeoutExpired:
        raise RwError("timeout", f"gcloud did not finish within {seconds:.0f}s") from None
    except OutputTooLargeError:
        raise RwError(
            "invalid_input", "the gcloud result is too large to return; narrow the command"
        ) from None
    except FileNotFoundError:
        raise RwError("dependency", "gcloud is not installed in this runtime") from None
    stderr = result.stderr or ""
    if result.returncode != 0:
        raise RwError(
            classify_error(stderr, result.returncode),
            error_text(stderr) or f"gcloud exited {result.returncode}",
        )
    return result.stdout or "", stderr


def text_call(ctx, args, project, *, timeout=None, deadline=None):
    """The text a gcloud command without a JSON form prints (`auth print-access-token`)."""
    return run_capture(ctx, args, project, timeout=timeout, deadline=deadline, json_output=False)[0]


def json_call(ctx, args, project, *, timeout=None, deadline=None):
    """(parsed JSON, stderr). Empty output is an error, never an empty result."""
    out, err = run_capture(ctx, args, project, timeout=timeout, deadline=deadline)
    if not out.strip():
        raise RwError("internal", "gcloud printed no JSON")
    try:
        return json.loads(out), err
    except ValueError:
        raise RwError("internal", "gcloud did not return a JSON value") from None


def parse_name(row):
    """(project, region, id) of a function row's `name`
    (`projects/<p>/locations/<r>/functions/<id>`); anything else is an error."""
    match = NAME.fullmatch(row.get("name") or "") if isinstance(row, dict) else None
    if not match or not valid_region(match.group(2)) or not valid_function_id(match.group(3)):
        raise RwError("internal", "gcloud returned a function without a usable name")
    return match.groups()


UNKNOWN_REGION = "unknown"


def unreachable_regions(stderr):
    """The locations gcloud said it could not reach (it exits 0 and warns), as region names. A
    warning that says "unreachable" but names no region gcloud's way is `["unknown"]`: the list
    is still not complete."""
    found = []
    for match in _UNREACHABLE.finditer(str(stderr)):
        for part in match.group(1).split(","):
            region = part.strip().rsplit("/", 1)[-1]
            if valid_region(region) and region not in found:
                found.append(region)
    if not found and re.search(r"unreachable", str(stderr), re.I):
        return [UNKNOWN_REGION]
    return found[:MAX_UNREACHABLE]


def list_functions(ctx, project, *, timeout=None, deadline=None):
    """(rows, unreachable): every function of the project in every location, 1st and 2nd gen,
    each in its generation's own format, and the regions gcloud could not reach (whose functions
    are missing: the caller must not call the list complete)."""
    rows, err = json_call(
        ctx,
        ["functions", "list", f"--project={project}"],
        project,
        timeout=timeout,
        deadline=deadline,
    )
    if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
        raise RwError("internal", "gcloud returned no function list")
    for row in rows:
        parse_name(row)
    return rows, unreachable_regions(err)


def list_regions(ctx, project, *, timeout=None, deadline=None):
    """The regions Cloud Functions serves for the project, sorted: the union of `functions regions
    list` (the v1 API's locations, 1st gen) and `functions regions list --gen2` (the v2 API's),
    both needing only `cloudfunctions.locations.list`, which `functions list` asks for too. A
    region in it with no function is a region that was read and is empty, which is what lets
    discovery sweep a region whose last function was deleted. A failure raises."""
    found = set()
    for extra in ([], ["--gen2"]):
        rows, _ = json_call(
            ctx,
            ["functions", "regions", "list", f"--project={project}", *extra],
            project,
            timeout=timeout,
            deadline=deadline,
        )
        if not isinstance(rows, list):
            raise RwError("internal", "gcloud returned no region list")
        for row in rows:
            name = row.get("locationId") if isinstance(row, dict) else None
            if not name and isinstance(row, dict) and isinstance(row.get("name"), str):
                name = row["name"].rsplit("/", 1)[-1]
            if not valid_region(name):
                raise RwError("internal", "gcloud returned a region without a usable name")
            found.add(name)
    return sorted(found)


def describe_function(ctx, project, region, name, *, timeout=None, deadline=None):
    """One function's row, in its generation's format (gcloud picks the generation). A function
    that does not exist is `not_found`."""
    if not valid_region(region) or not valid_function_id(name):
        raise RwError("invalid_input", "the target's region or function name is not valid")
    row, _ = json_call(
        ctx,
        ["functions", "describe", name, f"--region={region}", f"--project={project}"],
        project,
        timeout=timeout,
        deadline=deadline,
    )
    if not isinstance(row, dict):
        raise RwError("internal", "gcloud returned no function")
    parse_name(row)
    return row


def generation(row):
    """2 for a 2nd gen function (v2 format), 1 for a 1st gen one, None for a row that says neither."""
    environment = row.get("environment")
    if environment == "GEN_2" or (environment is None and "serviceConfig" in row):
        return 2
    if environment == "GEN_1" or (environment is None and "status" in row):
        return 1
    return None


def state(row):
    """The function's lifecycle state as Google reports it: 2nd gen `state` (ACTIVE, FAILED,
    DEPLOYING, DELETING, UNKNOWN), 1st gen `status` (ACTIVE, OFFLINE, DEPLOY_IN_PROGRESS,
    DELETE_IN_PROGRESS, UNKNOWN). None when the row carries neither (never read as ACTIVE)."""
    key = "state" if generation(row) == 2 else "status"
    value = row.get(key)
    return value if isinstance(value, str) and value else None
