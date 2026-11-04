"""expiring-items: secrets, certificates and keys that are expired or expire soon.

Metadata only: the list calls return each item's name and attributes, never a secret value, a key
or a certificate; and only name, kind, expiry and enabled leave this module."""

import math
from datetime import UTC, datetime

from .az import az_json
from .common import listing, resolve
from .errors import RwError

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
    """(items that are expired or expire within the threshold, how many rows, how many have no
    expiry). A row whose expiry cannot be read is counted as having none and reported by name."""
    items, no_expiry, unreadable = [], 0, []
    for row in rows:
        attrs = row.get("attributes") if isinstance(row.get("attributes"), dict) else {}
        name = str(row.get("name") or "")
        raw = attrs.get("expires")
        expires = parse_expiry(raw)
        if expires is None:
            no_expiry += 1
            if raw:
                unreadable.append(name)
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
    return items, len(rows), no_expiry, unreadable


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
    found, scanned, no_expiry, failed = [], {}, {}, []
    for kind, plural, argv in KINDS:
        try:
            rows = az_json(ctx, [*argv, "--vault-name", name])
        except RwError as exc:
            failed.append((plural, exc))
            scanned[plural] = no_expiry[plural] = None
            continue
        if not isinstance(rows, list):
            raise RwError("internal", f"az keyvault {kind} list returned no list")
        items, count, none, _ = classify(rows, kind, now, threshold_days)
        found += items
        scanned[plural], no_expiry[plural] = count, none
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
        "expired": sum(1 for i in found if i["expired"]),
        "items": listing(found, max_items),
    }
