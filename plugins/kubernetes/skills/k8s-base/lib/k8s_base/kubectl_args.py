"""Validation of the `kubectl` task's command line (INT-8). Pure functions, no I/O.

The command is an allow-list, parsed the way kubectl's own flag parser (pflag) reads
it, re-derived from rw-discovery's `rwdiscovery/cli.py` and the `cli:` block of its
kubernetes pack. `parse_command` returns the *normalised* argv (every flag rewritten
as `--name=value`, so a value can never be read as another flag) and that argv is the
only thing the script ever runs; anything else raises RwError before kubectl starts:

- `invalid_input`: not a plain command, a verb outside the allow-list, a flag outside
  the verb's allow-list (or the `allowedFlags` setting), a bad flag value, a connection
  or identity flag (`--kubeconfig`, `--context`, `--server`, `--token`, `--as`, `--raw`,
  `--insecure-skip-tls-verify`, ...), which no setting can enable.
- `permission_denied`: a resource argument naming a type in `deniedResources`
  (default Secrets), whatever the verb, the flags or `-o` say.

Settings only narrow the tables below: a verb or flag must be in the table AND in the
setting; adding `delete` to `allowedVerbs` changes nothing.
"""

import re
import shlex
import string

from k8s_base.errors import RwError
from k8s_base.resources import expand_denied, variants

DEFAULT_ALLOWED_VERBS = ("get", "describe", "logs", "top", "events", "explain", "api-resources")
DEFAULT_DENIED_RESOURCES = ("secrets",)
ALWAYS_DENIED = ("secrets",)  # the floor: no setting removes it
DEFAULT_ALLOWED_FLAGS = (
    "n",
    "A",
    "l",
    "o",
    "since",
    "tail",
    "previous",
    "container",
    "show-labels",
    "sort-by",
    "field-selector",
    "no-headers",
    "timestamps",
    "since-time",
    "all-containers",
    "prefix",
    "for",
    "types",
    "show-kind",
    "label-columns",
    "ignore-not-found",
    "containers",
    "sum",
    "api-group",
    "namespaced",
    "verbs",
    "api-version",
    "recursive",
    "show-events",
)  # not chunk-size, max-log-requests or limit-bytes: they change load, not what is shown

# Every flag a verb below may take: name -> (short letter or None, takes a value).
FLAGS = {
    "namespace": ("n", True),
    "all-namespaces": ("A", False),
    "selector": ("l", True),
    "field-selector": (None, True),
    "output": ("o", True),
    "show-labels": (None, False),
    "label-columns": ("L", True),
    "sort-by": (None, True),
    "no-headers": (None, False),
    "ignore-not-found": (None, False),
    "show-kind": (None, False),
    "chunk-size": (None, True),
    "show-events": (None, False),
    "container": ("c", True),
    "previous": ("p", False),
    "since": (None, True),
    "since-time": (None, True),
    "tail": (None, True),
    "timestamps": (None, False),
    "limit-bytes": (None, True),
    "all-containers": (None, False),
    "prefix": (None, False),
    "max-log-requests": (None, True),
    "ignore-errors": (None, False),
    "containers": (None, False),
    "sum": (None, False),
    "for": (None, True),
    "types": (None, True),
    "recursive": (None, False),
    "api-version": (None, True),
    "namespaced": (None, False),
    "api-group": (None, True),
    "verbs": (None, True),
    "cached": (None, False),
    "client": (None, False),
    "list": (None, False),
    "subresource": (None, True),
    "quiet": ("q", False),
    "revision": (None, True),
}
SHORTS = {short: name for name, (short, _) in FLAGS.items() if short}

