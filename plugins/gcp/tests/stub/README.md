# gcloud and curl stubs

The gcp plugin's fixtures are recorded without a Google account (lane F plan, Appendix C5): no ring
has a GCP credential, and this machine's own `gcloud` is logged in to shared infrastructure and
must never be used. So `rwtask test --record` runs against **stubs** of `gcloud` and `curl` that
answer from canned scenarios. Nothing here contacts Google.

```
bin/gcloud        the gcloud stub (python3 -I, standard library only; logic in stublib.py)
bin/curl          the curl stub: the one Cloud Monitoring read of bucket-size
bin/gsutil        a guard: gsutil is not used and not in the image; exits 2
bin/bq            a guard, same
stublib.py        what the stubs share: scenario lookup, argv matching, the curl header check
harness.py        runs a command with the stubs first on PATH and a throwaway CLOUDSDK_CONFIG
record.sh         records one fixture through harness.py (the only way to record)
identities.json   the invented project, numbers, e-mail domains, buckets and the fake token; the
                  stubs and tests/unit/test_fixture_hygiene.py read the same list
scenarios/<name>/calls.yaml   what each argv answers: stdout file, stderr, rc, citations
capture/          capture.sh + capture.py: how the canned answers are produced (below)
```

## Recording a fixture (the only way)

From anywhere, with `rwtask` on `PATH` (or `RWTASK=/path/to/rwtask`):

```
plugins/gcp/tests/stub/record.sh <skill>[/<task>] <stub-scenario> [rwtask test args...]

plugins/gcp/tests/stub/record.sh gcp-bucket-health/bucket-config healthy \
    --resource gcp/projects/shop-prod-1234
plugins/gcp/tests/stub/record.sh gcp-bucket-health/bucket-size broken --scenario broken-bucket \
    --resource gcp/projects/shop-prod-1234/buckets/shop-archive
```

The fixture is named after the stub scenario unless the arguments carry their own `--scenario`.
`record.sh` always goes through `harness.py`; never run a bare `rwtask test --record`, which would
use this machine's real `gcloud`. Replay needs neither the stubs nor `gcloud` on `PATH`:
`rwtask test gcp-bucket-health --plugin-dir plugins/gcp` answers from the recorded calls. `rwtask
test` shims `gcloud` and `curl` by default; a skill that one day needs `bq` or `gsutil` must add
the tool to the runtime image on purpose and record and replay it with `--shim bq` (`rwtask test
... --shim bq`, given the same way to `--record` and to the replay), and give the stub a real
`bin/bq` instead of the guard.

## The real gcloud must not be reachable

* `harness.py` makes `CLOUDSDK_CONFIG` a **new empty temporary directory** (`gcp-stub-config-*`),
  drops every `GOOGLE_*`, `GCLOUD_*` and `CLOUDSDK_*` variable of the caller (credential files,
  access tokens, projects, the active configuration), points the metadata server at a closed port
  with `GCE_METADATA_ROOT` (the variable gcloud reads for its own metadata client) and sets
  `CLOUDSDK_CORE_CHECK_GCE_METADATA=false`, and puts `bin/` first on `PATH`. It refuses to run if
  `gcloud`, `curl`, `gsutil` or `bq` does not resolve into `bin/`.
* So even a call that somehow missed the stub would reach a gcloud with **no account, no credential
  and no property**, and fail; it could not use this machine's login. (The plugin also gives each
  gcloud child its own empty `CLOUDSDK_CONFIG` and blanks the access-token, token-file and account
  variables; see `gcp_base/gcloud.py`.)
* The stubs refuse to answer (exit 2) unless `GCP_STUB_HARNESS=1`, which only `harness.py` sets.
* `tests/unit/test_stub_harness.py` asserts all of this.

Why the scenario is chosen with `GCP_STUB_SCENARIO` and not `RW_STUB_SCENARIO`: the task runner
drops every `RW_*` variable from the environment of a task, so the stub, a grandchild of the
task, would never see it.

## Scenarios

