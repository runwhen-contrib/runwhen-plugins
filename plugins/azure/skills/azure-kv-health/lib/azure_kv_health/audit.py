"""audit-failures: failed Key Vault operations from the audit log in Log Analytics.

The vault must send its audit log to a Log Analytics workspace (a diagnostic setting). A vault that
does not is reported as such; "no failures" is only said when a workspace was queried.

Which table holds the log depends on the diagnostic setting's `logAnalyticsDestinationType`:
  - `Dedicated` (resource-specific tables): `AZKVAuditLogs`. Its columns here are `_ResourceId`,
    `HttpStatusCode` (int), `OperationName` and `TimeGenerated`.
    https://learn.microsoft.com/azure/azure-monitor/reference/tables/azkvauditlogs
  - otherwise (Azure diagnostics, the default): `AzureDiagnostics`, with `ResourceProvider`,
    `ResourceId`, `Category`, `OperationName` and `httpStatusCode_d` (a double).
    https://learn.microsoft.com/azure/azure-monitor/reference/tables/azurediagnostics
How the destination type picks the table:
https://learn.microsoft.com/azure/azure-monitor/platform/resource-logs#send-to-log-analytics-workspace
"""

import json
import time

from .az import az_json, command_timeout
from .common import NESTED_LIMIT, listing, span, window
from .errors import RwError
from .vault import VAULT_ID, resolve

QUERY_PATH = "/v1/workspaces/{customer}/query"
MAX_WORKSPACES = 3  # (workspace, table) pairs queried; the rest are counted, not queried
ROWS = 50  # failed operation/status groups one query returns; it asks for one more to see a cut
BUDGET = 540  # seconds for the whole task (tasks.yaml: timeout 600s), less a margin for the result
LEGACY, DEDICATED = "AzureDiagnostics", "AZKVAuditLogs"
KQL = {
    LEGACY: """AzureDiagnostics
| where ResourceProvider == "MICROSOFT.KEYVAULT" and ResourceId =~ "{resource_id}"
| where Category == "AuditEvent" and toint(httpStatusCode_d) >= 400
| summarize failures = count(), firstSeen = min(TimeGenerated), lastSeen = max(TimeGenerated) by OperationName, httpStatusCode = toint(httpStatusCode_d)
| order by failures desc
| take {rows}""",
    DEDICATED: """AZKVAuditLogs
| where _ResourceId =~ "{resource_id}" and toint(HttpStatusCode) >= 400
| summarize failures = count(), firstSeen = min(TimeGenerated), lastSeen = max(TimeGenerated) by OperationName, httpStatusCode = toint(HttpStatusCode)
| order by failures desc
| take {rows}""",
}
AUDIT_GROUPS = ("audit", "alllogs")  # category groups that include the AuditEvent category


def diagnostic_settings(reply):
    """The settings list of `az monitor diagnostic-settings list` (a list, or {value: [...]})."""
    if isinstance(reply, dict):
        reply = reply.get("value")
    if not isinstance(reply, list):
        raise RwError("internal", "az monitor diagnostic-settings list returned no list")
    return [s for s in reply if isinstance(s, dict)]


def audit_enabled(setting):
    """The setting sends the audit log: the AuditEvent category, or a category group that holds it
    (`audit`, `allLogs`), switched on. Azure Monitor settings made in the portal since 2022 use the
    groups and leave `category` null."""
    for log in setting.get("logs") or []:
        if not isinstance(log, dict) or log.get("enabled") is not True:
            continue
        if str(log.get("category") or "").lower() == "auditevent":
            return True
        if str(log.get("categoryGroup") or "").lower() in AUDIT_GROUPS:
            return True
    return False


def table_of(setting):
    """The Log Analytics table this setting's logs land in."""
    kind = setting.get("logAnalyticsDestinationType")
    return DEDICATED if str(kind or "").lower() == "dedicated" else LEGACY


def describe(setting):
    sends = bool(setting.get("workspaceId"))
    return {
        "name": str(setting.get("name") or ""),
        "auditEvent": audit_enabled(setting),
        "logAnalytics": sends,
        "table": table_of(setting) if sends else None,
        "storageAccount": bool(setting.get("storageAccountId")),
        "eventHub": bool(setting.get("eventHubAuthorizationRuleId")),
    }


