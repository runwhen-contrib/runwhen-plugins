# aws CLI stub

No ring has an AWS credential and the machines that record fixtures have no `aws`. The fixtures of
this plugin are therefore recorded with the real runner against a stand-in for the CLI: `bin/aws`
answers each call from canned files under `scenarios/`. The plugin is **stub-recorded**; re-record
against a real account when a credential exists (plan Appendix C5).

```
# record one fixture, always through the harness (from the repo root)
plugins/aws/tests/stub/record.sh aws-lambda-health/function-config healthy healthy \
  aws/accounts/123456789012/regions/eu-west-1/lambda-functions/orders-api
#                                   <task>                       <fixture> <scenario> <resource>

# replay: the stub is not needed (replay uses the recorded calls)
rwtask test aws-lambda-health --plugin-dir plugins/aws

# check the canned CLI output against the pinned CLI in the runtime image (needs docker)
plugins/aws/tests/stub/verify/verify.sh
```

`record.sh` is the only way to record: it puts `bin` first on PATH, names the scenario in
`AWS_STUB_SCENARIO`, drops every other `AWS_*` variable of the caller (so no real account, profile or
endpoint reaches a recording), refuses to start unless `aws` resolves to the stub, and passes
`--record --force`. A recording starts with a minimal `expect` (status and error codes); add the
`expect.outputs` that matter after it. Never run a bare `rwtask test --record` here.

## How a call is answered

The scenario is named by `AWS_STUB_SCENARIO`, not `RW_STUB_SCENARIO`: the real runner strips every
`RW_*` variable from the task it starts, so a `RW_` name never reaches the stub under `rwtask run`
or `rwtask test --record`. The stub accepts no alias: with `AWS_STUB_SCENARIO` unset (or empty) it
refuses to run and exits 2, so a recording can never silently use a default scenario.

`bin/aws` (python3 -I, standard library only) normalises the argv, finds it in
`scenarios/$AWS_STUB_SCENARIO/calls.yaml` and prints the canned `stdout` file and `stderr` text and
exits with `rc`. Normalising drops `--output json`, `-o json` and `--no-cli-pager`, and replaces
the value after `--start-time` / `--end-time` with `<time>`; nothing else changes, and the order of
words is the order the skill sends them. An argv that is not in the scenario exits **2** with
`aws stub: scenario ... has no canned response for: ...` and the first known calls, so a test notices
a call nobody planned. `AWS_STUB_LOG=<file>` appends the normalised argv of every call as a JSON line.

`calls.yaml` is written as JSON (which is YAML), so the stub needs no YAML library:

```
{"calls": [{"argv": ["sts", "get-caller-identity", "--region", "us-east-1"],
            "doc": "https://docs.aws.amazon.com/cli/latest/reference/sts/get-caller-identity.html",
            "rc": 0, "stdout": "../_shared/sts-get-caller-identity.json", "stderr": ""}]}
```

