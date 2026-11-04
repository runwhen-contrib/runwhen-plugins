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

## azure-aks-health: clusters, scenarios and how its answers were captured

The scenarios above also answer the commands of `azure-aks-health` and of `azure-base/discover`'s
cluster listing, for two AKS clusters: `aks-orders-prod` (`rg-orders-prod`, eastus: healthy) and
`aks-orders-dev` (`rg-orders-dev`, westeurope: broken). Their files are `aks-*.json` next to the
scenario's other files, and their calls sit in a marked block at the top of each `calls.yaml`
(`# --- azure-aks-health ... ---`; the stub drops `#` lines).

| scenario | target cluster | what it shows |
| --- | --- | --- |
| `healthy` | `aks-orders-prod` | Succeeded, Running, a supported minor with an upgrade offered, two autoscaled pools inside their bounds, diagnostic settings to Log Analytics, calm node metrics; a failed storage account write in the group (not the cluster's); the three per-group cluster listings of discover |
| `broken` | `aks-orders-dev` | provisioningState Failed, Resource Health Degraded, a minor AKS no longer offers, the `apps` pool at its autoscaler maximum and on an older version, the `batch` Spot pool Failed, no diagnostic settings, an API server open to everyone, failed cluster and node pool writes in the Activity Log (one id spelled in capitals, one for a cluster whose name starts like this one's), CPU and memory above 80%, a disk interval with no value |
| `stopped` | `aks-orders-dev` | power state Stopped, pools Stopped, every Azure Monitor interval without a value |
| `monitor-forbidden` | `aks-orders-dev` | the cluster reads, but diagnostic settings, the Activity Log and metrics are AuthorizationFailed |
| `forbidden`, `throttled`, `not-found` | `aks-orders-dev`, `aks-orders-dev`, `nope` | `az aks show` is AuthorizationFailed, TooManyRequests, ResourceNotFound |
| `group-forbidden` | (discover) | the cluster listings of all three groups succeed while `rg-orders-dev`'s vaults are refused: a group's two partitions are independent |

**These answers are the pinned az's own, not typed.** `capture/capture_aks.sh` runs the real az 2.92.0
inside the `azure@1` image with `--network none` (`capture/fake_az.py` replaces only sign-in and the HTTP
layer), against the ARM REST bodies Microsoft documents for each operation (`capture/aks_world.py`: the
reference page is each call's `source`). So the key spellings (`enableRbac`, `authorizedIpRanges`,
`typePropertiesType`, `dnsServiceIp`), the nulls, the added `resourceGroup`, the sorted keys, the
`aggregation` and `--select` handling and the `ERROR:` texts and exit codes (`not found` is exit 3) are what
the CLI prints. `capture/splice_aks.py` copies the files into the scenarios and the calls into each
`calls.yaml`; run it again after changing the world. What is *not* verified is the content of the bodies
themselves (the values are invented, shaped after the reference pages), nor what a real subscription
returns (`served.json` of a capture lists the REST requests az made, with the api-version, so a request
the real service would refuse shows there).

## azure-appservice-health: web apps, function apps and plans

The scenarios `appservice-healthy`, `-broken`, `-forbidden`, `-write-forbidden`, `-not-found`, `-throttled`, `-swap-busy` and
`-partial` are this skill's own (a fixture of its tasks names its scenario in the `record.sh` manifest:
`name=scenario@<group>/<plural>/<name>+input=value`). One world: `plan-orders-prod` (P1v3, 2 instances, autoscale 2 to 5),
`app-orders-api` and the function app `func-orders-jobs` on it, all fine, in `rg-orders-prod`; `plan-orders-dev` (S2, 3
instances, autoscale at its maximum of 3, CPU and memory above 80%), the stopped `app-orders-web` (HTTPS off, TLS 1.0, a
failed deployment, a 5xx spike, a stopped staging slot) and the function app `func-orders-ingest` (missing intervals) in
`rg-orders-dev`, Resource Health Degraded. The healthy scenario accepts every write and changes nothing (the stub is
stateless), so a recorded `applied` write finds the object as it was. `appservice-partial` refuses `config/web`, the slot
list and the Http4xx metric of `app-orders-web`; `appservice-swap-busy` has a swap under way on `app-orders-api`
(`slotSwapStatus`). The autoscale list is the subscription's: it holds the prod plan's setting, the dev plan's (kept in
`RG-Platform`, not in the plan's group) and one that targets a scale set.

**These answers are the pinned az's own where the CLI shapes them** (`capture/appservice.py`, run in the azure@1 image with
`--network none`): every `az rest` reply is produced by the real `az rest` against a local HTTP server that answers with the
ARM body (so the formatting is az's, and the `ERROR: Not Found(...)`, `Forbidden(...)` and `Too Many Requests(...)` texts
are az's stderr for a 404, 403 and 429); the discover listings (`az webapp list`, `az functionapp list`,
`az appservice plan list`) and the plan `az appservice plan update` prints go through the CLI's own list functions and
serialization with the ARM bodies deserialized into azure-mgmt-web models. `az monitor metrics list` is an AAZ command: its
replies are checked against the schema in `azure/cli/command_modules/monitor/aaz/latest/monitor/metrics/_list.py` and printed
with knack's JSON settings, but the datapoints are typed, not rendered. The write error texts follow the azure-core form the
CLI prints for SDK-backed commands. The ARM bodies follow https://learn.microsoft.com/rest/api/appservice (API 2024-04-01) and
have not been re-recorded against a real subscription; the Kudu deployment status codes (0 to 4) are Kudu's `DeployStatus`.
The block of listings in `healthy/calls.yaml` and `group-forbidden/calls.yaml` is written between marker comments, like the AKS one.