def kql_quote(text):
    """`text` as the inside of a KQL double-quoted string: backslash and quote escaped."""
    return str(text).replace("\\", "\\\\").replace('"', '\\"')


def query_body(resource_id, table, start, end):
    """The JSON body of the Log Analytics query for one vault in one table. The id is checked to
    be a vault's resource id first and escaped for KQL anyway."""
    if not VAULT_ID.match(resource_id):
        raise RwError("internal", "the vault's resource id is not a vault resource id")
    query = KQL[table].format(resource_id=kql_quote(resource_id), rows=ROWS + 1)
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


def query_endpoint(ctx):
    """The Log Analytics endpoint of the active cloud, from `az cloud show` (a local read: the
    public cloud's https://api.loganalytics.io, a national cloud's own)."""
    cloud = az_json(ctx, ["cloud", "show"])
    endpoints = cloud.get("endpoints") if isinstance(cloud, dict) else None
    endpoint = endpoints.get("logAnalyticsResourceId") if isinstance(endpoints, dict) else None
    if not isinstance(endpoint, str) or not endpoint.startswith("https://"):
        raise RwError("dependency", "the active Azure cloud has no Log Analytics query endpoint")
    return endpoint.rstrip("/")


def targets_of(settings):
    """The (workspace id, table) pairs the audit log is sent to, in order, once each."""
    found = []
    for s in settings:
        pair = (s.get("workspaceId"), table_of(s))
        if audit_enabled(s) and pair[0] and pair not in found:
            found.append(pair)
    return found


def read(ctx, window_input, max_items, *, clock=time.monotonic, budget=BUDGET):
    started = clock()
    seconds = window(window_input)
    per_call = command_timeout(ctx.settings)
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
    targets = targets_of(settings)
    queried = targets[:MAX_WORKSPACES]
    out = {
        "vault": name,
        "windowSeconds": int(seconds),
        "diagnosticSettings": shown,
        "diagnosticSettingsOmitted": max(len(settings) - len(shown), 0),
        "auditLoggingToWorkspace": bool(targets),
        "table": None,
        "workspacesQueried": 0,
        "workspacesSkipped": max(len(targets) - MAX_WORKSPACES, 0),
        "truncated": False,
        "failureCount": None,
        "failures": None,
    }
    if not queried:
        return out
    endpoint = query_endpoint(ctx)
    start, end = span(ctx.now(), seconds)
    batches, failures, tables, cut = [], [], [], False
    for ws, table in queried:
        label = ws.rstrip("/").rsplit("/", 1)[-1]
        try:
            if clock() - started + 2 * per_call > budget:
                # each workspace takes two az calls; start none that could run past the task budget
                raise RwError(
                    "timeout", "no time left in the task's budget to query this workspace"
                )
            workspace = az_json(ctx, ["monitor", "log-analytics", "workspace", "show", "--ids", ws])
            customer = workspace.get("customerId") if isinstance(workspace, dict) else None
            if not customer:
                raise RwError("internal", "the workspace has no customerId")
            reply = az_json(
                ctx,
                [
                    "rest", "--method", "post",
                    "--url", endpoint + QUERY_PATH.format(customer=customer),
                    "--resource", endpoint,
                    "--body", query_body(resource_id, table, start, end),
                ],
            )  # fmt: skip
            rows = rows_of(reply)
            cut = cut or len(rows) > ROWS
            batches.append(rows[:ROWS])
            if table not in tables:
                tables.append(table)
        except RwError as exc:
            failures.append((label, exc))
    if len(failures) == len(queried):
        raise failures[0][1]  # nothing was queried: say why, not "no failures"
    for label, exc in failures:
        ctx.error(exc.code, exc.message, scope=f"workspace:{label}")
    rows = merge(batches)
    out["table"] = ", ".join(sorted(tables))
    out["workspacesQueried"] = len(batches)
    out["truncated"] = cut
    out["failureCount"] = sum(r["count"] for r in rows)
    out["failures"] = listing(rows, max_items)
    return out
