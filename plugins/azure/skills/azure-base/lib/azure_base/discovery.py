"""discover: the subscription, its resource groups and their Key Vaults, as facts and as an
rw.discovery.v1 sync.

The az calls return plain facts; `build_sync` turns them into the recorded sync (open, item
batches, commit) that papi's resource-sync API takes (CONTRACTS.md, "Discovery sync"). The child
types are the ones the plugin's skills target (D51): the resource group, which addresses a vault
(subscription -> resourcegroup -> keyvault), and the Key Vault.

Every call that lists below the subscription passes `-g`: one partition per resource group, so a
group that cannot be read (forbidden, throttled, out of time) is not complete and nothing is swept
under it, while the other groups still are. A resource group's name is lower-cased in the identity
(ARM treats names case-insensitively, so `RG-Prod` and `rg-prod` are one group); the name as ARM
spells it is the item's display name."""

import time

from .az import az_json
from .common import bound
from .errors import RwError

PLATFORM = "azure"
SOURCE = "azure-base/discover"
BATCH = 500  # items per batch (rw.discovery.v1 itemsBatch)
CANONICAL_KEY = "subscriptionId"
GROUP_PLURAL = (
    "resourcegroups"  # the plural of the type in integration/types.yaml (a test keeps it)
)
LABEL_LIMIT = 50  # Azure allows 50 tags per resource
LABEL_TEXT = 255
LIST_BUDGET = 300  # seconds for the per-group listings; the rest of the task's 600s is the push


def target_subscription(resource):
    """The subscription id from the run's target; anything but an azure/subscription is refused."""
    resource = resource or {}
    if resource.get("type") != f"{PLATFORM}/subscription" or not resource.get("name"):
        raise RwError(
            "invalid_input",
            f"discover runs on an azure/subscription, got {resource.get('type')!r}",
        )
    return str(resource["name"])


def group_key(name):
    """The name a resource group has in the identity: ARM is case-insensitive, so lower case."""
    return name.lower()


def subscription_facts(ctx, subscription):
    """The `subscription` output, from `az account show` (the local profile: no role needed)."""
    account = az_json(ctx, ["account", "show", "--subscription", subscription])
    if not isinstance(account, dict) or not account.get("id"):
        raise RwError("internal", "az account show returned no subscription")
    if str(account["id"]).lower() != subscription.lower():
        raise RwError(
            "internal",
            f"az account show answered for another subscription than {subscription}",
        )
    return {
        "subscriptionId": str(account["id"]).lower(),
        "displayName": str(account.get("name") or account["id"]),
        "tenantId": str(account.get("tenantId") or ""),
        "state": str(account.get("state") or "Unknown"),
    }


def labels(tags):
    """Azure tags as labels: at most LABEL_LIMIT, string values, cut to LABEL_TEXT."""
    tags = tags if isinstance(tags, dict) else {}
    return {
        str(k)[:LABEL_TEXT]: str(v)[:LABEL_TEXT]
        for k, v in list(tags.items())[:LABEL_LIMIT]
        if isinstance(v, str)
    }


def read_groups(ctx, subscription):
    """The resource group facts, by name. A failure to list raises: without the groups there is
    nothing to list below."""
    rows = az_json(ctx, ["group", "list", "--subscription", subscription])
    if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
        raise RwError("internal", "az group list returned no list")
    groups = []
    for row in rows:
        if not row.get("name") or not row.get("id"):
            raise RwError("internal", "az group list returned a group without a name or id")
        props = row.get("properties") if isinstance(row.get("properties"), dict) else {}
        groups.append(
            {
                "name": str(row["name"]),
                "location": str(row.get("location") or ""),
                "id": str(row["id"]),
                "provisioningState": str(props.get("provisioningState") or ""),
                "tags": labels(row.get("tags")),
            }
        )
    return sorted(groups, key=lambda g: group_key(g["name"]))


def read_vaults(ctx, subscription, group):
    """The Key Vault facts of one resource group, by name. A failure to list raises.

    `--resource-type vault`: without it az lists vaults and managed HSMs and swallows an error
    from either (`except: pass` in the CLI), which would read as a group with no vaults."""
    rows = az_json(
        ctx,
        [
            "keyvault", "list", "-g", group["name"], "--resource-type", "vault",
            "--subscription", subscription,
        ],
    )  # fmt: skip
    if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
        raise RwError("internal", "az keyvault list returned no list")
    vaults = []
    for row in rows:
        if not row.get("name") or not row.get("id"):
            raise RwError("internal", "az keyvault list returned a vault without a name or id")
        vaults.append(
            {
                "name": str(row["name"]),
                "resourceGroup": group["name"],
                "location": str(row.get("location") or ""),
                "id": str(row["id"]),
                "tags": labels(row.get("tags")),
            }
        )
    return sorted(vaults, key=lambda v: v["name"].lower())


def group_listing(groups, vault_counts, limit):
    items, total, omitted = bound(groups, limit)
    return {
        "items": [
            {
                "name": g["name"],
                "location": g["location"],
                "id": g["id"],
                "keyvaults": vault_counts.get(group_key(g["name"])),
            }
            for g in items
        ],
        "total": total,
        "omitted": omitted,
    }


def vault_listing(vaults, limit):
    items, total, omitted = bound(vaults, limit)
    return {
        "items": [{k: v[k] for k in ("name", "resourceGroup", "location", "id")} for v in items],
        "total": total,
        "omitted": omitted,
    }


