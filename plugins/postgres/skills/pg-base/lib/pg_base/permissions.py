"""check-permissions: the one query the server is asked and the verdicts built from it."""

from runwhen_capability.contracts import permission_hash

from . import access as ax
from .errors import RwError
from .pg import psql_json

SYSTEM_IDENTIFIER_SQL = (
    "SELECT json_build_object('systemIdentifier', system_identifier::text) FROM pg_control_system()"
)


def system_identifier(ctx):
    """The cluster's system_identifier as text, or None when this role may not read it."""
    try:
        row = psql_json(ctx, SYSTEM_IDENTIFIER_SQL)
    except RwError as exc:
        if exc.code == "permission_denied":
            return None
        raise
    value = (row or {}).get("systemIdentifier")
    return value if isinstance(value, str) and value else None


def check(ctx, rules):
    """The rw.access.v1 report for the rules (see CONTRACTS.md): one entry per rule, in order."""
    if not isinstance(rules, list) or len(rules) > ax.MAX_RULES:
        raise RwError("invalid_input", f"rules must be a list of at most {ax.MAX_RULES} rules")
    rules = [ax.plain(r) for r in rules]
    if any(not isinstance(r, dict) or not r for r in rules):
        raise RwError("invalid_input", "every rule must be a non-empty object")
    try:
        entries = [(permission_hash(r), r, *ax.parse_rule(r)) for r in rules]
    except ValueError as exc:
        raise RwError("invalid_input", f"a rule cannot be hashed: {exc}") from None

    live = [check for _, _, check, _ in entries if check is not None]
    facts = None
    if live:
        roles = {name for kind, name, _ in live if kind == ax.ROLE}
        functions = {name for kind, name, _ in live if kind == ax.FUNCTION}
        facts = psql_json(ctx, ax.facts_sql(roles, functions))
        if not isinstance(facts, dict):
            raise RwError("internal", "the server returned no permission facts")

    out = []
    for digest, rule, parsed, why in entries:
        entry = {"hash": digest, "rule": rule}
        if parsed is None:
            entry.update(verdict="unverified", evidence=ax._trim(f"not evaluated: {why}"))
        else:
            verdict, scope, evidence = ax.verdict(parsed, facts)
            entry["verdict"] = verdict
            if scope:
                entry["scope"] = scope
            entry["evidence"] = evidence
        out.append(entry)

    report = {"checkedAt": ctx.now().strftime("%Y-%m-%dT%H:%M:%SZ"), "rules": out}
    sid = system_identifier(ctx)
    if sid:
        report["root"] = {"canonicalKey": {"name": "systemIdentifier", "value": sid}}
    return report