| Scenario | What the stubs play |
| --- | --- |
| `healthy` | project `shop-prod-1234` with 3 private buckets, none over the size threshold |
| `broken` | the three plus `shop-public-assets` (`allUsers` in IAM), `shop-legacy` (legacy ACL grants `allUsers`, a conditional `allAuthenticatedUsers` binding) and `shop-archive` (1.5 TiB) |
| `forbidden` | `PERMISSION_DENIED` (HTTP 403) on every call |
| `throttled` | `RESOURCE_EXHAUSTED` / HTTP 429 on every call |
| `unauthenticated` | gcloud has no credential: "You do not currently have an active account selected" |
| `api-disabled` | `SERVICE_DISABLED` (HTTP 403) on every call: the Cloud Storage and Resource Manager APIs are off |
| `not-found` | bucket `nope` does not exist (a missing *project* answers 403, not 404, so there is no project-level not-found) |
| `project-denied` | the bucket list works, `projects describe` is denied: discover is partial |
| `partial-throttled` | the `broken` project, but the IAM read of `shop-logs` is throttled: public-access is partial |
| `du-fallback` | Cloud Monitoring has no series for `shop-assets` (10^12 + 5 bytes): bucket-size falls back to `gcloud storage du` |
| `monitoring-disabled` | the bucket list works, the Cloud Monitoring API is off: bucket-size fails with `dependency` |

Project, buckets, members and the service account are invented. The token is
`identities.json`'s fake one.

## Matching

The gcloud stub drops `--format=json`, `--format json`, `--quiet` and `-q` from the argv it was
given and compares the rest **exactly and in order** (nothing is sorted: flag order is part of the
question, and the plugin always writes `--flag=value`; every call carries
`--billing-project=<project>`). The curl stub compares the argv with the Monitoring query's
`interval.startTime` / `interval.endTime` values masked (`<T>`): they move with the run's
reference time, so the recorded fixture keeps the real values and the canned call a placeholder.
The curl stub also **checks the credential**: the call must pass it as one `-H @<file>`, a file of
mode 0600 in the working directory that holds the fake token and nothing on the command line.

An argv with no entry exits **2** and says so on stderr, naming the argv and the file to extend, so
a task that drifted fails loudly and never receives an empty answer.

## calls.yaml

A list of entries: `tool` (`curl`; absent for gcloud), `argv` (without the program name), `rc`,
`stdout` (a file in the scenario directory), `stderr`, `source` (the REST reference the answer
follows) and `cli_source` (the file of the **pinned gcloud** that formats it, `gcloud 592.0.0:
googlecloudsdk/...`, or the curl behaviour). Every value is one line of **JSON**, so the stub
(which has no YAML parser) and a real YAML parser read the same file:

```
- argv: ["storage", "buckets", "describe", "gs://shop-assets", "--raw", "--billing-project=shop-prod-1234"]
  rc: 0
  stdout: "bucket-shop-assets.json"
  source: "https://cloud.google.com/storage/docs/json_api/v1/buckets/get"
  cli_source: "gcloud 592.0.0: googlecloudsdk/command_lib/storage/resources/resource_util.py ..."
```

## Where the canned answers come from: the real gcloud

Canned output is **not typed from memory** (a hand-typed `du` line once hid a parse failure on every
bucket of 1 TB and up). `capture/capture.sh` regenerates `scenarios/*` by running the **real gcloud
592.0.0 of the runtime image** (`rwp-runtime-gcp:dev`, built from `runtimes/gcp`) with
`--network none` against a local fake of the Cloud Storage and Resource Manager REST APIs
(`CLOUDSDK_API_ENDPOINT_OVERRIDES_*`), so the JSON formatting, the timestamp rendering
(`2025-03-14T09:21:05.123000+00:00`), the `du` padding (`'{size:<13}{url}'`: a 13-digit size runs
into `gs://`), the error lines and the exit codes are gcloud's own. The fake world (invented
project, buckets, members; REST shapes from the reference pages) and the fault bodies live in
`capture/capture.py`; run `capture/capture.sh` after changing either. Two things are not gcloud's:
the Cloud Monitoring reply (curl is not a Google tool: written from the REST reference, with curl's
`--fail-with-body` behaviour for failures) and the one sentence gcloud appends when its credential
is an access token from the environment (stripped, see `HINT` in `capture.py`).

**Re-record against a real project when a credential exists** (plan Appendix C5); until then the
SKILL.md "Tests" sections say the plugin is stub-recorded.
