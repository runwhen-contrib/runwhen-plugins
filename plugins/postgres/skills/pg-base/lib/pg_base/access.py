"""What a role may do, as PostgreSQL permissions: the pure half of check-permissions.

A rule is one of (see integration/schemas/pg-permission.json):

    {role: pg_monitor}                                  membership, with the role's privileges
    {database: orders | "*", privilege: CONNECT|CREATE|TEMPORARY}
    {function: pg_catalog.pg_control_system, privilege: EXECUTE}

`facts_sql` asks the server about every named role, database and function in one statement;
`verdict` turns the answer into granted | partial (with scope) | denied for each rule. Nothing
in this module runs a query."""

import re
from collections.abc import Mapping

from .pg import dollar_quote

MAX_RULES = 500
MAX_SCOPE = 500  # databases or overloads one `scope` lists; the evidence says when it was cut
EVIDENCE_LIMIT = 400
PRIVILEGES = ("CONNECT", "CREATE", "TEMPORARY")
ROLE, DATABASE, FUNCTION = "role", "database", "function"


def plain(value):
    """JSON-plain copy of a value that may be an SDK AttrView (read-only Mapping)."""
    if isinstance(value, Mapping):
        return {str(k): plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    return value


def _name(value, limit=63):
    return isinstance(value, str) and 0 < len(value) <= limit and "\x00" not in value


_FUNCTION = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*[.][A-Za-z_][A-Za-z0-9_$]*$")  # as the schema


def _function_name(value):
    return isinstance(value, str) and len(value) <= 127 and bool(_FUNCTION.match(value))


def parse_rule(rule):
    """((kind, name, privilege), None) for a rule this task can evaluate, else (None, why)."""
    if not isinstance(rule, dict):
        return None, "the rule is not an object"
    keys = set(rule)
    if keys == {"role"}:
        if not _name(rule["role"]):
            return None, "role must be a role name of 1 to 63 characters"
        return (ROLE, rule["role"], "USAGE"), None
    if keys == {"database", "privilege"}:
        if not _name(rule["database"]):
            return None, "database must be a database name or * (1 to 63 characters)"
        if rule["privilege"] not in PRIVILEGES:
            return None, f"privilege must be one of {', '.join(PRIVILEGES)}"
        return (DATABASE, rule["database"], rule["privilege"]), None
    if keys == {"function", "privilege"}:
        if not _function_name(rule["function"]):
            return None, "function must be a qualified name, schema.name"
        if rule["privilege"] != "EXECUTE":
            return None, "privilege must be EXECUTE"
        return (FUNCTION, rule["function"], "EXECUTE"), None
    return None, "a rule is {role}, {database, privilege} or {function, privilege}"


def _array(values):
    items = ", ".join(dollar_quote(v) for v in values)
    return f"ARRAY[{items}]::text[]"


def facts_sql(roles, functions):
    """One statement returning the facts every parsed rule needs, as one JSON object."""
    return f"""SELECT json_build_object(
  'user', current_user::text,
  'superuser', current_setting('is_superuser') = 'on',
  'databases', (
    SELECT coalesce(json_agg(json_build_object(
      'name', d.datname,
      'template', d.datistemplate,
      'allowConn', d.datallowconn,
      'CONNECT', has_database_privilege(d.oid, 'CONNECT'),
      'CREATE', has_database_privilege(d.oid, 'CREATE'),
      'TEMPORARY', has_database_privilege(d.oid, 'TEMPORARY')
    ) ORDER BY d.datname), '[]'::json)
    FROM pg_database d),
  'roles', (
    SELECT coalesce(json_agg(json_build_object(
      'role', q.r,
      'exists', q.oid IS NOT NULL,
      'usage', coalesce(pg_has_role(current_user, q.oid, 'USAGE'), false),
      'member', coalesce(pg_has_role(current_user, q.oid, 'MEMBER'), false)
    ) ORDER BY q.r), '[]'::json)
    FROM (
      SELECT r, (SELECT oid FROM pg_roles WHERE rolname = r) AS oid
      FROM unnest({_array(sorted(roles))}) AS r) q),
  'functions', (
    SELECT coalesce(json_agg(json_build_object(
      'function', f,
      'overloads', (
        SELECT coalesce(json_agg(json_build_object(
          'signature', p.proname || '(' || oidvectortypes(p.proargtypes) || ')',
          'execute', has_function_privilege(p.oid, 'EXECUTE')
        ) ORDER BY p.proname, oidvectortypes(p.proargtypes)), '[]'::json)
        FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname || '.' || p.proname = f)
    ) ORDER BY f), '[]'::json)
    FROM unnest({_array(sorted(functions))}) AS f)
)"""


def _trim(text, limit=EVIDENCE_LIMIT):
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _scoped(names):
    """(scope list, ' (cut ...)' note) for a verdict's scope."""
    if len(names) <= MAX_SCOPE:
        return names, ""
    return names[:MAX_SCOPE], f"; scope cut to the first {MAX_SCOPE} of {len(names)}"


def _count(n, noun):
    return f"{n} {noun}" + ("" if n == 1 else "s")


def _by(items, key):
    return {item[key]: item for item in items or []}


def verdict(check, facts):
    """(verdict, scope or None, evidence) for a parsed rule against the facts."""
    kind, name, privilege = check
    user = facts.get("user", "the role")
    if kind == ROLE:
        info = _by(facts.get("roles"), "role").get(name)
        if info is None:
            return "unverified", None, "the server did not report on this role"
        if not info.get("exists"):
            return "denied", None, _trim(f"role {name} does not exist on this server")
        if facts.get("superuser"):
            return (
                "granted",
                None,
                _trim(f"{user} is a superuser: it holds every role's privileges"),
            )
        if info.get("usage"):
            return (
                "granted",
                None,
                _trim(f"{user} is a member of {name} and inherits its privileges"),
            )
        if info.get("member"):
            return (
                "denied",
                None,
                _trim(
                    f"{user} is a member of {name} without INHERIT: its privileges are not in effect"
                ),
            )
        return "denied", None, _trim(f"{user} is not a member of {name}")
    if kind == DATABASE:
        databases = facts.get("databases") or []
        if name != "*":
            info = _by(databases, "name").get(name)
            if info is None:
                return "denied", None, _trim(f"database {name} does not exist on this server")
            if privilege == "CONNECT" and not info.get("allowConn"):
                return "denied", None, _trim(f"database {name} does not accept connections")
            if info.get(privilege):
                return "granted", None, _trim(f"{user} has {privilege} on {name}")
            return "denied", None, _trim(f"{user} lacks {privilege} on {name}")
        candidates = [d for d in databases if not d.get("template") and d.get("allowConn")]
        if not candidates:
            return (
                "unverified",
                None,
                "no database accepts connections, so there is nothing to check",
            )
        granted = [d["name"] for d in candidates if d.get(privilege)]
        if len(granted) == len(candidates):
            return (
                "granted",
                None,
                _trim(f"{user} has {privilege} on all {len(candidates)} databases"),
            )
        if not granted:
            return (
                "denied",
                None,
                _trim(f"{user} lacks {privilege} on all {len(candidates)} databases"),
            )
        scope, note = _scoped(granted)
        return (
            "partial",
            scope,
            _trim(f"{user} has {privilege} on {len(granted)} of {len(candidates)} databases{note}"),
        )
    info = _by(facts.get("functions"), "function").get(name)
    overloads = (info or {}).get("overloads") or []
    if not overloads:
        return "denied", None, _trim(f"function {name} does not exist on this server")
    allowed = [o["signature"] for o in overloads if o.get("execute")]
    if len(allowed) == len(overloads):
        return (
            "granted",
            None,
            _trim(f"{user} can EXECUTE {name} ({_count(len(overloads), 'overload')})"),
        )
    if not allowed:
        return (
            "denied",
            None,
            _trim(f"{user} cannot EXECUTE {name} ({_count(len(overloads), 'overload')})"),
        )
    scope, note = _scoped(allowed)
    return (
        "partial",
        scope,
        _trim(f"{user} can EXECUTE {len(allowed)} of {len(overloads)} overloads of {name}{note}"),
    )
