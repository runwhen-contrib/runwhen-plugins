"""discover: the subscription and its Key Vaults, as facts and as an rw.discovery.v1 sync.

The az calls return plain facts; `build_sync` turns them into the recorded sync (open, item
batches, commit) that papi's resource-sync API takes (CONTRACTS.md, "Discovery sync"). The
child types are the ones the plugin's skills target (D51): today the Key Vault."""

import time

from .az import az_json
from .errors import RwError

PLATFORM = "azure"
SOURCE = "azure-base/discover"
BATCH = 500  # items per batch (rw.discovery.v1 itemsBatch)
CANONICAL_KEY = "subscriptionId"
LABEL_LIMIT = 50  # Azure allows 50 tags per resource
LABEL_TEXT = 255


def target_subscription(resource):
    """The subscription id from the run's target; anything but an azure/subscription is refused."""
    resource = resource or {}
    if resource.get("type") != f"{PLATFORM}/subscription" or not resource.get("name"):
        raise RwError(
            "invalid_input",
            f"discover runs on an azure/subscription, got {resource.get('type')!r}",
        )
    return str(resource["name"])


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


def read_vaults(ctx, subscription):
    """The Key Vault facts, by name. A failure to list raises."""
    rows = az_json(ctx, ["keyvault", "list", "--subscription", subscription])
    if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
        raise RwError("internal", "az keyvault list returned no list")
    vaults = []
    for row in rows:
        if not row.get("name") or not row.get("id"):
            raise RwError("internal", "az keyvault list returned a vault without a name or id")
        tags = row.get("tags") if isinstance(row.get("tags"), dict) else {}
        vaults.append(
            {
                "name": str(row["name"]),
                "resourceGroup": str(row.get("resourceGroup") or ""),
                "location": str(row.get("location") or ""),
                "id": str(row["id"]),
                "tags": {
                    str(k)[:LABEL_TEXT]: str(v)[:LABEL_TEXT]
                    for k, v in list(tags.items())[:LABEL_LIMIT]
                    if isinstance(v, str)
                },
            }
        )
    return sorted(vaults, key=lambda v: v["name"].lower())


def bound(items, limit):
    """(first `limit` items, total, omitted)."""
    return items[:limit], len(items), max(len(items) - limit, 0)


def vault_listing(vaults, limit):
    items, total, omitted = bound(vaults, limit)
    return {
        "items": [{k: v[k] for k in ("name", "resourceGroup", "location", "id")} for v in items],
        "total": total,
        "omitted": omitted,
    }


def vault_partitions(path, count):
    """The one partition a full sync of a subscription sweeps: its Key Vaults."""
    return [{"type": "keyvault", "parentPath": path, "status": "complete", "count": count}]


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


def build_sync(path, name, subscription, vaults, summary):
    """The rw.discovery.v1 document for one full sync of the subscription at `path`."""
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
    for vault in vaults:
        item = {
            "identity": {
                "platform": PLATFORM,
                "chain": [root, {"type": "keyvault", "name": vault["name"]}],
            },
            "displayName": vault["name"],
            "document": {k: vault[k] for k in ("name", "resourceGroup", "location", "id")},
        }
        if len(vault["id"]) <= 255:
            item["providerUid"] = vault["id"]
        if vault["tags"]:
            item["labels"] = vault["tags"]
        items.append(item)
    batches = [
        {"seq": i // BATCH + 1, "items": items[i : i + BATCH]} for i in range(0, len(items), BATCH)
    ]
    return {
        "apiVersion": "rw.discovery.v1",
        "open": {"source": SOURCE, "scopePath": path, "mode": "full"},
        "batches": batches,
        "commit": {"partitions": vault_partitions(path, len(vaults)), "summary": summary},
    }


def discover(ctx, max_items):
    """Run discovery on the subscription this task targets; returns the task's outputs and the
    sync document. A failure to read the account or list the vaults raises."""
    started = time.monotonic()
    resource = ctx.resource
    name = target_subscription(resource)
    path = resource["path"]
    subscription = subscription_facts(ctx, name)  # first: a failure here fails the task
    vaults = read_vaults(ctx, name)
    summary = summarize(
        {"subscription": 1, "keyvault": len(vaults)},
        vault_partitions(path, len(vaults)),
        [],
        int((time.monotonic() - started) * 1000),
        subscription["subscriptionId"],
    )
    sync = build_sync(path, name, subscription, vaults, summary)
    outputs = {
        "summary": summary,
        "subscription": subscription,
        "keyvaults": vault_listing(vaults, max_items),
    }
    return outputs, sync
