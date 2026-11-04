# The `az` stub

No ring has an Azure credential and this repository's CI has no az account, so the azure plugin's
fixtures are recorded against a stand-in for the CLI (lane F plan, Appendix C5). `bin/az` is a
python script with no dependencies that answers `az` commands from canned files.

## Recording: `record.sh` is the only way

```
plugins/azure/tests/stub/record.sh                                  # every fixture
plugins/azure/tests/stub/record.sh azure-kv-health/audit-failures   # one task
plugins/azure/tests/stub/record.sh azure-kv-health/audit-failures/dedicated   # one fixture
```

`RWTASK` names the rwtask binary (default: `rwtask` on the PATH). The script puts `tests/stub/bin`
first on the PATH, names the stub scenario with `AZURE_STUB_SCENARIO`, and before it records anything
checks that `az` answers `az version` with the stub marker (`"rw-azure-stub": true`): a recording
made with the real CLI would call a real subscription. It records each fixture with `--force`, keeps
the hand-written `expect:` block the fixture already has (a recording writes only `status` and
`errorCodes`; the `outputs` that prove the scenario are written by hand), and then replays every
fixture of the plugin with the stub OFF the PATH, so a fixture that only works while the stub is
there fails. A fixture that is new is recorded with the template `expect:`; write its outputs and run
the script again. A bare `rwtask test --record` is never a recording path here: it runs whatever `az`
comes first on the PATH.

Replaying (`rwtask test azure-kv-health --plugin-dir plugins/azure`) does not use the stub: replay
answers from the calls recorded in the fixture. The stub is for recording and for the unit tests.

## How a call is answered

`bin/az` takes the arguments after `az`, drops `--output json` / `-o json` (a caller adds it), and
looks for the first call in `scenarios/$AZURE_STUB_SCENARIO/calls.yaml` whose `argv` equals the rest.

- The scenario variable is `AZURE_STUB_SCENARIO` (`AZ_STUB_SCENARIO` is an alias), and the log
  variable `AZURE_STUB_LOG` (`AZ_STUB_LOG`). **Not** `RW_*`: the SDK runner removes every `RW_*`
  variable from the environment of a task script, so such a name never reaches the stub.
- **The stub refuses to run when the scenario is not named** (exit 2, nothing on stdout), `az version`
  included: no default scenario ever answers by accident.
- `az version` is answered by the stub itself with a marker key, `rw-azure-stub`, that `record.sh`
  checks.
- `calls.yaml` is JSON written as YAML (the stub has no YAML parser); lines starting with `#` are
  comments. Each call:

| key | meaning |
| --- | --- |
| `argv` | the arguments after `az`; `<ts>` inside an entry matches one timestamp, RFC 3339 UTC with whole seconds (`2026-11-04T01:00:00Z`) and nothing else: the start and end of a metrics window, which depend on when the run happened. A `*` is just a star |
| `stdout` | a file in the scenario directory, printed to stdout (omitted for a failing call) |
| `stderr` | printed to stderr, whatever the exit code (a successful call may carry a warning) |
| `rc` | the exit code |
| `source` | the documentation URL the canned shape follows; required |

An argv with no canned call exits **2** and prints `az stub: no canned response in scenario ... for:
az <argv>`, so a test notices a command it did not expect.

## Scenarios

One subscription (`4b1d1f7e-6f0a-4c5e-9a43-0c8a2d9e5b71`) with three resource groups (`rg-orders-dev`,
`rg-orders-prod`, and `RG-Platform`, mixed case on purpose: the identity lower-cases group names) and
two Key Vaults, `kv-orders-prod` (healthy) and `kv-orders-dev` (broken). Every scenario answers the
commands of both skills for one target vault.

| scenario | target vault | what it shows |
| --- | --- | --- |
| `healthy` | `kv-orders-prod` | nothing wrong; `az account show` prints a warning on stderr and exits 0 |
| `broken` | `kv-orders-dev` | Resource Health `Degraded`, availability dips, no purge protection, public network access, expired and expiring items (and a certificate-managed secret), audit failures, a latency spike |
| `no-diagnostics` | `kv-orders-dev` | as `broken`, with no diagnostic settings |
| `forbidden` | `kv-orders-dev` | no role assignment: `AuthorizationFailed` on every ARM call, `Forbidden` on the data plane |
| `data-plane-forbidden` | `kv-orders-dev` | the control plane works, the item lists are `Forbidden` and the text names the action to grant (Reader without a Key Vault data role) |
| `secrets-forbidden` | `kv-orders-dev` | as `broken`, but listing secrets is `Forbidden`: the other kinds can still be read |
| `not-found` | `nope` | `ResourceNotFound` for the vault in its group, and an unknown subscription |
| `throttled` | `kv-orders-dev` | ARM `TooManyRequests` on every call |
| `category-group` | `kv-orders-dev` | the audit log is enabled through the `allLogs` category group (`category` is null); only the audit calls |
| `dedicated` | `kv-orders-dev` | the audit log goes to a resource-specific table (`logAnalyticsDestinationType: Dedicated`), so it is queried in `AZKVAuditLogs`; only the audit calls |
| `group-forbidden` | (discover) | `rg-orders-dev`'s vaults cannot be listed: a `forbidden` partition and a partial run, the other groups complete |

## Where the shapes and texts come from

Canned CLI output must come from the pinned CLI, not from memory (plan Appendix C7).

- `keyvault-show-*`, `keyvault-list-*`, `group-list.json` and `cloud-show.json` are rendered by the
  pinned az 2.92.0 itself: `capture/render.py` runs inside the `azure@1` image (`--network none`),
  feeds the ARM REST body Microsoft documents into the azure-mgmt model az uses, and prints it
  through az's own serialization (unset fields are `null`, a private endpoint connection's state
  sits on the connection, `resourceGroup` is added, keys are sorted). `az cloud show` is run as is.
  See the docstring of `capture/render.py` for the command.
- The stderr texts for the sign-in, `account set`, subscription-not-found cases were captured from
  az 2.92.0 in the image or read from its source (`_profile.py`). `keyvault list` with
  `--resource-type vault` is in az's `custom.py`: without it az lists vaults and managed HSMs and
  swallows an error from either.
- `AuthorizationFailed`, `Forbidden` (with the `Action:` and `Resource:` lines of a Key Vault data
  plane refusal), `ResourceNotFound` and `TooManyRequests` follow the wording Microsoft documents and
  az prints. The metrics, diagnostic settings, Log Analytics, Resource Health and item list shapes
  follow the reference pages named in each call's `source` and have not been re-rendered by the CLI.
- The audit queries' tables: `AzureDiagnostics` (`ResourceId`, `Category`, `httpStatusCode_d`) and
  `AZKVAuditLogs` (`_ResourceId`, `HttpStatusCode`), per
  https://learn.microsoft.com/azure/azure-monitor/reference/tables/azurediagnostics and
  https://learn.microsoft.com/azure/azure-monitor/reference/tables/azkvauditlogs.

They are representative, not recordings of a real account: re-record against a real subscription
when a credential exists (parked). Dates in the item lists (`expires`) are chosen around 2026-11-04,
the day of the first recording: re-recording on another day changes which items are expired or
expiring.
