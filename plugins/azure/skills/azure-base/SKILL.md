---
name: azure-base
description: >-
  The Azure integration's own skill. discover reads a subscription (id, name, tenant, state) and
  lists its Key Vaults with resource group and location, and reports the subscription id as the
  root's canonical key. Use to see what is in a subscription, or to check that an Azure connection
  reaches it before running the other azure skills.
runwhen:
  role: [discovery]
  appliesTo:
    types: [azure/subscription]
  tags: [discovery, inventory]
---

## When to use

Reach for `discover` to see which Key Vaults a subscription holds and where, or to prove that a new
connection works: it needs only Reader on the subscription. The platform also runs it on its own,
every 15 minutes, to keep the inventory (the `azure/keyvault` resources the other azure skills
target) current.

It only reads, through the connection's own az session (`azure/cli`: `AZURE_CONFIG_DIR` and
`AZURE_SUBSCRIPTION_ID`). The `commandTimeout` setting (default 60s) bounds each az command.
`discover` has a 600s task timeout, which covers the az calls and the push to the platform: the push
is clamped to what is left of the budget (less a 10s margin), and a request that would start with
nothing left fails with `timeout`.

## What each task tells you

- `discover` returns `subscription` (id, display name, tenant, state), `keyvaults` (name, resource
  group, location, id), `summary` (counts and per-step errors) and a `sync` artifact: the whole
  `rw.discovery.v1` sync the platform ingests, with each vault's tags as labels.
  `summary.root.canonicalKey` is the subscription id. `keyvaults` is `{items, total, omitted}`:
  `total` is every vault found, `items` the first `maxItems` (default 200) of them and `omitted` how
  many were cut. Only that list is cut: the `sync` artifact always holds every vault. When the run has
  the per-run `runwhen/sync` credential (`RW_SYNC_CREDENTIAL`) `discover` also pushes the sync to the
  platform: open, batches, commit. A push that fails fails the task; a commit the platform holds, or
  items it rejects, make the run `partial` with an `internal` error scoped `sync`. Without the
  credential (a local `rwtask run`) the artifact is all it produces.

## How to read the result

- `keyvaults.items` empty with `total: 0`: the identity can list vaults and the subscription has none
  (or none it may see: Azure lists only the resources the identity has `Microsoft.KeyVault/vaults/read`
  on, so a role assigned at one resource group lists only that group's vaults). A listing the identity
  may not make at all is an error, never an empty list.
- `subscription.state` other than `Enabled` (`Warned`, `PastDue`, `Disabled`): Azure limits what the
  subscription's resources answer; the other azure skills may fail with `permission_denied`.
- Soft-deleted vaults are not listed. A vault is listed under the name it was created with; the
  sync keeps it as is.
- The run fails with a code, not an empty result, classified from az's own words (the `ERROR:` lines
  of stderr; the exit code decides first, and az's warnings on a successful command are ignored):
  - `permission_denied`: `AuthorizationFailed`, `Forbidden`, an AADSTS code, "Please run 'az login'".
  - `not_found`: `Subscription '...' not found` (the connection's subscription is gone or the identity
    cannot see it), `ResourceNotFound`.
  - `rate_limited`: `TooManyRequests`, 429, throttled. `unreachable`: cannot connect, DNS, TLS.
  - `timeout`: the command timeout. `dependency`: no az in the runtime, or a resource provider that is
    not registered. Anything else is `internal`.

## What to try next

- Read a vault's health with `azure:azure-kv-health/configuration`, `azure:azure-kv-health/availability`
  and the other tasks of that skill, on the vault `discover` listed.
- `permission_denied`: assign Reader on the subscription (or `Microsoft.KeyVault/vaults/read` at the
  scope that holds the vaults) to the connection's service principal or managed identity, then run
  `discover` again. The permission check for the azure integration comes after this pilot.
- The vault runs behind an AKS cluster: pod-level questions belong to `kubernetes:k8s-workload-health`.

## Tests

The plugin is stub-recorded: its fixtures were recorded against `tests/stub/bin/az`, a stand-in for
the az CLI that serves canned responses shaped after the CLI reference (`tests/stub/README.md`), not
against a real subscription. Re-record against a real account when a credential exists.
