---
name: azure-base
description: >-
  The Azure integration's own skill. discover reads a subscription (id, name, tenant, state), lists
  its resource groups and, group by group, their Key Vaults and AKS clusters with location, and
  reports the subscription id as the root's canonical key. Use to see what is in a subscription, or to check that an Azure connection
  reaches it before running the other azure skills.
runwhen:
  role: [discovery]
  appliesTo:
    types: [azure/subscription]
  tags: [discovery, inventory]
---

## When to use

Reach for `discover` to see which resource groups, Key Vaults and AKS clusters a subscription holds and where, or to
prove that a new connection works: it needs only Reader on the subscription. The platform also runs it on its own,
every 15 minutes, to keep the inventory (the `azure/keyvault` and `azure/aks-cluster` resources the other azure
skills target) current.

It only reads, through the connection's own az session (`azure/cli`: `AZURE_CONFIG_DIR`); the
subscription comes from the resource it runs on and goes to every az call as `--subscription`. The
`commandTimeout` setting (default 60s) bounds each az command. `discover` has a 600s task timeout,
which covers the az calls and the push to the platform: the listings stop after 300s, checked
before each listing (the types not yet listed, in the group where it ran out and in every later
group, become `failed` partitions and the run reports one `timeout` error scoped to each type:
`keyvault`, `aks-cluster`), the push is clamped to what is left of the budget (less a 10s margin), and a request
that would start with nothing left fails with `timeout`.

The plugin's resources are addressed `azure/subscriptions/<id>/resourcegroups/<group>/keyvaults/<name>` and
`.../aks-clusters/<name>`. The group is a type of its own: every vault and cluster call passes `-g`, which
scopes the call (and the role assignment it needs) to the group. Group names are lower case in the address, as ARM compares them
without case; the display name keeps ARM's spelling.

## What each task tells you

- `discover` returns `subscription` (id, display name, tenant, state), `resourceGroups` (name,
  location, id and how many vaults and AKS clusters the group holds, or null when they could not be
  listed), `keyvaults` and `aksClusters` (name, resource group, location, id), `summary` (counts and
  per-step errors) and a `sync` artifact: the whole `rw.discovery.v1` sync the platform ingests, with
  each group's, vault's and cluster's tags as labels.
  `summary.root.canonicalKey` is the subscription id. `keyvaults` is `{items, total, omitted}`:
  `total` is every vault found, `items` the first `maxItems` (default 200) of them and `omitted` how
  many were cut (`aksClusters` is the same). Only those lists are cut: the `sync` artifact always
  holds every vault and cluster. When the run has
  the per-run `runwhen/sync` credential (`RW_SYNC_CREDENTIAL`) `discover` also pushes the sync to the
  platform: open, batches, commit. A push that fails fails the task; a commit the platform holds, or
  items it rejects, make the run `partial` with an `internal` error scoped `sync`. Without the
  credential (a local `rwtask run`) the artifact is all it produces.

## How to read the result

- `keyvaults.items` (or `aksClusters.items`) empty with `total: 0`: the groups the identity can list hold none. Azure
  lists only the groups the identity may read (`Microsoft.Resources/subscriptions/resourceGroups/read`),
  so a role assigned at one resource group lists only that group. A group listing the identity may not
  make at all is an error, never an empty list.
- One group whose vaults cannot be listed (`permission_denied`, `rate_limited`...): the run is
  `partial`, the error is scoped `keyvault:<group>`, that group's partition is `forbidden` or `failed`
  (nothing under it is swept from the inventory) and its `resourceGroups` entry has `keyvaults: null`.
  The same holds for its AKS clusters (`aks-cluster:<group>`, `aksClusters: null`) and the two are
  independent: a group can have its vaults listed and its clusters refused. The other groups are
  complete.
- `providerNotRegistered` on a group lists the types (`keyvault`, `aks-cluster`) whose resource
  provider the subscription has not registered. Azure refuses the listing (`MissingSubscriptionRegistration`),
  but no resource of that type can exist, so the partition is `complete` with count 0 (it may be swept) and the
  run is not partial.
- `subscription.state` other than `Enabled` (`Warned`, `PastDue`, `Disabled`): Azure limits what the
  subscription's resources answer; the other azure skills may fail with `permission_denied`.
- Soft-deleted vaults are not listed. A vault is listed under the name it was created with; the
  sync keeps it as is.
- The run fails with a code, not an empty result, classified from az's own words (the `ERROR:` lines
  of stderr; the exit code decides first, and az's warnings on a successful command are ignored):
  - `permission_denied`: `AuthorizationFailed`, `Forbidden`, an AADSTS code, "Please run 'az login'".
  - `not_found`: `Subscription '...' not found` (the connection's subscription is gone or the identity
    cannot see it), `ResourceNotFound`, `ResourceGroupNotFound`.
  - `rate_limited`: `TooManyRequests`, 429, throttled. `unreachable`: cannot connect, DNS, TLS.
  - `timeout`: the command timeout. `dependency`: no az in the runtime, or a resource provider that is
    not registered. Anything else is `internal`.

## What to try next

- Read a vault's health with `azure:azure-kv-health/configuration`, `azure:azure-kv-health/availability`
  and the other tasks of that skill, on the vault `discover` listed; a cluster's with
  `azure:azure-aks-health/cluster-status`, `azure:azure-aks-health/node-pools` and the others, on the
  cluster `discover` listed.
- `permission_denied`: assign Reader on the subscription (or `Microsoft.Resources/subscriptions/resourceGroups/read`
  and `Microsoft.KeyVault/vaults/read` and `Microsoft.ContainerService/managedClusters/read` at the scope
  that holds the vaults and clusters) to the connection's
  service principal or managed identity, then run `discover` again. A connection whose identity has
  no role assignment at all is refused when it is made (`az account set`), as `permission_denied`. The permission check for the azure integration comes after this pilot.
- The vault runs behind an AKS cluster: pod-level questions belong to `kubernetes:k8s-workload-health`.

## Tests

The plugin is stub-recorded: its fixtures were recorded against `tests/stub/bin/az`, a stand-in for
the az CLI that serves canned responses shaped after the CLI reference (`tests/stub/README.md`), not
against a real subscription. Re-record against a real account when a credential exists.
