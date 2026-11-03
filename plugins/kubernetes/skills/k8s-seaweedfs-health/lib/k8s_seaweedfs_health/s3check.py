"""s3-check: PUT, GET and DELETE one small object through the S3 gateway, with no credentials, cleaning up."""

import re

from k8s_seaweedfs_health.common import nested
from k8s_seaweedfs_health.errors import RwError
from k8s_seaweedfs_health.fetch import get_text, load_release, pod_port
from k8s_seaweedfs_health.k8s import MESSAGE_LIMIT, ProxyError, proxy_delete, proxy_put

DEFAULT_BUCKET = "runwhen-s3-check"
BODY_FILE = "s3-check-body"
BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
PLAN = (
    "create-bucket",
    "put-object",
    "get-object",
    "delete-object",
    "confirm-deleted",
    "delete-bucket",
)


def check_bucket(bucket):
    if not BUCKET_RE.match(bucket) or ".." in bucket:
        raise RwError(
            "invalid_input",
            f"bucket {bucket!r} is not a valid S3 bucket name (3-63 of a-z 0-9 . -)",
        )


def object_key(now):
    """A key no earlier run used (second resolution), under a fixed prefix."""
    return f"runwhen-s3-check/{now.strftime('%Y%m%dT%H%M%SZ')}.txt"


def object_body(now):
    return f"runwhen s3-check {now.strftime('%Y-%m-%dT%H:%M:%SZ')}"


def refusal(what):
    return (
        f"the S3 gateway refused {what} without credentials (HTTP 401/403). This task holds no S3 "
        "credential and never reads Secrets, so it cannot sign a request: nothing was written."
    )


class Probe:
    """One run: the steps taken and what has to be undone."""

    def __init__(self, ctx, rel, pod, bucket, key):
        self.ctx, self.rel, self.pod, self.bucket, self.key = ctx, rel, pod, bucket, key
        self.name = pod["metadata"]["name"]
        self.port = pod_port(ctx, "s3")
        self.scheme = ctx.settings.scheme
        self.steps = []
        self.created = False
        self.object_attempted = False
        self.object_deleted = False
        self.content_matches = None
        self.refused = False

    def step(self, name, fn, first_write=False, expect_missing=False):
        """One HTTP request as a step: (ok, value). A failure is the step's data, not raised, so the
        clean-up still runs. A gateway that refuses the very first write skips the task."""
        entry = {"name": name, "ok": True, "error": None}
        value = None
        try:
            value = fn()
        except ProxyError as exc:
            if exc.kind == "upstream_denied" and first_write:
                self.refused = True  # nothing was written: there is nothing to clean up
                self.ctx.skip(refusal("an anonymous write"))
            if expect_missing and exc.kind == "upstream_missing":
                pass  # the answer wanted
            else:
                entry.update(
                    ok=False, error={"code": exc.code, "message": exc.message[:MESSAGE_LIMIT]}
                )
                if exc.kind == "rbac":
                    self.ctx.error(exc.code, exc.message[:MESSAGE_LIMIT], scope=f"s3-check/{name}")
        else:
            if expect_missing:
                entry.update(
                    ok=False,
                    error={
                        "code": "internal",
                        "message": "the object can still be read after DELETE",
                    },
                )
        self.steps.append(entry)
        return entry["ok"], value

    def get(self, path):
        return get_text(self.ctx, self.rel, self.pod, "s3", path)

    def put(self, path, body):
        (self.ctx.workdir / BODY_FILE).write_text(body)
        return proxy_put(
            self.ctx, self.rel.namespace, self.name, self.port, path, BODY_FILE, self.scheme
        )

    def delete(self, path):
        return proxy_delete(self.ctx, self.rel.namespace, self.name, self.port, path, self.scheme)

    def path(self):
        return f"{self.bucket}/{self.key}"