# Per verb: the flags it takes and the `-o` values it accepts (`x=*` accepts any `x=...`).
# Verbs that read files, run processes, open streams or write are absent on purpose:
# no -f/--filename, --watch, --follow, --template, jsonpath-file, custom-columns-file.
VERBS = {
    "get": {
        "flags": (
            "namespace all-namespaces selector field-selector output show-labels label-columns "
            "sort-by no-headers ignore-not-found show-kind chunk-size"
        ).split(),
        "outputs": (
            "name",
            "wide",
            "json",
            "yaml",
            "jsonpath=*",
            "jsonpath-as-json=*",
            "custom-columns=*",
        ),
    },
    "describe": {
        "flags": "namespace all-namespaces selector show-events chunk-size".split(),
    },
    "logs": {
        "flags": (
            "namespace container previous since since-time tail timestamps limit-bytes "
            "all-containers prefix selector max-log-requests ignore-errors"
        ).split(),
    },
    "top": {
        "flags": "namespace all-namespaces selector containers sort-by no-headers sum".split(),
    },
    "events": {
        "flags": "namespace all-namespaces for types output no-headers chunk-size".split(),
        "outputs": ("name", "json", "yaml", "jsonpath=*"),
    },
    "explain": {
        "flags": "recursive api-version output".split(),
        "outputs": ("plaintext", "plaintext-openapiv2"),
    },
    "api-resources": {
        "flags": "namespaced api-group verbs output no-headers sort-by cached".split(),
        "outputs": ("wide", "name"),
    },
    "api-versions": {"flags": []},
    "version": {"flags": ["output", "client"], "outputs": ("json", "yaml")},
    "auth can-i": {
        "flags": "namespace all-namespaces list subresource no-headers quiet".split(),
    },
    "rollout history": {
        "flags": "namespace revision output selector".split(),
        "outputs": ("name", "json", "yaml", "jsonpath=*"),
    },
}

# Flags that change which cluster, identity or transport a command uses, or print
# credentials (-v). Refused whatever the settings say; listed so the error is specific.
ALWAYS_REFUSED = frozenset(
    {
        "kubeconfig",
        "context",
        "cluster",
        "user",
        "server",
        "token",
        "as",
        "as-group",
        "as-uid",
        "raw",
        "insecure-skip-tls-verify",
        "certificate-authority",
        "client-certificate",
        "client-key",
        "tls-server-name",
        "username",
        "password",
        "cache-dir",
        "request-timeout",
        "proxy-url",
        "disable-compression",
        "kuberc",
        "v",
        "vmodule",
        "profile",
        "profile-output",
        "log-flush-frequency",
        "warnings-as-errors",
        "match-server-version",
    }
)
ALWAYS_REFUSED_SHORT = {"s": "server", "v": "v"}
# Refused for a reason other than the connection, named in the error.
REFUSED_REASON = {
    "v": "raises kubectl's log verbosity, which can print request headers and credentials",
    "vmodule": "raises kubectl's log verbosity, which can print request headers and credentials",
}

# A token that is one of these, starts with one of the prefixes or ends with ";" is shell
# syntax. Nothing here runs a shell, so it could only ever reach kubectl as a stray
# argument; say so instead.
_SHELL_TOKENS = frozenset({"|", "||", "&", "&&", ";", ">", ">>", "<", "<<"})
_SHELL_PREFIXES = ("$(", "`")
_UNREPEATABLE = frozenset({"output"})
_BOOLEAN_VALUES = frozenset({"true", "false"})
_INTEGER_RE = re.compile(r"-?[0-9]+")

MAX_COMMAND_CHARS = 4096
LOGS_DEFAULT_TAIL = "200"
LOGS_LIMIT_BYTES = 1 << 20  # kubectl logs stops reading after this many bytes
OUTPUT_MAX_BYTES = 1 << 20  # the task never returns more than this, whatever maxOutputLines is


def _invalid(message):
    return RwError("invalid_input", message)


def tokenise(command):
    """The command as argv tokens (a leading `kubectl` is dropped); never a shell."""
    if not isinstance(command, str):
        raise _invalid("command must be a string of kubectl arguments")
    if "\x00" in command:
        raise _invalid("the command contains a NUL character")
    if len(command) > MAX_COMMAND_CHARS:
        raise _invalid(f"the command is longer than {MAX_COMMAND_CHARS} characters")
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError as exc:
        raise _invalid(f"the command could not be split into arguments: {exc}") from None
    if tokens and tokens[0] == "kubectl":
        tokens = tokens[1:]
    if not tokens:
        raise _invalid("pass one kubectl command, e.g. get pods -n <namespace>")
    for token in tokens:
        if token in _SHELL_TOKENS or token.startswith(_SHELL_PREFIXES) or token.endswith(";"):
            raise _invalid(
                "no shell: one kubectl command per call, no pipes, redirects or chaining"
            )
    return tokens


def _norm_flags(allowed_flags):
    """The `allowedFlags` setting (long names or short letters, with or without dashes)
    as a set of long flag names."""
    names = set()
    for entry in allowed_flags:
        entry = str(entry).lstrip("-")
        if entry in FLAGS:
            names.add(entry)
        elif entry in SHORTS:
            names.add(SHORTS[entry])
    return names


