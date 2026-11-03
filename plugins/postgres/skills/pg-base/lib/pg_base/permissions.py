"""check-permissions: the one query the server is asked and the verdicts built from it."""

from runwhen_capability.contracts import permission_hash

from . import access as ax
from .errors import RwError
from .pg import psql_json

SYSTEM_IDENTIFIER_SQL = (
    "SELECT json_build_object('systemIdentifier', system_identifier::text) FROM pg_control_system()"
)


def system_identifier(ctx):
    """The cluster's system_identifier as text, or None when this role may not read it (only
    permission_denied is soft: any other failure propagates)."""
    try:
        row = psql_json(ctx, SYSTEM_IDENTIFIER_SQL)
    except RwError as exc:
        if exc.code == "permission_denied":
            return None
        raise
    if not isinstance(row, dict):
        raise RwError("internal", "the server returned no system identifier row")
    value = row.get("systemIdentifier")
    return value if isinstance(value, str) and value else None


def _jsonable(value):
    """A copy of `value` that is plain JSON (string keys, finite floats in the canonical range,
    no other types), for a rule that cannot be hashed as it came."""
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and (value == 0 or 1e-6 <= abs(value) < 1e21):
        return value
    return str(value)


def _entry(rule):
    """(hash, rule as reported, None) for a rule, or the same with a reason when it cannot be
    hashed as it came: the entry then carries the hash of its plain-JSON form and is unverified."""
    try:
        return permission_hash(rule), rule, None
    except (ValueError, TypeError) as exc:
        plain_rule = _jsonable(rule)
        return permission_hash(plain_rule), plain_rule, f"the rule cannot be hashed: {exc}"


def check(ctx, rules):
    """The rw.access.v1 report for the rules (see CONTRACTS.md): one entry per rule, in order.
    A rule the task cannot hash or evaluate is `unverified` with the reason; it never fails the
    call."""
    if not isinstance(rules, list) or len(rules) > ax.MAX_RULES:
        raise RwError("invalid_input", f"rules must be a list of at most {ax.MAX_RULES} rules")
    rules = [ax.plain(r) for r in rules]
    if any(not isinstance(r, dict) or not r for r in rules):
        raise RwError("invalid_input", "every rule must be a non-empty object")
    entries = []
    for rule in rules:
        digest, shown, unhashable = _entry(rule)
        parsed, why = (None, unhashable) if unhashable else ax.parse_rule(rule)
        entries.append((digest, shown, parsed, why))

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
        if parsed is not None:
            try:
                verdict, scope, evidence = ax.verdict(parsed, facts)
            except (KeyError, TypeError, AttributeError):
                parsed, why = None, "the server's answer for this rule was malformed"
        if parsed is None:
            entry.update(verdict="unverified", evidence=ax._trim(f"not evaluated: {why}"))
        else:
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
