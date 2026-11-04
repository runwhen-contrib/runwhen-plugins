# gcloud stub

The gcp plugin's fixtures are recorded without a Google account (lane F plan, Appendix C5): no ring
has a GCP credential, and this machine's own `gcloud` is logged in to shared infrastructure and
must never be used. So `rwtask test --record` runs against a **stub** `gcloud` that answers from
canned scenarios. Nothing here contacts Google.

```
bin/gcloud        the stub (python3 -I, standard library only)
bin/gsutil        a guard: gsutil is deprecated, the plugin uses `gcloud storage`; it exits 2
harness.py        runs a command with the stub first on PATH and a throwaway CLOUDSDK_CONFIG
scenarios/<name>/calls.yaml   what each argv answers: stdout file, stderr, rc, source
```

## The real gcloud must not be reachable

* Always go through `harness.py`. It makes `CLOUDSDK_CONFIG` a **new empty temporary directory**
  (`gcp-stub-config-*`), drops every `GOOGLE_*`, `GCLOUD_*` and `CLOUDSDK_*` variable of the caller
  (credential files, access tokens, projects, the active configuration), points the metadata server
  at a closed port, and puts `bin/` first on `PATH`. It refuses to run if `gcloud` or `gsutil` does
  not resolve into `bin/`.
* So even a call that somehow missed the stub would reach a gcloud with **no account, no credential
  and no property**, and fail; it could not use this machine's login.
* The stub itself refuses to run when `CLOUDSDK_CONFIG` is not such a directory (exit 2).
* `tests/unit/test_stub_harness.py` asserts all of this, including that the harness sets
  `CLOUDSDK_CONFIG` to a temporary directory and drops the caller's credentials.
* Never record with a plain `rwtask test --record` and the machine's PATH.

## Recording a fixture

From the repository root, with the `rwtask` of the shared venv:

```
python3 plugins/gcp/tests/stub/harness.py healthy -- \
  rwtask test gcp-bucket-health/bucket-config --plugin-dir plugins/gcp --record \
  --scenario healthy --resource gcp/projects/shop-prod-1234
```

Replay needs neither the stub nor `gcloud` on PATH: `rwtask test gcp-bucket-health --plugin-dir
plugins/gcp` answers from the recorded calls.

Why the scenario is chosen with `GCP_STUB_SCENARIO` and not `RW_STUB_SCENARIO`: the task runner
drops every `RW_*` variable from the environment of a task, so the stub, a grandchild of the task,
would never see it.

## Scenarios

| Scenario | What the stub plays |
| --- | --- |
| `healthy` | project `shop-prod-1234` with 3 private buckets, none over the size threshold |
| `broken` | the three plus `shop-public-assets` (`allUsers` in IAM), `shop-legacy` (legacy ACL grants `allUsers`, a conditional `allAuthenticatedUsers` binding) and `shop-archive` (1.5 TiB) |
| `forbidden` | `PERMISSION_DENIED` (HTTP 403) on every call |
| `partial-iam` | the `broken` project, but `get-iam-policy` is denied |
| `not-found` | project `no-such-project-1` and bucket `nope` do not exist |
| `throttled` | `RESOURCE_EXHAUSTED` / HTTP 429 on every call |
| `unauthenticated` | `Reauthentication required` on every call |

Project, buckets, members and the service account are invented.

## Matching

The stub drops `--format=json`, `--format json`, `--quiet` and `-q` from the argv it was given and
compares the rest **exactly and in order** (nothing is sorted: flag order is part of the question,
and the plugin always writes `--flag=value`). An argv with no entry exits **2** and says so on
stderr, naming the argv and the file to extend, so a task that drifted fails loudly and never
receives an empty answer.

## calls.yaml

A list of entries, `argv` (without the program name), `rc`, `stdout` (a file in the scenario
directory), `stderr` and `source`. Every value is one line of **JSON**, so the stub (which has no
YAML parser) and a real YAML parser read the same file:

```
- argv: ["storage", "buckets", "describe", "gs://shop-assets", "--raw"]
  rc: 0
  stdout: "bucket-shop-assets.json"
  source: "https://cloud.google.com/storage/docs/json_api/v1/buckets#resource"
```

`source` is the reference page whose shape the answer follows (JSON files cannot carry a comment,
so the citation sits next to the file in its call). The shapes are the REST resources
(`--raw` prints the API's own JSON for `gcloud storage buckets`) and the error texts are the ones
gcloud prints (`ERROR: (gcloud.<command>) HTTPError 403: ...`); they are written from the
references, not recorded from an account. **Re-record against a real project when a credential
exists** (plan Appendix C5); until then the SKILL.md "Tests" section says the plugin is
stub-recorded.