def _match_verb(tokens, allowed_verbs):
    """(verb, tokens consumed): the two-word verb (`auth can-i`) when that pair is one."""
    allowed = {str(v).strip() for v in allowed_verbs}
    candidates = [(f"{tokens[0]} {tokens[1]}", 2)] if len(tokens) >= 2 else []
    candidates.append((tokens[0], 1))
    for verb, used in candidates:
        if verb in VERBS and verb in allowed:
            return verb, used
    shown = ", ".join(sorted(v for v in allowed if v in VERBS))
    raise _invalid(f"'{tokens[0]}' is not an allowed verb; allowed: {shown or 'none'}")


class _Walker:
    """Left-to-right pass over the tokens after the verb, the way pflag reads them."""

    def __init__(self, args, verb, allowed_flags):
        self.args = args
        self.i = 0
        self.verb = verb
        self.allowed = set(VERBS[verb]["flags"]) & _norm_flags(allowed_flags)
        self.positionals = []
        self.flags = []  # (name, value or None) in order

    def run(self):
        while self.i < len(self.args):
            token = self.args[self.i]
            self.i += 1
            if token == "--":
                raise _invalid("-- is not allowed")
            if token.startswith("--"):
                self._long(token)
            elif len(token) > 1 and token[0] == "-" and token[1] in string.ascii_letters:
                self._short_cluster(token)
            elif len(token) > 1 and token[0] == "-":
                raise _invalid(f"{token} looks like a flag but is not one kubectl accepts here")
            else:
                self.positionals.append(token)
        return self.positionals, self.flags

    def _resolve(self, name, shown):
        if name in ALWAYS_REFUSED:
            why = REFUSED_REASON.get(
                name, "it changes the cluster, identity or transport this task runs against"
            )
            raise _invalid(f"{shown} is never allowed: {why}")
        if name not in FLAGS or name not in self.allowed:
            allowed = ", ".join(self._shown_allowed())
            raise _invalid(f"{shown} is not allowed for {self.verb}; allowed: {allowed or 'none'}")
        if name in _UNREPEATABLE and any(n == name for n, _ in self.flags):
            raise _invalid(f"--{name} given twice; pass it once")
        return name

    def _shown_allowed(self):
        out = []
        for name in VERBS[self.verb]["flags"]:
            if name in self.allowed:
                short = FLAGS[name][0]
                out.append(f"--{name} (-{short})" if short else f"--{name}")
        return out

    def _next_value(self, shown):
        if self.i >= len(self.args):
            raise _invalid(f"{shown} needs a value")
        value = self.args[self.i]
        self.i += 1
        return value

    def _long(self, token):
        given, has_equals, inline = token[2:].partition("=")
        name = self._resolve(given, f"--{given}")
        if FLAGS[name][1]:
            self.flags.append((name, inline if has_equals else self._next_value(f"--{name}")))
        elif has_equals:
            if inline.lower() not in _BOOLEAN_VALUES:
                raise _invalid(f"--{name} takes true or false, not '{inline}'")
            self.flags.append((name, inline.lower()))
        else:
            self.flags.append((name, None))

    def _short_cluster(self, token):
        """`-abc`, `-ovalue`, `-o=value` or `-o value`: booleans continue the cluster, a
        value flag ends it."""
        for j in range(1, len(token)):
            ch = token[j]
            if ch in ALWAYS_REFUSED_SHORT:
                self._resolve(ALWAYS_REFUSED_SHORT[ch], f"-{ch}")
            name = self._resolve(SHORTS.get(ch), f"-{ch}")
            if not FLAGS[name][1]:
                self.flags.append((name, None))
                continue
            rest = token[j + 1 :].removeprefix("=")
            self.flags.append((name, rest or self._next_value(f"-{ch} (--{name})")))
            return


def _check_output(value, verb):
    allowed = VERBS[verb].get("outputs", ())
    for entry in allowed:
        if entry.endswith("=*"):
            if value.startswith(entry[:-1]) and len(value) > len(entry) - 1:
                return
        elif value == entry:
            return
    raise _invalid(f"-o {value} is not allowed for {verb}; allowed: {', '.join(allowed) or 'none'}")


def resource_type(part):
    """One comma-separated resource argument as a bare, lower-cased type: `/name` and
    `.version.group` stripped, so `Secret`, `secrets.v1`, `secret/s1` and
    `secrets.v1./s1` all give `secret(s)`."""
    return part.split("/", 1)[0].lower().split(".", 1)[0].strip()


def denied_type(arg, denied_names):
    """The denied type named by one resource argument (a comma list, `type/name` or
    `type.group`), or None. `denied_names` is the set `denied_names()` returns."""
    for part in arg.split(","):
        kind = resource_type(part)
        if kind and variants(kind) & denied_names:
            return kind
    return None


