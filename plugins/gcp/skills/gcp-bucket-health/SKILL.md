---
name: gcp-bucket-health
description: >-
  Cloud Storage bucket health for a project or one bucket: bucket-config reads each bucket's
  settings (location, class, versioning, uniform access, public access prevention, retention,
  lifecycle, soft delete, customer-managed key); public-access finds grants to allUsers or
  allAuthenticatedUsers in IAM, legacy ACLs and the default object ACL; bucket-size reads bytes
  per bucket (and per object type) from Cloud Monitoring and flags the ones over a threshold. Use for "is this bucket exposed", "what is it
  configured to do" and "what is it costing". Facts only, read-only.
runwhen:
  role: health
  appliesTo:
    types: [gcp/project, gcp/bucket]
  tags: [storage, security, usage]
---

## When to use

Reach for it when a symptom points at Cloud Storage: a security review asks which buckets are
public, a bill or a quota grows, a deploy cannot read or write a bucket, or someone needs a
bucket's settings without opening the console. On a `gcp/project` target each task looks at the
project's buckets (the first `maxItems`, by name); on a `gcp/bucket` target, at that bucket only.
The targets are the ones `gcp:gcp-base/discover` lists.

All three tasks only read, through `gcloud storage` with the connection's `gcp/adc` credential
(`gcloud storage` is used instead of the deprecated `gsutil`: one credential, one flag set, JSON
output and one error format; `bucket-size` also makes one Cloud Monitoring REST read with `curl`).
Each call is bounded by the `commandTimeout` setting (default 60s).

Every call is billed to the target's project (`--billing-project=<project>`, and the same user
project on the Monitoring read), so the identity needs `serviceusage.services.use` on it besides
the permissions of each task. **Assumption, to verify against a real project (parked P18):** that
this permission is enough for user-project billing with a service-account key whose home project is
another one. Listing or describing buckets asks for the FULL projection (ACLs and IAM settings in
each bucket resource), which the JSON API gates on `storage.buckets.getIamPolicy`, so that
permission is declared by all three tasks; a denied per-bucket policy read is therefore not
expected, but if one happens (throttling, a condition) the bucket's `publicGrant` is `null`
(partial), never `false`.

## What each task tells you

- `bucket-config` returns `buckets`: per bucket the location, location type, storage class,
  creation and update times, `versioning`, `uniformBucketLevelAccess`, `publicAccessPrevention`,
  `retention` (period and whether it is locked, or null), `lifecycleRuleCount`,
  `softDeleteSeconds`, `cmek` and the key's resource name, and up to 50 labels.
- `public-access` returns `access` per bucket: `publicGrant` (an IAM binding, a legacy ACL entry or
  the default object ACL names `allUsers` or `allAuthenticatedUsers`), `publicViaIam`,
  `publicViaAcl`, `publicViaDefaultObjectAcl`, the matching `iamBindings` (role, members, whether
  the binding is conditional), `aclEntries` and `defaultObjectAclEntries`, and
  `blockedByPrevention` (a grant exists but public access prevention is enforced). `totals` counts
  `publicBuckets`, `unknown` and whether the count is `overThreshold` of `publicBucketThreshold`
  (default 0).
- `bucket-size` returns `sizes` per bucket, largest first: `sizeBytes` (summed over the object
  types), `byType` (`live-object`, `noncurrent-object`, `soft-deleted-object`, `multipart-upload`),
  `sizeTiB`, `overThreshold` of `usageThresholdTib` (default 0.5 TiB) and `source` (`monitoring`,
  or `du` for the one fallback below), and `totals` with the bytes in all and how many buckets were
  `notMeasured`.
- Every task's `totals` carries `total` (the buckets the target covers), `omitted` (left out by
  `maxItems`) and `complete` (every bucket has an entry and none is undecided or unmeasured).

## How to read the result

- `publicGrant: false` means neither IAM nor an ACL nor the default object ACL names a public
  member. IAM is always checked, with or without uniform bucket-level access; ACLs (the bucket
  ACL and the default object ACL, which new objects start with) only count without it (with it
  they are disabled, `publicViaAcl` and `publicViaDefaultObjectAcl` are false).
