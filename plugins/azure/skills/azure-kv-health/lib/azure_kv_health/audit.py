"""audit-failures: failed Key Vault operations from the audit log in Log Analytics.

The vault must send its AuditEvent log to a Log Analytics workspace (a diagnostic setting). A vault
that does not is reported as such; "no failures" is only said when a workspace was queried."""

import json

from .az import az_json
from .common import NESTED_LIMIT, listing, resolve, span, window
from .errors import RwError

QUERY_URL = "https://api.loganalytics.io/v1/workspaces/{customer}/query"
QUERY_RESOURCE = "https://api.loganalytics.io"
MAX_WORKSPACES = 3
ROWS = 50
KQL = """AzureDiagnostics
| where ResourceProvider == "MICROSOFT.KEYVAULT" and ResourceId =~ "{resource_id}"
| where Category == "AuditEvent" and toint(httpStatusCode_d) >= 400
| summarize failures = count(), firstSeen = min(TimeGenerated), lastSeen = max(TimeGenerated) by OperationName, httpStatusCode = toint(httpStatusCode_d)
| order by failures desc
| take {rows}"""


def diagnostic_settings(reply):
    """The settings list of `az monitor diagnostic-settings list` (a list, or {value: [...]})."""
    if isinstance(reply, dict):
        reply = reply.get("value")
    if not isinstance(reply, list):
        raise RwError("internal", "az monitor diagnostic-settings list returned no list")
    return [s for s in reply if isinstance(s, dict)]


def audit_enabled(setting):
    return any(
        isinstance(log, dict) and log.get("category") == "AuditEvent" and log.get("enabled") is True
        for log in setting.get("logs") or []
    )


def describe(setting):
    return {
        "name": str(setting.get("name") or ""),
        "auditEvent": audit_enabled(setting),
        "logAnalytics": bool(setting.get("workspaceId")),
        "storageAccount": bool(setting.get("storageAccountId")),
        "eventHub": bool(setting.get("eventHubAuthorizationRuleId")),
    }


def query_body(resource_id, start, end):
    query = KQL.format(resource_id=resource_id.upper(), rows=ROWS)
    return json.dumps({"query": query, "timespan": f"{start}/{end}"}, separators=(",", ":"))


def rows_of(reply):
    """(operation, status, count, firstSeen, lastSeen) tuples from a Log Analytics response."""
    tables = reply.get("tables") if isinstance(reply, dict) else None
    if not isinstance(tables, list) or not tables:
        raise RwError("internal", "Log Analytics returned no table")
    table = tables[0]
    names = [c.get("name") for c in table.get("columns") or []]
    wanted = ["OperationName", "httpStatusCode", "failures", "firstSeen", "lastSeen"]
    if any(w not in names for w in wanted):
        raise RwError("internal", f"Log Analytics returned columns {names}, not {wanted}")
    index = [names.index(w) for w in wanted]
    return [tuple(row[i] for i in index) for row in table.get("rows") or []]


def merge(batches):
    """Sum the counts of the same (operation, status) across workspaces; widen first/last seen."""
    merged = {}
    for op, status, count, first, last in (row for rows in batches for row in rows):
        entry = merged.setdefault((op, status), [0, first, last])
        entry[0] += int(count)
        entry[1] = min(entry[1], first)
        entry[2] = max(entry[2], last)
    out = [
        {
            "operation": op or "",
            "httpStatusCode": status,
            "count": c,
            "firstSeen": f,
            "lastSeen": seen,
        }
        for (op, status), (c, f, seen) in merged.items()
    ]
    return sorted(out, key=lambda r: (-r["count"], r["operation"], r["httpStatusCode"] or 0))


def read(ctx, window_input, max_items):
    seconds = window(window_input)
    subscription, name, resource_id, _ = resolve(ctx)
    settings = diagnostic_settings(
        az_json(
            ctx,
            [
                "monitor",
                "diagnostic-settings",
                "list",
                "--resource",
                resource_id,
                "--subscription",
                subscription,
            ],
        )
    )
    shown = [describe(s) for s in settings][:NESTED_LIMIT]
    workspaces = []
    for s in settings:
        ws = s.get("workspaceId")
        if audit_enabled(s) and ws and ws not in workspaces:
            workspaces.append(ws)
    out = {
        "vault": name,
        "windowSeconds": int(seconds),
        "diagnosticSettings": shown,
        "diagnosticSettingsOmitted": max(len(settings) - len(shown), 0),
        "auditLoggingToWorkspace": bool(workspaces),
        "workspacesQueried": 0,
        "workspacesSkipped": max(len(workspaces) - MAX_WORKSPACES, 0),
    }
    start, end = span(ctx.now(), seconds)
    batches, failures = [], []
    for ws in workspaces[:MAX_WORKSPACES]:
        label = ws.rstrip("/").rsplit("/", 1)[-1]
        try:
            workspace = az_json(ctx, ["monitor", "log-analytics", "workspace", "show", "--ids", ws])
            customer = workspace.get("customerId") if isinstance(workspace, dict) else None
            if not customer:
                raise RwError("internal", "the workspace has no customerId")
            reply = az_json(
                ctx,
                [
                    "rest", "--method", "post",
                    "--url", QUERY_URL.format(customer=customer),
                    "--resource", QUERY_RESOURCE,
                    "--body", query_body(resource_id, start, end),
                ],
            )  # fmt: skip
            batches.append(rows_of(reply))
        except RwError as exc:
            failures.append((label, exc))
    if workspaces and len(failures) == len(workspaces[:MAX_WORKSPACES]):
        raise failures[0][1]  # nothing was queried: say why, not "no failures"
    for label, exc in failures:
        ctx.error(exc.code, exc.message, scope=f"workspace:{label}")
    rows = merge(batches)
    out["workspacesQueried"] = len(batches)
    out["failureCount"] = sum(r["count"] for r in rows) if batches else None
    out["failures"] = listing(rows, max_items) if batches else None
    return out
