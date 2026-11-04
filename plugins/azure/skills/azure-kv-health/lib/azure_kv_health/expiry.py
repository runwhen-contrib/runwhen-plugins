"""expiring-items: secrets, certificates and keys that are expired or expire soon.

Metadata only: the list calls return each item's name and attributes, never a secret value, a key
or a certificate; and only name, kind, expiry and enabled leave this module."""

import math
from datetime import UTC, datetime
from types import SimpleNamespace

from .az import az_json
from .common import listing
from .errors import RwError
from .vault import resolve

KINDS = (
    ("secret", "secrets", ["keyvault", "secret", "list"]),
    ("certificate", "certificates", ["keyvault", "certificate", "list"]),
    ("key", "keys", ["keyvault", "key", "list"]),
)


def parse_expiry(text):
    """An aware UTC datetime from Azure's ISO 8601 `expires`, or None when it has none/cannot be read."""
    if not isinstance(text, str) or not text:
        return None
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def classify(rows, kind, now, threshold_days):
    """What the rows of one list say about expiry.

    items: those that are expired or expire within the threshold. scanned: the rows looked at.
    no_expiry: rows with no expiry date. unreadable: names of rows whose expiry is set but cannot
    be read (they count in no_expiry too: nothing is known about them, and they are listed by name
    so they are not mistaken for items that never expire). managed_skipped: secrets Key Vault
    manages for a certificate (`managed: true`); the certificate row carries that expiry, and the
    secret is left out so the same date is not reported twice."""
    items, no_expiry, unreadable, managed, scanned = [], 0, [], 0, 0
    for row in rows:
        if kind == "secret" and row.get("managed") is True:
            managed += 1
            continue
        scanned += 1
        attrs = row.get("attributes") if isinstance(row.get("attributes"), dict) else {}
        name = str(row.get("name") or "")
        raw = attrs.get("expires")
        expires = parse_expiry(raw)
        if expires is None:
            no_expiry += 1
            if raw:
                unreadable.append({"kind": kind, "name": name})
            continue
        days = math.floor((expires - now).total_seconds() / 86400)
        if days <= threshold_days:
            items.append(
                {
                    "kind": kind,
                    "name": name,
                    "expires": raw,
                    "daysRemaining": days,
                    "expired": expires <= now,
                    "enabled": attrs.get("enabled") is not False,
                }
            )
    return SimpleNamespace(
        items=items,
        scanned=scanned,
        no_expiry=no_expiry,
        unreadable=unreadable,
        managed_skipped=managed,
    )


def read(ctx, threshold_days, max_items):
    if (
        isinstance(threshold_days, bool)
        or not isinstance(threshold_days, int)
        or not 0 <= threshold_days <= 3650
    ):
        raise RwError("invalid_input", "thresholdDays must be an integer from 0 to 3650")
    # the vault is looked up first: a name that is gone is not_found here, whereas the data-plane
    # lists would only fail to resolve its host name
    _, name, _, _ = resolve(ctx)
    now = ctx.now()
    found, unreadable, scanned, no_expiry, failed = [], [], {}, {}, []
    managed = 0
    for kind, plural, argv in KINDS:
        try:
            rows = az_json(ctx, [*argv, "--vault-name", name])
        except RwError as exc:
            failed.append((plural, exc))
            scanned[plural] = no_expiry[plural] = None
            continue
        if not isinstance(rows, list):
            raise RwError("internal", f"az keyvault {kind} list returned no list")
        result = classify(rows, kind, now, threshold_days)
        found += result.items
        unreadable += result.unreadable
        managed += result.managed_skipped
        scanned[plural], no_expiry[plural] = result.scanned, result.no_expiry
    if len(failed) == len(KINDS):  # nothing could be read: say why, not "no expiring items"
        raise failed[0][1]
    for plural, exc in failed:
        ctx.error(exc.code, exc.message, scope=plural)
    found.sort(key=lambda i: (i["daysRemaining"], i["kind"], i["name"]))
    return {
        "vault": name,
        "thresholdDays": threshold_days,
        "scanned": scanned,
        "noExpiry": no_expiry,
        "unreadableExpiry": listing(unreadable, max_items),
        "managedSecretsSkipped": managed,
        "expired": sum(1 for i in found if i["expired"]),
        "items": listing(found, max_items),
    }
