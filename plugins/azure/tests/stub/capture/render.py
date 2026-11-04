"""Render the control-plane JSON the stub serves with the real CLI's own code.

The stub's canned files must look like what the pinned az prints, formatting included: a hand-typed
file gets small things wrong (a model's unset fields are printed as null; a nested block is flattened;
az adds `resourceGroup`; keys are sorted). So the shapes of the commands the skills depend on most
(`az keyvault show`, `az keyvault list -g`, `az group list`) are produced here: the ARM REST body
(what Microsoft documents) goes into the azure-mgmt model az uses, then through az's own
`todict` (with the invoker's additional-properties flattening) and global `resourceGroup` transform, and out through knack's JSON formatter settings
(`indent=2, sort_keys=True`). `az cloud show` is simply run.

    docker run --rm --network none -v "$PWD/capture:/capture:ro" -v "$OUT:/out" \
        --entrypoint /opt/az/bin/python rwp-runtime-azure:dev -I /capture/render.py /out

Writes keyvault-show-<name>.json, keyvault-list-<group>.json, group-list.json and cloud-show.json
into /out. Needs no network and no login. REST references:
  https://learn.microsoft.com/rest/api/keyvault/keyvault/vaults/get
  https://learn.microsoft.com/rest/api/keyvault/keyvault/vaults/list-by-resource-group
  https://learn.microsoft.com/rest/api/resources/resource-groups/list
"""

import json
import os
import subprocess
import sys

from azure.cli.core.commands import AzCliCommandInvoker
from azure.cli.core.commands.transform import _add_resource_group
from azure.cli.core.util import todict
from azure.mgmt.keyvault import models as kv
from azure.mgmt.resource.resources.v2024_11_01 import models as rm

SUB = "4b1d1f7e-6f0a-4c5e-9a43-0c8a2d9e5b71"
TENANT = "c0ffee00-1234-4abc-8def-0123456789ab"
GROUPS = {  # name -> location, tags
    "rg-orders-dev": ("westeurope", {"env": "dev", "team": "orders"}),
    "rg-orders-prod": ("eastus", {"env": "prod", "team": "orders"}),
    "RG-Platform": ("northeurope", {"team": "platform"}),
}


def group_id(group):
    return f"/subscriptions/{SUB}/resourceGroups/{group}"


def vault_rest(name, group, location, tags, props):
    """The ARM body of a vault (Vaults - Get)."""
    base = {
        "sku": {"family": "A", "name": "standard"},
        "tenantId": TENANT,
        "accessPolicies": [],
        "enabledForDeployment": False,
        "enabledForDiskEncryption": False,
        "enabledForTemplateDeployment": False,
        "enableSoftDelete": True,
        "enableRbacAuthorization": True,
        "vaultUri": f"https://{name}.vault.azure.net/",
        "provisioningState": "Succeeded",
    }
    base.update(props)
    return {
        "id": f"{group_id(group)}/providers/Microsoft.KeyVault/vaults/{name}",
        "name": name,
        "type": "Microsoft.KeyVault/vaults",
        "location": location,
        "tags": tags,
        "properties": base,
    }


VAULTS = {
    "kv-orders-prod": vault_rest(
        "kv-orders-prod",
        "rg-orders-prod",
        "eastus",
        {"env": "prod", "team": "orders"},
        {
            "softDeleteRetentionInDays": 90,
            "enablePurgeProtection": True,
            "publicNetworkAccess": "Disabled",
            "networkAcls": {
                "bypass": "AzureServices",
                "defaultAction": "Deny",
                "ipRules": [{"value": "203.0.113.0/24"}],
                "virtualNetworkRules": [],
            },
            "privateEndpointConnections": [
                {
                    "id": f"{group_id('rg-orders-prod')}/providers/Microsoft.KeyVault/vaults/"
                    "kv-orders-prod/privateEndpointConnections/pe-orders",
                    "properties": {
                        "privateEndpoint": {
                            "id": f"{group_id('rg-orders-prod')}/providers/Microsoft.Network/"
                            "privateEndpoints/pe-orders"
                        },
                        "privateLinkServiceConnectionState": {
                            "status": "Approved",
                            "description": "Auto-Approved",
                            "actionsRequired": "None",
                        },
                        "provisioningState": "Succeeded",
                    },
                }
            ],
        },
    ),
    "kv-orders-dev": vault_rest(
        "kv-orders-dev",
        "rg-orders-dev",
        "westeurope",
        {"env": "dev", "team": "orders"},
        {"softDeleteRetentionInDays": 7, "publicNetworkAccess": "Enabled"},
    ),
}


def cli_json(value):
    """What `az ... --output json` prints for a model or a list of models."""
    result = todict(value, AzCliCommandInvoker.remove_additional_prop_layer)
    _add_resource_group(result)
    return json.dumps(result, indent=2, sort_keys=True, separators=(",", ": ")) + "\n"


def write(out, name, text):
    with open(os.path.join(out, name), "w", encoding="utf-8") as handle:
        handle.write(text)


def main(out):
    for name, body in VAULTS.items():
        write(out, f"keyvault-show-{name}.json", cli_json(kv.Vault(body)))
    by_group = {}
    for body in VAULTS.values():
        group = body["id"].split("/")[4]
        by_group.setdefault(group, []).append(kv.Vault(body))
    for group in GROUPS:
        write(out, f"keyvault-list-{group.lower()}.json", cli_json(by_group.get(group, [])))
    groups = [
        rm.ResourceGroup.deserialize(
            {
                "id": group_id(name),
                "name": name,
                "type": "Microsoft.Resources/resourceGroups",
                "location": location,
                "tags": tags,
                "properties": {"provisioningState": "Succeeded"},
            }
        )
        for name, (location, tags) in GROUPS.items()
    ]
    write(out, "group-list.json", cli_json(groups))
    done = subprocess.run(
        ["az", "cloud", "show", "--output", "json"], capture_output=True, text=True, check=True
    )
    write(out, "cloud-show.json", done.stdout)


if __name__ == "__main__":
    main(sys.argv[1])
