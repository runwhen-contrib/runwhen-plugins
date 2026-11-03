"""kubectl access for this skill's scripts."""

import json


def kubectl_json(ctx, *args):
    """Run `kubectl <args> -o json` and return the parsed document.

    `ctx.run` raises on a non-zero exit with a classified error code, so a
    failed call (forbidden, not found, unreachable) never turns into data here.
    """
    result = ctx.run(["kubectl", *args, "-o", "json"])
    return json.loads(result.stdout)