- `publicGrant: null` with an error scoped to the bucket (`permission_denied`, or `rate_limited`
  when the read was throttled): the IAM policy could not be read, so "not public" is unknown, not
  true. The run is `partial`. Treat `unknown` in `totals` the same way.
- Errors scoped to a bucket name are listed one by one up to 50; the rest are one error scoped
  `buckets` with their `count` and codes. The per-bucket IAM reads of `public-access` have a time
  budget (the task's 600s less 10s): a bucket not started in time is a `timeout` error scoped to
  it, the run is `partial` and `complete` is false.
- `blockedByPrevention: true`: the grant is there but public access prevention is `enforced`, so
  nobody can use it. `publicAccessPrevention: inherited` does not block anything on its own (an
  organisation policy may still).
- A conditional binding (`conditional: true`) is public only while its condition holds; read the
  policy for the condition.
- `buckets.items`, `access.items` or `sizes.items` empty with `total` 0: the project has no
  buckets. A bucket missing from `items` while `total` counts it has an error scoped to its name
  (it could not be read or measured); `omitted` above zero means the list was cut at `maxItems`:
  raise the setting or target buckets one by one.
- `sizeBytes` comes from Cloud Monitoring, metric `storage.googleapis.com/storage/v2/total_bytes`
  (bytes by storage class and object type), asked once for the whole project (the newest point of
  the last 6 hours, summed over classes and types: noncurrent versions, soft-deleted objects and
  incomplete multipart uploads are in it, shown apart in `byType`). Google measures it once a day
  and shows it with a delay of up to 12 minutes, so a size is up to a day old, and it does not
  track **buckets with no objects**: a bucket without a series has an unknown size, not zero. On a
  project target such buckets are left out of `sizes`, counted in `notMeasured`, and named (up to 50)
  in one `not_found` error scoped `monitoring`. A `gcp/bucket` target without a series is measured
  with `gcloud storage du` instead (`source: du`, live objects only, bounded by `sizeTimeout`): `du`
  walks every object, which is why it is the fallback and never the method for a project. If the
  Cloud Monitoring API is disabled or the identity lacks `monitoring.timeSeries.list`, the task
  fails (`dependency` or `permission_denied`) rather than guessing.
- Failures carry a code, classified from gcloud's own words (see `gcp:gcp-base`): `permission_denied`
  (HTTP 403 and 401, an unusable credential: `Reauthentication required`), `not_found` (no such
  bucket or project), `rate_limited` (HTTP 429, `RESOURCE_EXHAUSTED`), `dependency` (the Cloud
  Storage or Cloud Monitoring API is disabled), `unreachable`, `timeout`, `internal`. A project target whose bucket
  list fails is `failed`; if every bucket fails, the first failure is the result.
- Not read: operation rates (the classic bundle's fourth task, a request-count query). The
  Monitoring read that `bucket-size` makes is the way to add them later.

## What to try next

- Public grant on a bucket that should be private: remove the `allUsers` binding
  (`gcloud storage buckets remove-iam-policy-binding`) and set public access prevention to
  `enforced`; run `gcp:gcp-bucket-health/public-access` again.
- Large or growing bucket: `bucket-config` shows whether it has lifecycle rules, versioning or a
  retention policy keeping objects alive.
- A task came back `permission_denied` or `dependency`: run `gcp:gcp-base/discover` to see what the
  connection can list at all.
- The bucket belongs to a workload on GKE: pod-level questions are `kubernetes:k8s-workload-health`.

## Tests

The plugin's fixtures are recorded against a gcloud/curl stub with canned answers
(`plugins/gcp/tests/stub/`), not a Google account: no ring has a GCP credential. The gcloud
answers are what the pinned gcloud (592.0.0) itself printed against a local fake of the REST APIs
(formatting, padding, error lines and exit codes are real; the shapes follow the REST reference);
the Cloud Monitoring reply follows its REST reference. Re-record against a real project when a
credential exists.
