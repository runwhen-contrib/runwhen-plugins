"""Finding the function a task runs on: its row, in its generation's format."""

from . import fnlist
from .errors import RwError


def locate(ctx, target, timeout):
    """The function's row. With the target's region it is one `functions describe` (gcloud takes
    the generation from the function; one that does not exist is `not_found`). Without it the
    project's functions are listed and the one with this id is picked: no match is `not_found`
    (unless gcloud could not reach some regions, which may hold it: then it is `unreachable`, never
    a function that does not exist), and a match in several regions is `invalid_input` (the id is
    not enough; give the target's `region`)."""
    if target.region:
        return fnlist.describe_function(
            ctx, target.project, target.region, target.name, timeout=timeout
        )
    rows, unreachable = fnlist.list_functions(ctx, target.project, timeout=timeout)
    found = [r for r in rows if fnlist.parse_name(r)[2] == target.name]
    if len(found) == 1:
        return found[0]
    if found:
        regions = sorted(fnlist.parse_name(r)[1] for r in found)[:10]
        raise RwError(
            "invalid_input",
            f"function {target.name} exists in {len(found)} regions ({', '.join(regions)}): "
            "set the target's region",
        )
    if unreachable:
        raise RwError(
            "unreachable",
            f"function {target.name} was not in the list, but gcloud could not reach "
            f"{', '.join(unreachable)}: it may be there",
        )
    raise RwError("not_found", f"no function {target.name} in project {target.project}")
