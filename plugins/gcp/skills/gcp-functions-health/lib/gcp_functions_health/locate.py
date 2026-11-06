"""Finding the function a task runs on: its row, in its generation's format."""

from . import fnlist
from .errors import RwError


def locate(ctx, target, timeout, deadline=None):
    """The function's row. A target with a region (its path's `region` ancestor) is one `functions
    describe` (gcloud takes the generation from the function; one that does not exist is
    `not_found`). A target without one lists the project's functions and picks the one with this
    id: no match is `not_found` (unless gcloud could not reach some regions, which may hold it:
    then it is `unreachable`, never a function that does not exist), and a match in several
    regions, or a match while some regions were unreachable (the id may be in them too), is
    `invalid_input`: the id does not say which function is meant."""
    if target.region:
        return fnlist.describe_function(
            ctx, target.project, target.region, target.name, timeout=timeout, deadline=deadline
        )
    rows, unreachable = fnlist.list_functions(
        ctx, target.project, timeout=timeout, deadline=deadline
    )
    found = [r for r in rows if fnlist.parse_name(r)[2] == target.name]
    if len(found) == 1 and not unreachable:
        return found[0]
    if found:
        regions = sorted(fnlist.parse_name(r)[1] for r in found)[:10]
        why = (
            f"function {target.name} exists in {len(found)} regions ({', '.join(regions)})"
            if len(found) > 1
            else f"function {target.name} is in {regions[0]}, but gcloud could not reach "
            f"{', '.join(unreachable)}, where an id of the same name may also exist"
        )
        raise RwError("invalid_input", f"{why}: the target needs a region")
    if unreachable:
        raise RwError(
            "unreachable",
            f"function {target.name} was not in the list, but gcloud could not reach "
            f"{', '.join(unreachable)}: it may be there",
        )
    raise RwError("not_found", f"no function {target.name} in project {target.project}")
