---
name: aws-base
description: >-
  The AWS integration's own skill. discover reads an account's identity and, in
  every region the account has enabled, lists the Lambda functions (runtime,
  memory, timeout, state, last update status), and reports the account id as the
  root's canonical key; it fails when the credential is for another account than
  the target. Use to see what AWS holds in an account and which
  regions could not be read, or to find out why another aws skill cannot find a
  function.
runwhen:
  role: [discovery]
  appliesTo:
    types: [aws/account]
  tags: [discovery, inventory]
---

## When to use

Reach for `discover` to see which regions of an account hold Lambda functions, what runtime each
runs and whether it is in a good state, or when an aws skill came back `not_found` or
`permission_denied` and you want to know whether the account, the region or the function is the
problem. The platform also runs it on its own, every 15 minutes, to keep the inventory current.

It only reads, through the aws CLI v2 (`--output json --no-cli-pager`, every call with an explicit
`--region`), with the credential of the connection. It needs `lambda:ListFunctions` in each region
and, only while the `regions` setting is empty, `ec2:DescribeRegions` (the regions the account has
enabled); `sts:GetCallerIdentity`, which names the account, needs no permission. The task declares
only `lambda:ListFunctions`: a permission list cannot say "only when", so `ec2:DescribeRegions` is
a conditional note here and in the task description, and a run that lacks it fails with
`permission_denied` naming it. The `regions` setting lists the regions to read instead of asking
EC2.

The credential carries no region (a region is not a secret and would be redacted from every output
that holds one). The two calls with no region of their own, `sts get-caller-identity` and
`ec2 describe-regions`, pass the `defaultRegion` setting (default `us-east-1`) as `--region`; use
another partition's region there for GovCloud, China or the sovereign cloud (`eusc-de-east-1`).
Each region is read with its own `--region`, one call at a time, inside a 420 s budget for the
whole loop: a region the budget did not reach is `failed` with `timeout`, never empty.

The account the credential reaches must be the target's: when the target is named by a 12-digit
account id and `sts get-caller-identity` says another account, the run fails with `permission_denied`
"credential is for account X, target is Y" before any region is read, so one account's functions
are never synced under another's root.

`list-functions` answers with each function's environment variables. The call is narrowed with
`--query` to a handful of fields, so they never reach this task, the sync or its recording.

## What each task tells you

- `discover` returns `account` (the account id, the principal the credential runs as, the home
  region used for account-level calls, how many regions were read and whether that was every
  enabled region), `regions` (each region read, its `status` and function count), `summary` (counts
  and per-region errors) and a `sync` artifact: the whole `rw.discovery.v1` sync the platform
  ingests. `summary.root.canonicalKey` is the account id, so two connections to one account are one
  root. `regions` is `{items, total, omitted}`: `items` the first `maxItems` (default 200), `omitted`
  how many were cut; only that list is cut, the `sync` artifact holds everything. Every region that
  was tried is a region item with its own function partition, so a region that was read and holds
  no function is `complete` with count 0: functions an earlier run found there are swept when they
  are gone. When the run has
  the per-run `runwhen/sync` credential (`RW_SYNC_CREDENTIAL`) `discover` also pushes the sync to the
  platform: open, batches, commit. A push that fails fails the task; a commit the platform holds, or
  items it rejects, make the run `partial` with an `internal` error scoped `sync`. Without the
  credential (a local `rwtask run`) the artifact is all it produces.

## How to read the result

- A region that was read and holds no function is not an error: `regions` lists it with
  `functions: 0` and the sync has its region item and a `complete` function partition of count 0.
- Regions with `status: forbidden`: the credential may not list Lambda functions there (a service
  control policy, a permission boundary, a policy that names regions). They are ONE
  `permission_denied` error scoped `regions` that lists them all (an account with an SCP that
  denies most regions would otherwise fill the error list); the run is `partial` and every other
  region stands. Each keeps its region item with `status: {discovery: forbidden}` and a `forbidden`
  function partition, so the platform does not delete what an earlier run found there. To stop
  reading them, set the `regions` setting to the regions you use.
- `status: failed` with a `rate_limited`, `timeout`, `unreachable` or `internal` error: the list
  could not be read to its end. Retried runs usually fix `rate_limited`. A list is followed page by
  page (`--max-items` with `NextToken`); one that is still not at its end after 100 pages is
  `failed` with `internal`, never reported as complete.
- `regionsComplete: false`: the `regions` setting narrowed the scan; the sync's region partition is
  `excluded`, so regions outside the list are neither reported nor removed. This is also the way to
  drop the `ec2:DescribeRegions` need.
- Opt-in regions the account has not enabled are not in `ec2 describe-regions` and are not read.
- The run fails with a code, not an empty result, classified from the error code the CLI prints
  (`An error occurred (AccessDeniedException) ...`), not from the exit code alone:
  - `permission_denied`: `AccessDenied*`, `UnauthorizedOperation`, `ExpiredToken*`,
    `InvalidClientTokenId`, `UnrecognizedClientException`, `SignatureDoesNotMatch`,
    `InvalidIdentityToken`, `IDPCommunicationError`, `RegionDisabledException`, an IRSA
    `AssumeRoleWithWebIdentity` that is not authorized, no credentials. An expired or invalid
    credential fails the run at `sts get-caller-identity`, before any region.
  - `not_found`: `ResourceNotFoundException`, `NoSuchEntity`.
  - `rate_limited`: `Throttling*`, `TooManyRequestsException`, `RequestLimitExceeded`; the CLI has
    already retried twice.
  - `unreachable`: cannot connect to the endpoint, DNS, TLS. `timeout`: connect or read timeout.
  - `invalid_input`: a malformed region or parameter. `dependency`: no `aws` in the runtime.
    Anything else is `internal`.

## What to try next

- Regions are `forbidden`: ask who owns the service control policy, or set `regions` to the regions
  that matter; then run `discover` again.
- A function is listed here: run `aws:aws-lambda-health/function-config`, then `function-metrics`
  and `function-errors`, against it.
- A function that Lambda runs inside EKS or on a node you also manage: pod-level questions belong
  to `kubernetes:k8s-workload-health`.

## Tests

The plugin is **stub-recorded**: no ring has an AWS credential, so the fixtures were recorded with
the real runner against a stand-in for the CLI (`plugins/aws/tests/stub`, see its README) whose
canned responses follow the AWS CLI reference and whose error text is the CLI's own. They check the
skill's logic and its handling of the CLI's output, not an AWS account; they are re-recorded
against a real account when a credential exists.
