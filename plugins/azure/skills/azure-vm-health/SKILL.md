---
name: azure-vm-health
description: >-
  Health of one Azure virtual machine or VM scale set, from Azure's control plane: a VM's power and
  provisioning state, VM agent, extension status and boot diagnostics; Azure Resource Health; Azure
  Monitor CPU, available memory, disk and network over a window (every interval counted, no data never
  read as healthy); critical, error and warning Activity Log entries; for a scale set its model,
  instance states, failed instances, instances not on the latest model and autoscale settings. A
  separate write task, run-os-check, runs one of four fixed read-only scripts inside a running Linux VM
  (disk, memory, uptime, pending patches). Use when a VM is unreachable or slow, an extension or the
  agent is failing, a scale set rollout is stuck or an instance is unhealthy, or capacity is capped.
runwhen:
  role: health
  appliesTo:
    types: [azure/virtual-machine, azure/vm-scale-set]
  tags: [health, vm, vmss, metrics, compute]
---

## When to use

A VM does not answer, is slow, or an extension or the agent reports a failure; a scale set stops
rolling out, loses instances or sits at its maximum size. Each task answers one question about the
resource the run targets. Every task except `run-os-check` only reads, through the connection's own az
session (`azure/cli`), and none returns a secret, an OS profile or custom data.

The target is `azure/subscriptions/<id>/resourcegroups/<group>/virtual-machines/<name>` or
`.../vm-scale-sets/<name>` (both listed by `azure:azure-base/discover`). Every task looks the resource
up first with the resource group from the target: one that is gone is `not_found`. `commandTimeout`
(default 60s) bounds each az command. The metric and activity tasks take a `window` (1m to 30d) that
ends at the run's reference time.

| Task | VM | Scale set |
| --- | --- | --- |
| `instance-view`, `run-os-check` | yes | refused (`invalid_input`) |
| `scale-set-config`, `scale-set-instances`, `autoscale` | refused | yes |
| `resource-health`, `metrics`, `activity-errors` | yes | yes |

## What each task tells you

- `instance-view` returns `instanceView`: `powerState` and `running`, the model's `provisioningState`
  and the instance view's own, every status, the agent (`version`, `ready`), each extension with its
  status and `failed`, and `bootDiagnostics` (`enabled`, `consoleScreenshotAvailable`,
  `serialLogAvailable`).
- `resource-health` returns `health`: Azure's `availabilityState`, `healthy`, its summary and detail
  (cut to 1024 characters), the reason, times and the number of service-impacting events.
- `metrics` returns `metrics`: `cpu` (Percentage CPU), `availableMemory` (Available Memory Bytes),
  `diskRead`, `diskWrite`, `networkIn`, `networkOut`. Each has `hasData`, the intervals with and without
  a value, `min`, `max`, `last`, the newest 50 intervals; CPU counts intervals whose highest value is
  above `cpuThresholdPercent` (80) and memory those whose lowest is below `availableMemoryFloorMb` (256).
- `activity-errors` reads the resource group's log (up to `activityScanLimit`, 500, events; the caller
  and claims are not fetched) and returns `activity`: counts per level and the Critical, Error and
  Warning entries about this resource or something under it such as an extension (`target`), worst and
  newest first. Messages are redacted of identifiers and secrets. `scanCut: true`: the group had more
  events than were read, so the list may be incomplete and an empty one proves nothing.
- `scale-set-config` returns `scaleSet`: SKU and capacity, orchestration mode, zones, upgrade mode,
  automatic repairs and OS upgrade, the image's marketplace reference, health probe, extensions.
- `scale-set-instances` returns `instances`: counts (`provisioningFailed`, `modelOutdated`, `healthy`, `unhealthy`,
  `healthUnknown` (these three add up to `total`), `withFailedExtensions`, by provisioning and power state), the Azure instance view
  summary, and the instances, those with something to look at first.
- `autoscale` returns `autoscale`: the settings in the scale set's group that target it, per profile the
  minimum, maximum, default and whether the current capacity is at either, and each rule.
- `run-os-check` (write) takes `check`: `disk`, `memory`, `uptime` or `patches`, and returns `osCheck`
  with the fixed script's SHA-256 and typed facts from its output: filesystems against `diskUsedPercent`
  (85), memory used (total minus MemAvailable) against `memoryUsedPercent` (90), uptime and load against
  `uptimeDays` (30), or pending updates (`apt-get -s dist-upgrade`, so what `upgrade` keeps back counts) and
  the age of the package index.