def bucket_exists(probe):
    """True/False from a list of the bucket (200 / 404). A gateway that refuses even that (401/403)
    wants credentials: the task skips. Anything else raises."""
    try:
        probe.get(f"{probe.bucket}/?list-type=2&max-keys=1")
    except ProxyError as exc:
        if exc.kind == "upstream_missing":
            return False
        if exc.kind == "upstream_denied":
            probe.ctx.skip(refusal("to list the probe bucket"))
        raise
    return True


def cleanup(probe):
    """Undo what this run did, whatever happened before: the object, then a bucket this run created.
    Returns what could not be removed."""
    left, todo = [], []
    if probe.refused:
        return left
    if probe.object_attempted and not probe.object_deleted:
        todo.append(("cleanup-delete-object", probe.path(), probe.path()))
    if probe.created:
        todo.append(("delete-bucket", probe.bucket, probe.bucket))
    for name, target, what in todo:
        ok, _ = probe.step(name, lambda t=target: probe.delete(t))
        if not ok:
            left.append(what)
            err = probe.steps[-1]["error"]
            probe.ctx.error(
                err["code"],
                f"could not remove {what}: {err['message']}"[:MESSAGE_LIMIT],
                scope="s3-check/cleanup",
            )
    return left


def attempt(probe, bucket, existed, body):
    """The probe proper; the caller cleans up after it, however it ends."""
    if not existed:
        probe.created, _ = probe.step(
            "create-bucket", lambda: probe.put(bucket, ""), first_write=True
        )
        if not probe.created:
            return
    probe.object_attempted = True
    put, _ = probe.step("put-object", lambda: probe.put(probe.path(), body), first_write=existed)
    if not put:
        return
    got, text = probe.step("get-object", lambda: probe.get(probe.path()))
    if got:
        probe.content_matches = text == body
        if not probe.content_matches:
            probe.steps[-1].update(
                ok=False,
                error={
                    "code": "internal",
                    "message": "the object read back is not what was written",
                },
            )
    probe.object_deleted, _ = probe.step("delete-object", lambda: probe.delete(probe.path()))
    if probe.object_deleted:
        probe.step("confirm-deleted", lambda: probe.get(probe.path()), expect_missing=True)


def run(ctx, release="", bucket=DEFAULT_BUCKET):
    check_bucket(bucket)
    rel = load_release(ctx, release)
    ok, rest = rel.role_pods("s3")
    if not ok and not rest:
        ctx.skip(
            "the release has no S3 gateway pod (no pod serves the s3 role), so there is nothing to test"
        )
    if not ok:
        raise RwError(
            "unreachable",
            f"no Running S3 gateway pod to test: {', '.join(p['metadata']['name'] for p in rest)} not Running",
            scope="s3",
        )
    now = ctx.now()
    probe = Probe(ctx, rel, ok[0], bucket, object_key(now))
    existed = bucket_exists(
        probe
    )  # a gateway that refuses even a list skips here, before any write
    head = {
        "gateway": {"pod": probe.name, "port": probe.port},
        "bucket": bucket,
        "key": probe.key,
        "bucketExisted": existed,
        "plan": [s for s in PLAN if not existed or s not in ("create-bucket", "delete-bucket")],
    }
    if ctx.dry_run:
        return {
            "probe": {
                **head,
                "bucketCreated": False,
                "steps": [],
                "passed": None,
                "contentMatches": None,
                "anonymousWrite": None,
                "cleanedUp": None,
                "leftBehind": [],
            }  # fmt: skip
        }
    try:
        attempt(probe, bucket, existed, object_body(now))
    finally:
        left = cleanup(probe)
    steps = probe.steps
    work = [s for s in steps if s["name"] in PLAN]
    put = next((s for s in work if s["name"] == "put-object"), None)
    expected = [s for s in PLAN if existed is False or s not in ("create-bucket", "delete-bucket")]
    return {
        "probe": {
            **head,
            "bucketCreated": probe.created,
            "steps": nested(steps),
            "passed": [s["name"] for s in work] == expected
            and all(s["ok"] for s in work)
            and not left,
            "contentMatches": probe.content_matches,
            "anonymousWrite": None if put is None else (True if put["ok"] else None),
            "cleanedUp": not left,
            "leftBehind": left,
        }
    }
