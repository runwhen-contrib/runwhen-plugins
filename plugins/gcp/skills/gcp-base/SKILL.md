---
name: gcp-base
description: >-
  The Google Cloud integration's own skill. discover reads a project and lists its Cloud
  Storage buckets (name, location, storage class, creation time) and its Cloud Functions (id,
  region, generation, state, runtime) and reports the project id as the root's canonical key. Use to see what a gcp connection holds, or to find out why
  another gcp skill reports permission_denied, not_found or dependency (a disabled API).
runwhen:
  role: [discovery]
  appliesTo:
    types: [gcp/project]
  tags: [discovery]
---

## When to use

Reach for `discover` to see what a project holds: its number and state, every Cloud Storage
bucket and every Cloud Function (both generations, all regions) the identity may list. The
platform also runs it on its own every 15 minutes, to keep the `gcp/bucket` and
`gcp/cloud-function` resources that the other gcp skills target. It only reads: three gcloud calls,
`gcloud projects describe`, `gcloud storage buckets list --raw`, `gcloud functions list` and, once that works, `gcloud functions regions list` (v1 and `--gen2`)
(which needs `cloudfunctions.functions.list` and `cloudfunctions.locations.list`), with the connection's
credential (`gcp/adc`: a service-account key, or the pod's own identity through the metadata
server), each billed to the project it reads (`--billing-project`, which needs
`serviceusage.services.use`; an assumption to verify against a real project, parked P18).
`discover` lists buckets with the FULL projection, so it needs `storage.buckets.getIamPolicy` too.
The `commandTimeout` setting (default 60s) bounds each call; `discover` has a 600s task
timeout, which covers the calls and the push to the platform: the push is clamped to what is left
of the budget (less a 10s margin), and a request that would start with nothing left fails with
`timeout`.

## What each task tells you

- `discover` returns `project` (id, number, display name, lifecycle state), `buckets`
  (`{items, total, omitted}`: `total` is every bucket found, `items` the first `maxItems`
  (default 200) and `omitted` how many were cut), `summary` (counts and per-step errors) and a
  `sync` artifact: the whole `rw.discovery.v1` sync the platform ingests, which always holds every
  bucket. `summary.root.canonicalKey` is the project id. When the run has the per-run
  `runwhen/sync` credential (`RW_SYNC_CREDENTIAL`) `discover` also pushes the sync to the
  platform: open, batches, commit. A push that fails fails the task; a commit the platform holds,
  or items it rejects, make the run `partial` with an `internal` error scoped `sync`. Without the
  credential (a local `rwtask run`) the artifact is all it produces.

## How to read the result

- `projectNumber`, `displayName` and `lifecycleState` all null, with an error scoped `project`
  (`permission_denied`, `dependency`, `rate_limited` or `not_found`): the project resource could
  not be had although the bucket list worked (the identity lacks `resourcemanager.projects.get`,
  the Resource Manager API is off, the call was throttled, or the project is gone). The run is
  `partial`; the buckets stand. A response in a shape this task does not know (neither Resource
  Manager v1's `lifecycleState`/`name` nor v3's `state`/`displayName`) is an `internal` error
  scoped `project`, not silent nulls.
- Each type is read on its own. `buckets.read` or `functions.read` false, with an error scoped
  `buckets` / `functions` (`permission_denied`, `dependency` for a disabled API, `rate_limited`,
  ...): that list could not be read; the other type and the project stand and the run is `partial`.
  Its sync partition is `forbidden` / `failed` with a `reason`, so nothing is swept: it is never
  "the project has no functions" (or no buckets). Only when both lists fail does the task fail, with
  the bucket list's error. `items` empty with `total` 0 and `read` true is a project with none.
- The sync is project -> bucket and project -> region -> cloud-function: a region item for each
  region that holds a function (or was unreachable), and one `cloud-function` partition per region.
  The same function id in two regions is two functions. Every region Cloud Functions serves
  (`functions regions list`) gets a region item and a `complete` partition, with a count of 0 when
  it holds no function, so a region whose last function was deleted is swept; a region that was
  unreachable stays `failed`. If the region list itself cannot be read (an error scoped
  `functions`), the region partition is `failed` and no empty region is guessed.
- `functions.unreachableRegions` not empty: gcloud exits 0 but could not reach those regions
  (it only warns; `unknown` when it did not say which); their functions are missing, the run is
  `partial` (`unreachable`) and the partitions of those regions are `failed` (all of them, for
  `unknown`).
- `state` is the generation's own field: 2nd gen `state` (`ACTIVE`, `FAILED`, `DEPLOYING`, ...),
  1st gen `status` (`ACTIVE`, `OFFLINE`, ...); null when the row carries neither.
- `buckets.items` empty and `total` 0 with `read` true: the project has no buckets (an identity
  that may not list them gets `read: false` and an error; it never returns an empty list).
- `buckets.omitted` above zero: the list was cut at `maxItems`; raise the setting, or read the
  `sync` artifact for the whole list.
- `lifecycleState: DELETE_REQUESTED`: the project is scheduled for deletion.
- The run fails with a code, not an empty result, classified from gcloud's own words:
  - `permission_denied`: `PERMISSION_DENIED`, HTTP 403 and 401, `Permission '...' denied on
    resource`, and an unusable credential (`Reauthentication required`, `You do not currently
    have an active account`, `invalid_grant`): D64 has no separate code for authentication.
  - `not_found`: the project does not exist or was deleted (HTTP 404). Google often answers 403
    for a project the identity cannot see, so a mistyped project id may also read as
    `permission_denied`.
  - `rate_limited`: HTTP 429, `RESOURCE_EXHAUSTED`, `Quota exceeded`.
  - `dependency`: the Cloud Storage or Resource Manager API is disabled for the project
    (`SERVICE_DISABLED`), or `gcloud` is missing from the runtime.
  - `unreachable`: DNS, TLS and connection failures. `timeout`: the call outran
    `commandTimeout`, or `DEADLINE_EXCEEDED`. `invalid_input`: HTTP 400. Anything else is
    `internal`.

## What to try next

- Missing permission: grant a role that carries it (see the IAM permissions reference for
  `storage.buckets.list` and `resourcemanager.projects.get`), then run
  `gcp:gcp-base/discover` again.
- A function listed here is the target of `gcp:gcp-functions-health/function-state`,
  `gcp:gcp-functions-health/function-errors` and `gcp:gcp-functions-health/function-metrics`.
- A bucket listed here is the target of `gcp:gcp-bucket-health/bucket-config`,
  `gcp:gcp-bucket-health/public-access` and `gcp:gcp-bucket-health/bucket-size`.

## Tests

The plugin's fixtures are recorded against a gcloud/curl stub with canned answers
(`plugins/gcp/tests/stub/`), not a Google account: no ring has a GCP credential. The answers are
what the pinned gcloud (592.0.0) itself printed against a local fake of the REST APIs; re-record
against a real project when a credential exists.
