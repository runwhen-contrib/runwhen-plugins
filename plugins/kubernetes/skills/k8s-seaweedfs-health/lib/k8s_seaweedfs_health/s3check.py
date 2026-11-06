"""s3-check: PUT, GET and DELETE one small object in an existing bucket through the S3 gateway, with no credentials.

The task never creates or deletes a bucket: the bucket must exist. Everything it writes is one object,
and it removes that object again, whatever happened before.
"""

import hashlib
import re
import time

from k8s_seaweedfs_health.buckets import root_tag
from k8s_seaweedfs_health.common import nested
from k8s_seaweedfs_health.errors import RwError
from k8s_seaweedfs_health.fetch import get_text, load_release, pod_port
from k8s_seaweedfs_health.k8s import MESSAGE_LIMIT, ProxyError, proxy_delete, proxy_put

DEFAULT_BUCKET = "runwhen-s3-check"
BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
PLAN = ("put-object", "get-object", "delete-object", "confirm-deleted")

BUDGET = 50.0  # seconds for the whole run, inside the task's 60s timeout
CALL_LIMIT = 15.0  # seconds for one request, at most
DELETE_RESERVE = 15.0  # seconds nothing before the DELETE may take
MIN_CALL = 1.0  # a request is not worth starting with less left than this
clock = time.monotonic

# ProxyError kinds that say the request was not carried out: nothing can have landed
NOT_LANDED = ("rbac", "upstream_denied", "upstream_missing")


def check_bucket(bucket):
    if not BUCKET_RE.match(bucket) or ".." in bucket:
        raise RwError(
            "invalid_input",
            f"bucket {bucket!r} is not a valid S3 bucket name (3-63 of a-z 0-9 . -)",
        )


def object_key(ctx, now):
    """A key no other run used, under a fixed prefix: the run's own time to the microsecond and six
    hex digits of sha256(target path + time). Derived from `now` alone, so a replay writes the same key."""
    digest = hashlib.sha256(f"{ctx.resource.path}{now.isoformat()}".encode()).hexdigest()[:6]
    return f"runwhen-s3-check/{now.strftime('%Y%m%dT%H%M%S%fZ')}-{digest}.txt"


def object_body(now):
    return f"runwhen s3-check {now.strftime('%Y-%m-%dT%H:%M:%SZ')}"


def refusal(what):
    return (
        f"the S3 gateway refused {what} without credentials (HTTP 401/403). This task holds no S3 "
        "credential and never reads Secrets, so it cannot sign a request: nothing was written."
    )


def missing_bucket(bucket):
    return f"bucket {bucket} does not exist; create it to enable the write check"


class Budget:
    """The seconds left of this run; each request gets min(CALL_LIMIT, what is left)."""

    def __init__(self, total=BUDGET):
        self.end = clock() + total

    def remaining(self):
        return self.end - clock()

    def timeout(self, reserve=0.0):
        """Seconds for the next request, keeping `reserve` for later ones. A request that cannot get
        MIN_CALL seconds is not sent: it fails as a timeout."""
        seconds = min(CALL_LIMIT, self.remaining() - reserve)
        if seconds < MIN_CALL:
            raise ProxyError(
                "timeout",
                f"not enough of the {BUDGET:g}s budget left to send this request "
                f"({max(self.remaining(), 0):.0f}s left, {reserve:g}s kept for the DELETE)",
                "timeout",
            )
        return seconds


class Probe:
    """One run: the steps taken and what has to be undone."""

    def __init__(self, ctx, rel, pod, bucket, key, budget):
        self.ctx, self.rel, self.pod, self.bucket, self.key = ctx, rel, pod, bucket, key
        self.budget = budget
        self.name = pod["metadata"]["name"]
        self.port = pod_port(ctx, "s3")
        self.scheme = ctx.settings.scheme
        self.steps = []
        self.object_attempted = False  # a PUT was sent that may have landed
        self.object_deleted = False
        self.content_matches = None
        self.refused = False

    def step(
        self, name, fn, first_write=False, expect_missing=False, missing_ok=False, report=True
    ):
        """One HTTP request as a step: (ok, value). A failure is the step's data and a `ctx.error`
        (scope s3-check/<name>), not an exception, so the clean-up still runs. A gateway that refuses
        the very first write skips the task; one that does not know the bucket skips it too.
        `expect_missing`: the request must answer 404 (a 2xx is the failure); `missing_ok`: a 404 is fine."""
        entry = {"name": name, "ok": True, "error": None}
        value = None
        try:
            value = fn()
        except ProxyError as exc:
            if first_write and exc.kind == "upstream_denied":
                self.refused = True  # nothing was written: there is nothing to clean up
                self.ctx.skip(refusal("an anonymous write"))
            if first_write and exc.kind == "upstream_missing":
                self.refused = True
                self.ctx.skip(missing_bucket(self.bucket))
            if (expect_missing or missing_ok) and exc.kind == "upstream_missing":
                pass  # the answer wanted
            else:
                self.fail(entry, exc.code, exc.message, report and f"s3-check/{name}")
        else:
            if expect_missing:
                self.fail(
                    entry,
                    "internal",
                    "the object can still be read after DELETE",
                    report and f"s3-check/{name}",
                )
        self.steps.append(entry)
        return entry["ok"], value

    def fail(self, entry, code, message, scope):
        entry.update(ok=False, error={"code": code, "message": message[:MESSAGE_LIMIT]})
        if scope:
            self.ctx.error(code, f"{entry['name']}: {message}"[:MESSAGE_LIMIT], scope=scope)

    def get(self, path, reserve=0.0):
        return get_text(
            self.ctx, self.rel, self.pod, "s3", path, timeout=self.budget.timeout(reserve)
        )

    def put(self, path, body):
        timeout = self.budget.timeout(DELETE_RESERVE)  # raises before anything is sent
        self.object_attempted = True
        try:
            return proxy_put(
                self.ctx,
                self.rel.namespace,
                self.name,
                self.port,
                path,
                body,
                self.scheme,
                timeout=timeout,
            )
        except ProxyError as exc:
            if exc.kind in NOT_LANDED:
                self.object_attempted = False
            raise

    def delete(self, path):
        return proxy_delete(
            self.ctx,
            self.rel.namespace,
            self.name,
            self.port,
            path,
            self.scheme,
            timeout=self.budget.timeout(),
        )

    def path(self):
        return f"{self.bucket}/{self.key}"