def partition(type_name, status, parent_path, count=None, reason=None):
    entry = {"type": type_name, "status": status, "parentPath": parent_path}
    if status == "complete" and count is not None:
        entry["count"] = count
    if reason:
        entry["reason"] = reason[:4096]
    return entry


def group_path(path, group):
    """The resource path of a group: where its Key Vaults sit."""
    return f"{path}/{GROUP_PLURAL}/{group_key(group['name'])}"


def summarize(counts, partitions, errors, duration_ms, subscription_id=None):
    """The rw.discovery.summary.v1 value, also the commit's `summary`. The script builds it a
    second time after the push, with the push's errors and the partitions the platform accepted."""
    totals = {"complete": 0, "failed": 0, "forbidden": 0, "excluded": 0}
    for p in partitions:
        totals[p["status"]] += 1
    summary = {"counts": dict(counts), "partitions": totals, "durationMs": duration_ms}
    if errors:
        summary["errors"] = list(errors)
    if subscription_id:
        summary["root"] = {"canonicalKey": {"name": CANONICAL_KEY, "value": subscription_id}}
    return summary


def build_sync(path, name, subscription, groups, vaults_by_group, partitions, summary):
    """The rw.discovery.v1 document for one full sync of the subscription at `path`.
    `vaults_by_group` maps group_key -> its vaults (a group whose listing failed is absent)."""
    root = {"type": "subscription", "name": name}
    items = [
        {
            "identity": {"platform": PLATFORM, "chain": [root]},
            "providerUid": subscription["subscriptionId"],
            "displayName": subscription["displayName"][:255] or name,
            "document": dict(subscription),
            "status": {"state": subscription["state"]},
        }
    ]
    for group in groups:
        link = {"type": "resourcegroup", "name": group_key(group["name"])}
        item = {
            "identity": {"platform": PLATFORM, "chain": [root, link]},
            "displayName": group["name"][:255],
            "document": {k: group[k] for k in ("name", "location", "id", "provisioningState")},
        }
        if len(group["id"]) <= 255:
            item["providerUid"] = group["id"]
        if group["tags"]:
            item["labels"] = group["tags"]
        items.append(item)
        for vault in vaults_by_group.get(group_key(group["name"]), []):
            vault_item = {
                "identity": {
                    "platform": PLATFORM,
                    "chain": [root, link, {"type": "keyvault", "name": vault["name"]}],
                },
                "displayName": vault["name"],
                "document": {k: vault[k] for k in ("name", "resourceGroup", "location", "id")},
            }
            if len(vault["id"]) <= 255:
                vault_item["providerUid"] = vault["id"]
            if vault["tags"]:
                vault_item["labels"] = vault["tags"]
            items.append(vault_item)
    batches = [
        {"seq": i // BATCH + 1, "items": items[i : i + BATCH]} for i in range(0, len(items), BATCH)
    ]
    return {
        "apiVersion": "rw.discovery.v1",
        "open": {"source": SOURCE, "scopePath": path, "mode": "full"},
        "batches": batches,
        "commit": {"partitions": partitions, "summary": summary},
    }


def discover(ctx, max_items, *, clock=time.monotonic, budget=LIST_BUDGET):
    """Run discovery on the subscription this task targets; returns the task's outputs and the
    sync document. A failure to read the account or list the resource groups raises. A group whose
    vaults cannot be listed is a `forbidden` or `failed` partition and a scoped error, and the run
    goes on with the other groups; once `budget` seconds are spent no further group is listed (each
    left is a `failed` partition with a `timeout` error)."""
    started = clock()
    resource = ctx.resource
    name = target_subscription(resource)
    path = resource["path"]
    subscription = subscription_facts(ctx, name)  # first: a failure here fails the task
    groups = read_groups(ctx, name)
    partitions = [partition("resourcegroup", "complete", path, len(groups))]
    vaults_by_group, counts_by_group = {}, {}
    out_of_time = []
    for group in groups:
        key, where = group_key(group["name"]), group_path(path, group)
        if clock() - started >= budget:
            out_of_time.append(group["name"])
            partitions.append(
                partition(
                    "keyvault", "failed", where, reason="the time budget for listing was used up"
                )
            )
            continue
        try:
            vaults = read_vaults(ctx, name, group)
        except RwError as exc:
            status = "forbidden" if exc.code == "permission_denied" else "failed"
            partitions.append(partition("keyvault", status, where, reason=exc.message))
            ctx.error(exc.code, exc.message, scope=f"keyvault:{key}")
            continue
        vaults_by_group[key], counts_by_group[key] = vaults, len(vaults)
        partitions.append(partition("keyvault", "complete", where, len(vaults)))
    if out_of_time:
        ctx.error(
            "timeout",
            f"the {budget}s budget for listing Key Vaults ran out; "
            f"{len(out_of_time)} resource groups were not listed",
            scope="keyvault",
        )
    vaults = sorted(
        (v for group_vaults in vaults_by_group.values() for v in group_vaults),
        key=lambda v: (v["name"].lower(), v["resourceGroup"].lower()),
    )
    summary = summarize(
        {"subscription": 1, "resourcegroup": len(groups), "keyvault": len(vaults)},
        partitions,
        ctx.errors,
        int((clock() - started) * 1000),
        subscription["subscriptionId"],
    )
    sync = build_sync(path, name, subscription, groups, vaults_by_group, partitions, summary)
    outputs = {
        "summary": summary,
        "subscription": subscription,
        "resourceGroups": group_listing(groups, counts_by_group, max_items),
        "keyvaults": vault_listing(vaults, max_items),
    }
    return outputs, sync
