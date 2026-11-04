"""Reading ARM objects through `az rest`.

`az webapp show` and `az functionapp show` also read the app's publishing profile (the FTP URL, and
with it the publishing credentials) and `az webapp config appsettings list` returns every app setting
with its value; none of that is needed to answer a health question. A GET of the object returns the
object and nothing else, so this skill reads App Service through `az rest --method get` only: a task
cannot reach an app setting, a connection string or a publishing credential, because no call it makes
returns one (and the three the skill never makes are named in the tests).

A relative URL is an ARM resource id: az prefixes the active cloud's Resource Manager endpoint."""

import time

from .az import az_json, command_timeout
from .errors import RwError

API_WEB = "2024-04-01"  # Microsoft.Web sites, serverfarms, slots, deployments, config/web
API_AUTOSCALE = "2022-10-01"  # Microsoft.Insights autoscalesettings
API_HEALTH = "2022-10-01"  # Microsoft.ResourceHealth availabilityStatuses
MAX_PAGES = 5  # pages of one list that are followed; more is reported, not hidden
PAGE_BUDGET = (
    120.0  # seconds one list may spend on its pages; what is left unread is reported, not hidden
)


def url(path, api):
    return f"{path}?api-version={api}"


def get(ctx, path, api=API_WEB):
    """The JSON object ARM returns for `path`. An empty reply is not an answer."""
    reply = az_json(ctx, ["rest", "--method", "get", "--url", url(path, api)])
    if not isinstance(reply, dict):
        raise RwError("internal", f"Azure returned no object for {path.rsplit('/', 1)[-1]}")
    return reply


def get_list(ctx, path, api=API_WEB, budget=PAGE_BUDGET):
    """(rows, complete) of an ARM list: every `value` row of up to MAX_PAGES pages that fit in `budget`
    seconds on the monotonic clock (a page is only asked for when a full commandTimeout still fits).
    `complete` is false when the list ended for pages or time with a nextLink left. The first page is
    always asked. A page that is not a list raises."""
    started, need = time.monotonic(), command_timeout(ctx.settings)
    rows, address = [], url(path, api)
    for page_number in range(MAX_PAGES):
        if page_number and time.monotonic() - started + need > budget:
            return rows, False
        page = az_json(ctx, ["rest", "--method", "get", "--url", address])
        values = page.get("value") if isinstance(page, dict) else None
        if not isinstance(values, list):
            raise RwError("internal", f"Azure returned no list for {path.rsplit('/', 1)[-1]}")
        rows += [row for row in values if isinstance(row, dict)]
        address = page.get("nextLink")
        if not address:
            return rows, True
    return rows, False