`stdout` is a path relative to the scenario directory (`../_shared/` holds files several scenarios
use). `doc` is the reference page the shape of that file follows; every canned file is named by a
call with a `doc` (a test checks it). A file holds exactly what the CLI prints for the call,
including the `--query` projection the skill sends: the skills narrow `list-functions`,
`get-function-configuration` and `filter-log-events` with `--query`, so a response carries only the
fields they ask for (and never a function's environment variables); absent fields print as `null`.
The projections always keep `NextToken`, as the CLI drops it otherwise.

## Scenarios

The account is 123456789012, the principal `user/rw-monitor`, the regions ap-south-1, eu-west-1,
us-east-1; the function of the health skill is `orders-api` in eu-west-1.

| Scenario | What it models |
| --- | --- |
| `healthy` | Two functions in eu-west-1, the others empty; `orders-api` Active on python3.12, invoked, no errors, no reserved concurrency, no matching log events |
| `broken` | `orders-api` on python3.8 with failed last update, KMS env error, reserved concurrency 5 and a FAILED provisioned config on alias `live`, 8% errors, throttles, durations at the timeout; 50 error events (credential-looking text, a 1200-character line) with a `NextToken` |
| `forbidden` | sts works; ec2 `UnauthorizedOperation`, lambda `AccessDeniedException` |
| `not-found` | `get-function-configuration` of `nope`: `ResourceNotFoundException` |
| `no-log-group` | `report-batch` exists, has never run: no datapoints (idle, not healthy), `ResourceNotFoundException` for the log group |
| `logs-forbidden` | configuration readable, `logs:FilterLogEvents` denied |
| `throttled` | `TooManyRequestsException` / `Throttling` / `ThrottlingException` after `(reached max retries: 2)` for one region, one metric and the logs |
| `expired` | `ExpiredToken` (STS) / `ExpiredTokenException` |
| `partial` | an SCP denies `lambda:ListFunctions` in ap-south-1 (explicit deny wording) |
| `paged` | the eu-west-1 function list and its instance list take two pages each (`NextToken` then `--starting-token`) |
| `ec2-denied` | lambda lists fine, `ec2:DescribeInstances` is `UnauthorizedOperation` in every region (discover: the instance list is forbidden, the function list stands) |

The `aws-ec2-health` scenarios are prefixed `ec2-`. Three instances in eu-west-1: `i-0a1b2c3d4e5f60718`
(`orders-web-1`, t3.medium), `i-0b7c1d2e3f4a5b6c7` (`orders-batch-2`, t3.small) and
`i-0c0ffee0123456789` (`batch-runner`, m5.large). `healthy`, `broken` and `paged` also list them for discover.

| Scenario | What it models |
| --- | --- |
| `ec2-healthy` | `orders-web-1` running: both checks ok, no events, 4 datapoints of CPU (max 41 percent), network, EBS and a CPU credit balance around 490 |
| `ec2-broken` | `orders-batch-2` running: the system check impaired since 00:41, a pending `instance-retirement` and a completed `system-reboot`, CPU near 100 percent, credit balance down to 3.2, IMDSv2 optional, two volumes, tags `db_password=hunter2` and a commit-sha-shaped `build` (both withheld from the output) |
| `ec2-stopped` | `batch-runner` stopped by the user: checks `not-applicable`, state reason `Client.UserInitiatedShutdown`, no datapoints for any metric (no data, not zero) |
| `ec2-forbidden` | `UnauthorizedOperation` for `DescribeInstances` and `DescribeInstanceStatus` |
| `ec2-not-found` | `InvalidInstanceID.NotFound` for both |
| `ec2-throttled` | `RequestLimitExceeded` for `DescribeInstanceStatus`; `Throttling` for the `NetworkIn` metric (the rest stand) |
| `ec2-metrics-forbidden` | the instance reads, every `GetMetricStatistics` call `AccessDenied` |

## Where the canned output comes from (plan C7)

Canned output is the pinned CLI's, not typed from memory. `verify/verify.sh` runs aws CLI
**2.39.3** in the runtime image (`rwp-runtime-aws:dev`, as uid 65532 on a read-only root, no network
but loopback) against a fake HTTP endpoint that answers in each service's own wire protocol
(Lambda rest-json, Logs json 1.1, CloudWatch json 1.0, STS query, EC2 ec2; the EC2 XML answers are
encoded from the CLI's own service model, member names and shapes included), once for every call of
every scenario. It turns each canned file back into the API response its `--query` projection comes
from (adding what the projection must drop, such as a function's environment variables) and
compares what the CLI prints with the file: the key order, padding, `null`s, number formatting, and
for failures the stderr text and exit code. `verify.sh --write` replaces a file with the CLI's
output. It found that `ec2 describe-regions` prints `OptInStatus, RegionName, Endpoint` (not the
documented order) and that the CLI prints **nothing** for an empty `get-function-concurrency`
response (`lambda-get-function-concurrency.json` in `healthy` is an empty file on purpose). Only the
`paged` scenario's `list-functions` answers are skipped: their `NextToken` is the CLI's own
encoding of its page state. Failure stderr is a blank line, `aws: [ERROR]: An error occurred
(<Code>) when calling the <Operation> operation: <message>`, exit code 254, with `(reached max
retries: 2)` after the operation when the CLI gave up retrying a throttle; rc 253 `Unable to locate
credentials` and rc 255 `Could not connect to the endpoint URL` are exercised by the unit tests.
The IAM wording of `<message>` follows
https://docs.aws.amazon.com/IAM/latest/UserGuide/troubleshoot_access-denied.html.