def denied_names(denied_resources, resolver=None):
    """Every spelling of the denied resources: the setting's entries plus `secrets`, which
    no setting can remove (a floor like ALWAYS_REFUSED for flags). Plural, singular, kind
    and short names resolve through resources.py; `resolver()` (rows of `kubectl
    api-resources -o wide`) is called once for a name that table lacks, and an entry that
    resolves nowhere raises invalid_input."""
    return expand_denied([*ALWAYS_DENIED, *denied_resources], resolver)


def _check_denied(verb, positionals, flags, denied_resources, resolver=None):
    """Refuse a denied type in any resource argument. The first positional is the type
    (list) of get/describe/top/explain/..., later ones are names unless they are
    `type/name`; `auth can-i VERB TYPE` carries the type in any position."""
    args = list(positionals) if verb == "auth can-i" else positionals[:1]
    args += [p for p in positionals[1:] if "/" in p]
    args += [value for name, value in flags if name == "for" and value]
    if not args:
        return
    names = denied_names(denied_resources, resolver)
    for arg in args:
        kind = denied_type(arg, names)
        if kind:
            raise RwError(
                "permission_denied",
                f"'{kind}' is a denied resource type for this task (deniedResources); "
                "no verb or flag can read it through here",
            )


def _logs_args(flags, max_output_lines):
    """Validate `logs` values and return the defaults to append: a tail window when the
    caller gave none (the last 200 lines, or maxOutputLines when a --since window is set,
    so the newest lines of the window are the ones returned), and a byte limit."""
    for name, value in flags:
        if name == "tail" and (value is None or not re.fullmatch(r"[0-9]+", value)):
            raise _invalid(f"--tail must be a non-negative integer, not '{value}'")
        if name == "limit-bytes":
            if value is None or not _INTEGER_RE.fullmatch(value) or int(value) < 1:
                raise _invalid(f"--limit-bytes must be a positive integer, not '{value}'")
    defaults = []
    if not any(n == "tail" for n, _ in flags):
        windowed = any(n in ("since", "since-time") for n, _ in flags)
        defaults.append(f"--tail={max_output_lines if windowed else LOGS_DEFAULT_TAIL}")
    if not any(n == "limit-bytes" for n, _ in flags):
        defaults.append(f"--limit-bytes={LOGS_LIMIT_BYTES}")
    return defaults


def parse_command(
    command,
    *,
    allowed_verbs=DEFAULT_ALLOWED_VERBS,
    denied_resources=DEFAULT_DENIED_RESOURCES,
    allowed_flags=DEFAULT_ALLOWED_FLAGS,
    max_output_lines=2000,
    resolver=None,
):
    """Validate `command` and return the normalised kubectl argv (no leading `kubectl`).
    Raises RwError: invalid_input for anything outside the allow-lists (or a
    deniedResources entry that cannot be resolved), permission_denied for a denied
    resource type. The first violation wins. `resolver` returns the rows of
    `kubectl api-resources -o wide` and is only called for a denied name outside the
    built-in table."""
    tokens = tokenise(command)
    verb, used = _match_verb(tokens, allowed_verbs)
    positionals, flags = _Walker(tokens[used:], verb, allowed_flags).run()
    for name, value in flags:
        if name == "output":
            _check_output(value, verb)
    for p in positionals:
        if p.startswith("-"):
            raise _invalid(f"'{p}' is not a valid resource argument")
    _check_denied(verb, positionals, flags, denied_resources, resolver)
    defaults = _logs_args(flags, max_output_lines) if verb == "logs" else []
    rendered = [f"--{n}" if v is None else f"--{n}={v}" for n, v in flags]
    return [*tokens[:used], *positionals, *rendered, *defaults]


def truncate_output(text, max_lines, max_bytes=OUTPUT_MAX_BYTES, from_end=False):
    """(text, total_lines, truncated): `max_lines` lines (the first, or with `from_end` the
    last, which is where the newest log lines are), at most `max_bytes` bytes. total_lines
    counts the whole text."""
    lines = text.splitlines()
    total = len(lines)
    kept_lines = lines[-max_lines:] if from_end else lines[:max_lines]
    kept = "\n".join(kept_lines)
    if kept_lines:
        kept += "\n"
    cut = total > max_lines
    raw = kept.encode("utf-8")
    if len(raw) > max_bytes:
        if from_end:
            kept = raw[-max_bytes:].decode("utf-8", errors="ignore")
        else:
            kept = raw[:max_bytes].decode("utf-8", errors="ignore")
        cut = True
    return kept, total, cut
