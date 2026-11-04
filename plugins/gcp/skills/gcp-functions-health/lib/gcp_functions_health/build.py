"""The build behind a function's latest deployment, from Cloud Build.

A 2nd gen function names its build in `buildConfig.build`, a 1st gen one in `buildName` (and
`buildId`): `projects/<number>/locations/<location>/builds/<id>`. `gcloud builds describe <id>
--region=<location>` returns the whole build resource, with its steps and their environment; only
the status facts below are kept, never a step, an argument or a variable."""

import re

from .errors import RwError
from .fnlist import json_call
from .logtext import clip

BUILD_ID = re.compile(r"[A-Za-z0-9-]{8,64}")
BUILD_NAME = re.compile(r"projects/[^/]+/locations/([^/]+)/builds/([^/]+)")
TEXT = 1024


def build_ref(row, region):
    """(build id, location) of the function's latest build, or None when the row names none. A
    1st gen function that only has a `buildId` is looked up in the function's own region."""
    config = row.get("buildConfig")
    name = config.get("build") if isinstance(config, dict) else row.get("buildName")
    if isinstance(name, str) and name:
        match = BUILD_NAME.fullmatch(name)
        if match and BUILD_ID.fullmatch(match.group(2)):
            return match.group(2), match.group(1)
        return None
    build_id = row.get("buildId") if not isinstance(config, dict) else None
    if isinstance(build_id, str) and BUILD_ID.fullmatch(build_id):
        return build_id, region
    return None


def text(value):
    return clip(value)[0] if isinstance(value, str) and value else None


def build_facts(doc, build_id):
    """The `build` output from a Cloud Build resource: its status and times, never its steps."""
    if not isinstance(doc, dict) or not isinstance(doc.get("status"), str):
        raise RwError("internal", "gcloud builds describe returned no build status")
    failure = doc.get("failureInfo")
    return {
        "id": build_id,
        "status": doc["status"],
        "statusDetail": text(doc.get("statusDetail")),
        "createTime": doc.get("createTime") if isinstance(doc.get("createTime"), str) else None,
        "startTime": doc.get("startTime") if isinstance(doc.get("startTime"), str) else None,
        "finishTime": doc.get("finishTime") if isinstance(doc.get("finishTime"), str) else None,
        "failureType": failure.get("type") if isinstance(failure, dict) else None,
        "failureDetail": text(failure.get("detail")) if isinstance(failure, dict) else None,
        "logUrl": doc["logUrl"][:TEXT] if isinstance(doc.get("logUrl"), str) else None,
    }


def read_build(ctx, project, ref, timeout):
    """The facts of the build `ref` = (id, location); a failure raises RwError."""
    build_id, location = ref
    doc, _ = json_call(
        ctx,
        ["builds", "describe", build_id, f"--region={location}", f"--project={project}"],
        project,
        timeout=timeout,
    )
    return build_facts(doc, build_id)