def bucket_listing(probe):
    """'allowed' when an anonymous list of the bucket answers 200 with a ListBucketResult, 'denied' when
    the gateway answers 401/403 (a write-only anonymous identity is possible: the write is still tried).
    A 404 skips the task: the bucket must exist. Anything else raises."""
    what = f"S3 list of bucket {probe.bucket}"
    try:
        text = probe.get(f"{probe.bucket}/?list-type=2&max-keys=1")
    except ProxyError as exc:
        if exc.kind == "upstream_missing":
            probe.ctx.skip(missing_bucket(probe.bucket))
        if exc.kind == "upstream_denied":
            return "denied"
        raise
    try:
        tag = root_tag(text, what)
    except RwError as exc:
        raise RwError("internal", f"not an S3 endpoint: {exc.message}") from None
    if tag != "ListBucketResult":
        raise RwError(
            "internal",
            f"not an S3 endpoint: {what} has root element {tag!r}, expected 'ListBucketResult'",
        )
    return "allowed"


def cleanup(probe):
    """Undo what this run did, whatever happened before: the object, if a PUT may have landed.
    Returns what could not be removed."""
    left = []
    if probe.refused or not probe.object_attempted or probe.object_deleted:
        return left
    target = probe.path()
    # a DELETE of a key that is not there answers 404: gone is what cleanup wants
    ok, _ = probe.step(
        "cleanup-delete-object", lambda: probe.delete(target), missing_ok=True, report=False
    )
    if not ok:
        left.append(target)
        err = probe.steps[-1]["error"]
        probe.ctx.error(
            err["code"],
            f"could not remove {target}: {err['message']}"[:MESSAGE_LIMIT],
            scope="s3-check/cleanup",
        )
    return left


def attempt(probe, body):
    """The probe proper; the caller cleans up after it, however it ends."""
    put, _ = probe.step("put-object", lambda: probe.put(probe.path(), body), first_write=True)
    if not put:
        return  # the cleanup deletes first: the PUT may have landed
    got, text = probe.step("get-object", lambda: probe.get(probe.path(), reserve=DELETE_RESERVE))
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
            probe.ctx.error(
                "internal",
                "get-object: the object read back is not what was written",
                scope="s3-check/get-object",
            )
    probe.object_deleted, _ = probe.step("delete-object", lambda: probe.delete(probe.path()))
    if probe.object_deleted:
        probe.step("confirm-deleted", lambda: probe.get(probe.path()), expect_missing=True)


def run(ctx, release="", bucket=DEFAULT_BUCKET):
    check_bucket(bucket)
    budget = Budget()
    rel = load_release(ctx, release)
    ok, rest = rel.role_pods("s3")
    if not ok and not rest:
        ctx.skip(
            "the release has no S3 gateway pod (no pod serves the s3 role: none has an s3 flag "
            "in its command or args), so there is nothing to test"
        )
    if not ok:
        raise RwError(
            "unreachable",
            f"no Running S3 gateway pod to test: {', '.join(p['metadata']['name'] for p in rest)} not Running",
            scope="s3",
        )
    now = ctx.now()
    probe = Probe(ctx, rel, ok[0], bucket, object_key(ctx, now), budget)
    listing = bucket_listing(probe)  # a missing bucket skips here, before any write
    head = {
        "gateway": {"pod": probe.name, "port": probe.port},
        "bucket": bucket,
        "key": probe.key,
        "bucketListing": listing,
        "plan": list(PLAN),
    }
    if ctx.dry_run:
        return {
            "probe": {
                **head,
                "steps": [],
                "passed": None,
                "contentMatches": None,
                "anonymousWrite": None,
                "cleanedUp": None,
                "leftBehind": [],
            }  # fmt: skip
        }
    try:
        attempt(probe, object_body(now))
    finally:
        left = cleanup(probe)
    steps = probe.steps
    work = [s for s in steps if s["name"] in PLAN]
    put = next((s for s in work if s["name"] == "put-object"), None)
    # true only for a 2xx PUT whose read-back matched; false for a 2xx PUT that stored something else;
    # null when the PUT failed or the read-back did not answer
    anonymous_write = probe.content_matches if put is not None and put["ok"] else None
    return {
        "probe": {
            **head,
            "steps": nested(steps),
            "passed": [s["name"] for s in work] == list(PLAN)
            and all(s["ok"] for s in work)
            and not left,
            "contentMatches": probe.content_matches,
            "anonymousWrite": anonymous_write,
            "cleanedUp": not left,
            "leftBehind": left,
        }
    }
