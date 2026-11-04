"""discover: the subscription, its resource groups and what the plugin's skills target in them, as facts
and as an rw.discovery.v1 sync.

The az calls return plain facts; `build_sync` turns them into the recorded sync (open, item
batches, commit) that papi's resource-sync API takes (CONTRACTS.md, "Discovery sync"). The child
types are the ones the plugin's skills target (D51): the resource group, which addresses the
resources below it (subscription -> resourcegroup -> keyvault | aks-cluster | app-service-plan | web-app),
the Key Vault, the AKS cluster, the App Service plan and the web app (a function app is a web app of
kind functionapp). CHILD_TYPES is the table of them: one row per type a skill targets.

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
LISTED = {
    "keyvault": "Key Vaults",
    "aks-cluster": "AKS clusters",
    "app-service-plan": "App Service plans",
    "web-app": "web apps",
}  # the child types, in words
BASE_FACTS = (
    "name",
    "resourceGroup",
    "location",
    "id",
)  # what every child's listing and document hold
# the facts a type keeps beyond BASE_FACTS (in its listing and in its sync item's document)
EXTRA_FACTS = {
    "app-service-plan": ("kind", "skuName", "skuTier"),
    "web-app": ("kind", "state"),
}


def facts(type_name):
    return (*BASE_FACTS, *EXTRA_FACTS.get(type_name, ()))


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


def read_aks_clusters(ctx, subscription, group):
    """The AKS cluster facts of one resource group, by name. A failure to list raises (there is
    nothing like Key Vault's `--resource-type` to guard: `az aks list -g` has one source)."""
    rows = az_json(ctx, ["aks", "list", "-g", group["name"], "--subscription", subscription])
    if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
        raise RwError("internal", "az aks list returned no list")
    clusters = []
    for row in rows:
        if not row.get("name") or not row.get("id"):
            raise RwError("internal", "az aks list returned a cluster without a name or id")
        clusters.append(
            {
                "name": str(row["name"]),
                "resourceGroup": group["name"],
                "location": str(row.get("location") or ""),
                "id": str(row["id"]),
                "tags": labels(row.get("tags")),
            }
        )
    return sorted(clusters, key=lambda c: c["name"].lower())


def listed(ctx, argv):
    """The rows of one az listing; a failure to list raises, and so does a reply that is not a list of
    named, identified objects."""
    rows = az_json(ctx, argv)
    if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
        raise RwError("internal", f"az {' '.join(argv[:2])} returned no list")
    for row in rows:
        if not row.get("name") or not row.get("id"):
            raise RwError(
                "internal", f"az {' '.join(argv[:2])} returned an object without a name or id"
            )
    return rows


def base_row(row, group):
    return {
        "name": str(row["name"]),
        "resourceGroup": group["name"],
        "location": str(row.get("location") or ""),
        "id": str(row["id"]),
        "tags": labels(row.get("tags")),
    }


def read_plans(ctx, subscription, group):
    """The App Service plans of one resource group, by name. A failure to list raises."""
    rows = listed(
        ctx, ["appservice", "plan", "list", "-g", group["name"], "--subscription", subscription]
    )
    plans = []
    for row in rows:
        sku = row.get("sku") if isinstance(row.get("sku"), dict) else {}
        plans.append(
            {
                **base_row(row, group),
                "kind": str(row.get("kind") or ""),
                "skuName": str(sku.get("name") or ""),
                "skuTier": str(sku.get("tier") or ""),
            }
        )
    return sorted(plans, key=lambda p: p["name"].lower())


def read_web_apps(ctx, subscription, group):
    """The web apps of one resource group, function apps included (a function app is a web app of kind
    functionapp): `az webapp list` leaves the function apps out and `az functionapp list` lists only
    them, so both are read, and both must succeed for the listing to be complete."""
    rows = listed(ctx, ["webapp", "list", "-g", group["name"], "--subscription", subscription])
    rows += listed(
        ctx, ["functionapp", "list", "-g", group["name"], "--subscription", subscription]
    )
    apps, seen = [], set()
    for row in rows:
        if str(row["id"]).lower() in seen:
            continue
        seen.add(str(row["id"]).lower())
        apps.append(
            {
                **base_row(row, group),
                "kind": str(row.get("kind") or ""),
                "state": str(row.get("state") or ""),
            }
        )
    return sorted(apps, key=lambda a: a["name"].lower())


# One row per child type of the resource group a skill targets: (type name in types.yaml, reader).
# A reader takes (ctx, subscription, group) and returns [{name, resourceGroup, location, id, tags}].
CHILD_TYPES = (
    ("keyvault", read_vaults),
    ("aks-cluster", read_aks_clusters),
    ("app-service-plan", read_plans),
    ("web-app", read_web_apps),
)


def group_listing(groups, vault_counts, limit, cluster_counts, plan_counts=None, app_counts=None):
    items, total, omitted = bound(groups, limit)
    return {
        "items": [
            {
                "name": g["name"],
                "location": g["location"],
                "id": g["id"],
                "keyvaults": vault_counts.get(group_key(g["name"])),
                "aksClusters": cluster_counts.get(group_key(g["name"])),
                "appServicePlans": (plan_counts or {}).get(group_key(g["name"])),
                "webApps": (app_counts or {}).get(group_key(g["name"])),
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


def cluster_listing(clusters, limit):
    """Same shape as vault_listing: name, resource group, location and id of each AKS cluster."""
    return vault_listing(clusters, limit)


def child_listing(type_name, rows, limit):
    """name, resource group, location, id and the type's own facts of each plan or web app."""
    items, total, omitted = bound(rows, limit)
    return {
        "items": [{k: v[k] for k in facts(type_name)} for v in items],
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
    """The resource path of a group: where its Key Vaults and AKS clusters sit."""
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


def build_sync(path, name, subscription, groups, children, partitions, summary):
    """The rw.discovery.v1 document for one full sync of the subscription at `path`.
    `children` maps a child type name (CHILD_TYPES) to {group_key: its resources}; a group whose
    listing failed is absent from it."""
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
        for type_name, _ in CHILD_TYPES:
            for child in children.get(type_name, {}).get(group_key(group["name"]), []):
                child_item = {
                    "identity": {
                        "platform": PLATFORM,
                        "chain": [root, link, {"type": type_name, "name": child["name"]}],
                    },
                    "displayName": child["name"],
                    "document": {k: child[k] for k in facts(type_name)},
                }
                if len(child["id"]) <= 255:
                    child_item["providerUid"] = child["id"]
                if child["tags"]:
                    child_item["labels"] = child["tags"]
                items.append(child_item)
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
    resources of one type cannot be listed is a `forbidden` or `failed` partition of that type and a
    scoped error, and the run goes on with the group's other types and the other groups; once
    `budget` seconds are spent no further group is listed (each type left is a `failed` partition
    with a `timeout` error)."""
    started = clock()
    resource = ctx.resource
    name = target_subscription(resource)
    path = resource["path"]
    subscription = subscription_facts(ctx, name)  # first: a failure here fails the task
    groups = read_groups(ctx, name)
    partitions = [partition("resourcegroup", "complete", path, len(groups))]
    children = {type_name: {} for type_name, _ in CHILD_TYPES}  # type -> {group key: resources}
    out_of_time = []
    for group in groups:
        key, where = group_key(group["name"]), group_path(path, group)
        if clock() - started >= budget:
            out_of_time.append(group["name"])
            for type_name, _ in CHILD_TYPES:
                partitions.append(
                    partition(
                        type_name, "failed", where, reason="the time budget for listing was used up"
                    )
                )
            continue
        for type_name, reader in CHILD_TYPES:
            try:
                found = reader(ctx, name, group)
            except RwError as exc:
                status = "forbidden" if exc.code == "permission_denied" else "failed"
                partitions.append(partition(type_name, status, where, reason=exc.message))
                ctx.error(exc.code, exc.message, scope=f"{type_name}:{key}")
                continue
            children[type_name][key] = found
            partitions.append(partition(type_name, "complete", where, len(found)))
    if out_of_time:
        for type_name, _ in CHILD_TYPES:
            ctx.error(
                "timeout",
                f"the {budget}s budget for listing {LISTED[type_name]} ran out; "
                f"{len(out_of_time)} resource groups were not listed",
                scope=type_name,
            )

    def flat(type_name):
        return sorted(
            (c for group_children in children[type_name].values() for c in group_children),
            key=lambda c: (c["name"].lower(), c["resourceGroup"].lower()),
        )

    vaults, clusters = flat("keyvault"), flat("aks-cluster")
    plans, apps = flat("app-service-plan"), flat("web-app")
    counts_by_group = {k: len(v) for k, v in children["keyvault"].items()}
    clusters_by_group = {k: len(v) for k, v in children["aks-cluster"].items()}
    plans_by_group = {k: len(v) for k, v in children["app-service-plan"].items()}
    apps_by_group = {k: len(v) for k, v in children["web-app"].items()}
    summary = summarize(
        {
            "subscription": 1,
            "resourcegroup": len(groups),
            "keyvault": len(vaults),
            "aks-cluster": len(clusters),
            "app-service-plan": len(plans),
            "web-app": len(apps),
        },
        partitions,
        ctx.errors,
        int((clock() - started) * 1000),
        subscription["subscriptionId"],
    )
    sync = build_sync(path, name, subscription, groups, children, partitions, summary)
    outputs = {
        "summary": summary,
        "subscription": subscription,
        "resourceGroups": group_listing(
            groups, counts_by_group, max_items, clusters_by_group, plans_by_group, apps_by_group
        ),
        "keyvaults": vault_listing(vaults, max_items),
        "aksClusters": cluster_listing(clusters, max_items),
        "appServicePlans": child_listing("app-service-plan", plans, max_items),
        "webApps": child_listing("web-app", apps, max_items),
    }
    return outputs, sync
