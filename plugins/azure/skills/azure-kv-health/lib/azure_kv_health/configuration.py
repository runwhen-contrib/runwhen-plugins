"""configuration: the vault's protection and network settings, as plain facts."""

from .common import resolve


def _count(value):
    return len(value) if isinstance(value, list) else 0


def read(ctx):
    _, name, _, doc = resolve(ctx)
    props = doc.get("properties") if isinstance(doc.get("properties"), dict) else {}
    acls = props.get("networkAcls") if isinstance(props.get("networkAcls"), dict) else {}
    sku = props.get("sku") if isinstance(props.get("sku"), dict) else {}
    retention = props.get("softDeleteRetentionInDays")
    return {
        "vault": name,
        "resourceGroup": doc.get("resourceGroup") or None,
        "location": doc.get("location") or None,
        "sku": sku.get("name") or None,
        "provisioningState": props.get("provisioningState") or None,
        # Soft delete cannot be switched off on a vault created since 2020, and newer API versions
        # may leave the property null: only an explicit false means it is off.
        "softDeleteEnabled": props.get("enableSoftDelete") is not False,
        "softDeleteRetentionDays": retention if isinstance(retention, int) else None,
        # Purge protection is off unless the property is true (null means never enabled).
        "purgeProtectionEnabled": props.get("enablePurgeProtection") is True,
        "rbacAuthorization": props.get("enableRbacAuthorization") is True,
        "publicNetworkAccess": props.get("publicNetworkAccess") or None,
        "networkAclDefaultAction": acls.get("defaultAction") or None,
        "ipRuleCount": _count(acls.get("ipRules")),
        "virtualNetworkRuleCount": _count(acls.get("virtualNetworkRules")),
        "privateEndpointConnectionCount": _count(props.get("privateEndpointConnections")),
        "accessPolicyCount": _count(props.get("accessPolicies")),
        "enabledForDeployment": props.get("enabledForDeployment") is True,
        "enabledForDiskEncryption": props.get("enabledForDiskEncryption") is True,
        "enabledForTemplateDeployment": props.get("enabledForTemplateDeployment") is True,
    }
