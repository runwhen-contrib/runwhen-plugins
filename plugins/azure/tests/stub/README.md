# The `az` stub

No ring has an Azure credential and this repository's CI has no az account, so the azure plugin's
fixtures are recorded against a stand-in for the CLI (lane F plan, Appendix C5). `bin/az` is a
python script with no dependencies that answers `az` commands from canned files:

```
PATH=plugins/azure/tests/stub/bin:$PATH AZ_STUB_SCENARIO=healthy \
  rwtask test azure-kv-health/availability --plugin-dir plugins/azure --record \
  --scenario healthy --resource azure/subscriptions/4b1d1f7e-6f0a-4c5e-9a43-0c8a2d9e5b71/keyvaults/kv-orders-prod
```

Replaying a recorded fixture (`rwtask test azure-kv-health --plugin-dir plugins/azure`) does not use
the stub: replay answers from the calls recorded in the fixture. The stub is only for recording
and for the unit tests.

## How a call is answered

`bin/az` takes the arguments after `az`, drops `--output json` / `-o json` (a caller adds it), and
looks for the first call in `scenarios/$AZ_STUB_SCENARIO/calls.yaml` whose `argv` equals the
rest. The scenario is `$AZ_STUB_SCENARIO`. **Not** `RW_STUB_SCENARIO`: the SDK runner removes every `RW_*`
variable from the environment of a task script, so a variable with that prefix never reaches the stub
under `rwtask run` or `rwtask test` (the stub still reads `RW_STUB_SCENARIO` when it is run directly).
`calls.yaml` is JSON written as YAML (the stub has no YAML parser); lines starting with `#`
are comments. Each call:

| key | meaning |
| --- | --- |
| `argv` | the arguments after `az`; a `*` inside an entry matches any text (the start and end time of a metrics window, which depend on when the run happened) |
| `stdout` | a file in the scenario directory, printed to stdout (omitted for a failing call) |
| `stderr` | printed to stderr, whatever the exit code (a successful call may carry a warning) |
| `rc` | the exit code |
| `source` | the documentation URL the canned shape follows; required |

An argv with no canned call exits **2** and prints `az stub: no canned response in scenario ...
for: az <argv>`, so a test notices a command it did not expect. `AZ_STUB_LOG=<file>` appends each
argv (JSON, one per line) to that file.

## Scenarios

One subscription (`4b1d1f7e-6f0a-4c5e-9a43-0c8a2d9e5b71`) with two Key Vaults, `kv-orders-prod`
(healthy) and `kv-orders-dev` (broken). Every scenario answers the commands of both skills for one
target vault.

| scenario | target vault | what it shows |
| --- | --- | --- |
| `healthy` | `kv-orders-prod` | nothing wrong; `az account show` prints a warning on stderr and exits 0 |
| `broken` | `kv-orders-dev` | Resource Health `Degraded`, availability dips, no purge protection, public network access, expired and expiring items, audit failures, a latency spike |
| `no-diagnostics` | `kv-orders-dev` | as `broken`, with no diagnostic settings |
| `forbidden` | `kv-orders-dev` | no role assignment: `AuthorizationFailed` on every ARM call, `Forbidden` on the data plane |
| `data-plane-forbidden` | `kv-orders-dev` | the control plane works, the item lists are `Forbidden` (Reader without a Key Vault data role) |
| `secrets-forbidden` | `kv-orders-dev` | as `broken`, but listing secrets is `Forbidden`: the other kinds can still be read |
| `not-found` | `nope` | `The Vault 'nope' not found within subscription.` and an unknown subscription |
| `throttled` | `kv-orders-dev` | ARM `TooManyRequests` on every call |

## Where the shapes and texts come from

The JSON follows the reference page named in each call's `source`; the field names and nesting are
those of `az ... -o json` (the control-plane commands) or the REST response (`az rest`). The
stderr texts for the sign-in, `keyvault show` not-found and subscription-not-found cases were
captured from az 2.92.0 in the azure@1 image or read from its source; `AuthorizationFailed`,
`Forbidden` and `TooManyRequests` follow the wording Microsoft documents and az prints. They are
representative, not recordings of a real account: re-record against a real subscription when a
credential exists (parked). Dates in the item lists (`expires`) are chosen around 2026-11-04, the
day of the first recording: re-recording on another day changes which items are expired or expiring.
