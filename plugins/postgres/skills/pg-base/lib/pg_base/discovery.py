"""discover: the instance and its databases, as facts and as an rw.discovery.v1 sync.

The queries return plain facts; `build_sync` turns them into the recorded sync (open, item
batches, commit) that papi's resource-sync API takes (CONTRACTS.md, "Discovery sync")."""

import os
import time

from .errors import RwError
from .pg import psql_json

PLATFORM = "postgres"
SOURCE = "pg-base/discover"
BATCH = 500  # items per batch (rw.discovery.v1 itemsBatch)
CANONICAL_KEY = "systemIdentifier"

INSTANCE_SQL = """SELECT json_build_object(
  'version', current_setting('server_version'),
  'versionNum', current_setting('server_version_num')::int,
  'inRecovery', pg_is_in_recovery(),
  'currentUser', current_user::text
)"""

SYSTEM_IDENTIFIER_SQL = (
    "SELECT json_build_object('systemIdentifier', system_identifier::text) FROM pg_control_system()"
)

# Not templates, and only databases that accept connections. pg_database_size() needs CONNECT
# on the database or pg_read_all_stats, and raises for the whole statement otherwise, so it is
# asked only where `sizeAllowed`; the others come back with a null size and are reported.
DATABASES_SQL = """SELECT coalesce(json_agg(json_build_object(
  'name', d.datname,
  'owner', pg_get_userbyid(d.datdba)::text,
  'encoding', pg_encoding_to_char(d.encoding),
  'sizeAllowed', p.ok,
  'sizeBytes', CASE WHEN p.ok THEN pg_database_size(d.oid) END
) ORDER BY d.datname), '[]'::json)
FROM pg_database d
CROSS JOIN LATERAL (
  SELECT has_database_privilege(d.oid, 'CONNECT')
      OR pg_has_role(current_user, 'pg_read_all_stats', 'USAGE') AS ok
) p
WHERE NOT d.datistemplate AND d.datallowconn"""


def target_name(resource):
    """The instance name from the run's target; anything but a postgres/instance is refused."""
    resource = resource or {}
    if resource.get("type") != f"{PLATFORM}/instance" or not resource.get("name"):
        raise RwError(
            "invalid_input",
            f"discover runs on a postgres/instance, got {resource.get('type')!r}",
        )
    return str(resource["name"])


def instance_facts(ctx):
    """The `instance` output: the connection's host and port, and what the server says about itself.
    systemIdentifier is filled in by the caller."""
    row = psql_json(ctx, INSTANCE_SQL)
    if not isinstance(row, dict):
        raise RwError("internal", "the server returned no instance facts")
    port = os.environ.get("PGPORT") or "5432"
    return {
        "host": os.environ.get("PGHOST") or "localhost",
        "port": int(port) if port.isdigit() else 5432,
        "version": row["version"],
        "versionNum": row["versionNum"],
        "inRecovery": bool(row["inRecovery"]),
        "systemIdentifier": None,
        "currentUser": row["currentUser"],
    }


def read_system_identifier(ctx, errors):
    """The system_identifier, or None with the reason appended to `errors` when it cannot be read."""
    try:
        row = psql_json(ctx, SYSTEM_IDENTIFIER_SQL)
    except RwError as exc:
        errors.append(
            {
                "code": exc.code,
                "message": f"system_identifier: {exc.message}",
                "scope": CANONICAL_KEY,
            }
        )
        return None
    value = (row or {}).get(CANONICAL_KEY)
    return value if isinstance(value, str) and value else None


def read_databases(ctx, errors):
    """The database facts, sizeBytes null (and an error appended) where the role may not size it."""
    rows = psql_json(ctx, DATABASES_SQL)
    if not isinstance(rows, list):
        raise RwError("internal", "the server returned no database list")
    databases = []
    for row in rows:
        if not row.get("sizeAllowed"):
            errors.append(
                {
                    "code": "permission_denied",
                    "message": (
                        "sizeBytes needs CONNECT on the database or membership of pg_read_all_stats"
                    ),
                    "scope": row["name"],
                }
            )
        databases.append(
            {
                "name": row["name"],
                "owner": row["owner"],
                "encoding": row["encoding"],
                "sizeBytes": row["sizeBytes"],
            }
        )
    return databases


def bound(items, limit):
    """(first `limit` items, total, omitted)."""
    return items[:limit], len(items), max(len(items) - limit, 0)


def database_listing(databases, limit):
    items, total, omitted = bound(databases, limit)
    return {"items": items, "total": total, "omitted": omitted}


def build_sync(path, name, instance, databases, summary):
    """The rw.discovery.v1 document for one full sync of the instance at `path`."""
    root = {"type": "instance", "name": name}
    items = [
        {
            "identity": {"platform": PLATFORM, "chain": [root]},
            **(
                {"providerUid": instance["systemIdentifier"]}
                if instance["systemIdentifier"]
                else {}
            ),
            "displayName": name,
            "document": {
                "host": instance["host"],
                "port": instance["port"],
                "version": instance["version"],
                "versionNum": instance["versionNum"],
                "systemIdentifier": instance["systemIdentifier"],
            },
            "status": {"inRecovery": instance["inRecovery"]},
        }
    ]
    for db in databases:
        items.append(
            {
                "identity": {
                    "platform": PLATFORM,
                    "chain": [root, {"type": "database", "name": db["name"]}],
                },
                "displayName": db["name"],
                "document": {k: db[k] for k in ("name", "owner", "encoding", "sizeBytes")},
            }
        )
    batches = [
        {"seq": i // BATCH + 1, "items": items[i : i + BATCH]} for i in range(0, len(items), BATCH)
    ]
    return {
        "apiVersion": "rw.discovery.v1",
        "open": {"source": SOURCE, "scopePath": path, "mode": "full"},
        "batches": batches,
        "commit": {
            "partitions": [
                {
                    "type": "database",
                    "parentPath": path,
                    "status": "complete",
                    "count": len(databases),
                }
            ],
            "summary": summary,
        },
    }


def discover(ctx, max_items):
    """Run discovery on the instance this task targets; returns the task's outputs and the
    sync document. A failure to connect or to list databases raises; a size the role may not
    read, or an unreadable system_identifier, is a ctx.error and discovery goes on."""
    started = time.monotonic()
    resource = ctx.resource
    name = target_name(resource)
    path = resource["path"]
    errors = []
    instance = instance_facts(ctx)  # connect first: a failure here fails the task
    instance["systemIdentifier"] = read_system_identifier(ctx, errors)
    databases = read_databases(ctx, errors)
    for error in errors:
        ctx.error(error["code"], error["message"], scope=error["scope"])

    summary = {
        "counts": {"instance": 1, "database": len(databases)},
        "partitions": {"complete": 1, "failed": 0, "forbidden": 0, "excluded": 0},
        "durationMs": int((time.monotonic() - started) * 1000),
    }
    if errors:
        summary["errors"] = errors
    if instance["systemIdentifier"]:
        summary["root"] = {
            "canonicalKey": {"name": CANONICAL_KEY, "value": instance["systemIdentifier"]}
        }
    sync = build_sync(path, name, instance, databases, summary)
    outputs = {
        "summary": summary,
        "instance": instance,
        "databases": database_listing(databases, max_items),
    }
    return outputs, sync