## How to read the result

- `hasData: false` or `missing` above zero: Azure Monitor had no datapoint. That is no information,
  not a healthy VM. `availableMemory` is only published where Azure has a memory reading; its absence
  is normal for some images and sizes (Azure may answer 400 "Failed to find metric configuration": that
  is `unavailable: true`, `hasData: false`). `lastGrainMissing: true`: `last` is an older reading.
- `agent.reported: false` or `ready: null`: no agent report (normal for a deallocated VM); `ready: false`
  is Azure's "Not Ready". `powerState: deallocated` or `stopped` is a state, not a fault.
  `bootDiagnostics` flags are false for a stopped VM.
- A failed extension fails the VM's `provisioningState` (Failed) even when the VM runs; read the
  extension's `message`. A scale set's `healthUnknown` counts instances with no health extension or
  probe: unknown, not healthy. `modelOutdated` after a model change is normal until the upgrade
  policy has rolled it (`upgradeMode: Manual` never does). `listedMatchesCapacity: false`: the
  instances listed differ from the model's capacity (a scale operation in flight or a partial list).
- `scale-set-instances` with `orchestrationMode: Flexible` has no instance list: its instances are
  ordinary VMs of the group (error `invalid_input` scoped `instances`, the summary stands). Either
  half of the task failing is `partial` with the scope `instance-view` or `instances`.
- `autoscale.found: false`: no setting in that resource group targets the set (none configured, or its
  setting lives in another group). `capacityAtMaximum: true` means autoscale cannot add more.
- `run-os-check` is `skipped` with the reason unless the VM is Linux, running and Azure reports its agent
  `Ready` (no report is not ready); with `--dry-run` it reads the instance view and reports the script it would run
  (`executed: false`). `pending: null` is "could not tell" (no known package manager), never up to
  date; `patches` reads the package manager's local database and does not refresh it, so it is as fresh
  as the index. `indexAgeSeconds` comes from apt's `update-success-stamp`, which exists only where
  apt's periodic job runs: null when the file is absent, and always null for dnf and yum, whose
  cache-only query also appends to its own log (`/var/log/dnf.log`). `cutInGuest` on `disk`: the
  guest keeps the 40 fullest local file systems.
- Lists are cut at `maxItems` (200; `omitted` says how many), nested lists at 50, text at 1024.
- The run fails with a code, never an empty result, from az's own `ERROR:` lines: `permission_denied`
  (the message names the action: `Microsoft.Compute/virtualMachines/runCommand/action` for the guest
  task), `not_found`, `rate_limited` (429), `unreachable`, `timeout`, `dependency` (no az, or a provider
  such as Microsoft.ResourceHealth not registered), else `internal`.

## What to try next

- Extension failed or agent Not Ready: fix it in Azure; `run-os-check` cannot run until the agent is ready.
- `resource-health` Unavailable or Degraded: Azure's side, check back or open a support case; Available
  with CPU or memory crossing: `run-os-check` with `memory` or `disk`, or the workload on the VM.
- Scale set instance failed or unhealthy: `activity-errors` on the scale set for the failed operation;
  `autoscale` at maximum with high CPU: raise the maximum (change it in Azure; this skill only reads).
- `azure:azure-base/discover` lists what else is in the group; a VM that is an AKS node belongs to
  `azure-aks-health`, and pod-level questions to `kubernetes:k8s-workload-health`.

## run-os-check

It is remote command execution on a customer VM, so it is a write task (`readOnly: false`, `idempotent`,
not `destructive`, `dryRun`, with `effects`). The script is a constant of the skill
(`lib/azure_vm_health/guest_scripts.py`): chosen by name, never built from input, run as root by the VM
agent, reads `/proc`, `df` or the package manager's local database, writes nothing and refreshes no
index. Each blocking command in it runs under `timeout 30`; `osCheckTimeout` (a duration, 10s to 240s)
only bounds how long the client waits, and a script already running in the guest is not cancelled.
The first run command on a VM installs the RunCommandLinux handler, which needs outbound access. It
needs `Microsoft.Compute/virtualMachines/runCommand/action`, which Reader does not include.

## Tests

The plugin is stub-recorded: the fixtures were recorded against `tests/stub/bin/az`, whose canned output
was rendered by the real az CLI against a synthetic Azure Resource Manager (`tests/stub/README.md`), not
against a real subscription. Re-record against a real account when a credential exists.
