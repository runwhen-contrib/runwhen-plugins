---
name: gcp-bucket-health
description: >-
  Cloud Storage bucket health for a project or one bucket: bucket-config reads each bucket's
  settings (location, class, versioning, uniform access, public access prevention, retention,
  lifecycle, soft delete, customer-managed key); public-access finds grants to allUsers or
  allAuthenticatedUsers in IAM and legacy ACLs; bucket-size measures bytes per bucket with gcloud
  storage du and flags the ones over a threshold. Use for "is this bucket exposed", "what is it
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
output and one error format). Each gcloud call is bounded by the `commandTimeout` setting
(default 60s).

## What each task tells you

- `bucket-config` returns `buckets`: per bucket the location, location type, storage class,
  creation and update times, `versioning`, `uniformBucketLevelAccess`, `publicAccessPrevention`,
  `retention` (period and whether it is locked, or null), `lifecycleRuleCount`,
  `softDeleteSeconds`, `cmek` and the key's resource name, and up to 50 labels.
- `public-access` returns `access` per bucket: `publicGrant` (an IAM binding or a legacy ACL entry
  names `allUsers` or `allAuthenticatedUsers`), `publicViaIam`, `publicViaAcl`, the matching
  `iamBindings` (role, members, whether the binding is conditional) and `aclEntries`, and
  `blockedByPrevention` (a grant exists but public access prevention is enforced). `totals` counts
  `publicBuckets`, `unknown` and whether the count is `overThreshold` of `publicBucketThreshold`
  (default 0).
- `bucket-size` returns `sizes` per bucket, largest first: `sizeBytes`, `sizeTiB` and
  `overThreshold` of `usageThresholdTib` (default 0.5 TiB), and `totals` with the bytes in all and
  how many buckets were `notMeasured`.

## How to read the result

- `publicGrant: false` means neither IAM nor an ACL names a public member. IAM is always checked,
  with or without uniform bucket-level access; ACLs only count without it (with it they are
  disabled, `publicViaAcl` is false).
- `publicGrant: null` with a `permission_denied` error scoped to the bucket: the IAM policy could
  not be read (`storage.buckets.getIamPolicy`), so "not public" is unknown, not true. The run is
  `partial`. Treat `unknown` in `totals` the same way.
- `blockedByPrevention: true`: the grant is there but public access prevention is `enforced`, so
  nobody can use it. `publicAccessPrevention: inherited` does not block anything on its own (an
  organisation policy may still).
- A conditional binding (`conditional: true`) is public only while its condition holds; read the
  policy for the condition.
- `buckets.items`, `access.items` or `sizes.items` empty with `total` 0: the project has no
  buckets. A bucket missing from `items` while `total` counts it has an error scoped to its name
  (it could not be read or measured); `omitted` above zero means the list was cut at `maxItems`:
  raise the setting or target buckets one by one.
- `sizeBytes` is the sum of the live objects' sizes (`gcloud storage du`); noncurrent versions and
  soft-deleted objects are not in it, so a versioned bucket can cost more than it shows. A
  `timeout` error scoped to the bucket means `sizeTimeout` ran out on a large bucket; one scoped
  `budget` lists buckets never started because the task's 900s were nearly used up
  (`notMeasured`): measure those one at a time.
- Failures carry a code, classified from gcloud's own words (see `gcp:gcp-base`): `permission_denied`
  (HTTP 403 and 401, an unusable credential: `Reauthentication required`), `not_found` (no such
  bucket or project), `rate_limited` (HTTP 429, `RESOURCE_EXHAUSTED`), `dependency` (the Cloud
  Storage API is disabled), `unreachable`, `timeout`, `internal`. A project target whose bucket
  list fails is `failed`; if every bucket fails, the first failure is the result.
- Not read: operation rates (the classic bundle's fourth task) need the Cloud Monitoring API and
  are not part of this skill yet.

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

The plugin's fixtures are recorded against a gcloud stub with canned answers
(`plugins/gcp/tests/stub/`), not a Google account: no ring has a GCP credential. The answers
follow the REST reference shapes and gcloud's documented error texts; re-record against a real
project when a credential exists.
