"""plugin.yaml and the plugin layout (the SDK's archive.md section 3a, rule budget of section 4).

Ported unchanged from the plugin registry's `manifest.py`.
"""

import re
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel
from runwhen_capability.contracts import (
    ContractError,
    ContractViolation,
    check,
    load_yaml,
    split_frontmatter,
    validate,
)

from plugin_tools.archive import E_INVALID_PLUGIN, FileEntry

# archive.md section 4 (D60).
MAX_RULES = 10
MAX_RULE_WORDS = 80

PLUGIN_YAML = "plugin.yaml"
INTEGRATION_FILES = (
    "integration/integration.yaml",
    "integration/types.yaml",
    "integration/facets.yaml",
    "integration/dependencies.yaml",
)

_ROOT_FILES = frozenset({PLUGIN_YAML, "README.md", "LICENSE", "CHANGELOG.md", "connections.yaml"})
_INTEGRATION_TREES = ("integration/schemas/", "integration/auth/")
_SKILL_SUBTREES = ("scripts/", "lib/", "schemas/", "references/", "assets/", "tests" + "/")
_SKILL_PATH = re.compile(r"^skills/([^/]+)/(.+)$")
_RULE_PATH = re.compile(r"^rules/([^/]+)\.md$")
_KNOWLEDGE_PATH = re.compile(r"^knowledge/([^/]+)\.md$")

Detail = dict[str, str]


class InvalidPlugin(Exception):
    """A plugin tree is rejected. `details` are `{"path", "message"}` entries for the API."""

    code = E_INVALID_PLUGIN

    def __init__(self, detail: str, details: list[Detail] | None = None) -> None:
        super().__init__(detail)
        self.detail = detail
        self.details = details or []


class PluginManifest(BaseModel):
    name: str
    version: str
    description: str = ""
    requires: dict[str, str] = {}


def violation_details(file: str, violations: list[ContractViolation]) -> list[Detail]:
    return [
        {"path": f"{file}:{v.path}" if v.path else file, "message": v.message} for v in violations
    ]


def summarize(what: str, details: list[Detail]) -> str:
    first = details[0]
    more = f" (and {len(details) - 1} more)" if len(details) > 1 else ""
    return f"{what}: {first['path']}: {first['message']}{more}"


def _load(file: str, data: bytes) -> Any:
    """Decode and parse one YAML file (YAML 1.2); InvalidPlugin when it cannot be read."""
    try:
        return load_yaml(data.decode("utf-8"))
    except UnicodeDecodeError:
        message = "not valid UTF-8"
    except RecursionError:
        message = "nested too deeply"
    except Exception as exc:  # the parser's own errors, whatever their type
        message = f"not valid YAML: {exc}"
    raise InvalidPlugin(f"{file} {message}", [{"path": file, "message": message}])


def parse_plugin_yaml(data: bytes) -> PluginManifest:
    """Parse and schema-check plugin.yaml (`E_INVALID_PLUGIN` with the violations as details)."""
    doc = _load(PLUGIN_YAML, data)
    try:
        check("plugin", doc)
    except ContractError as exc:
        details = violation_details(PLUGIN_YAML, exc.violations)
        raise InvalidPlugin(summarize("plugin.yaml is invalid", details), details) from exc
    return PluginManifest.model_validate(doc)


def _check_yaml(path: str, data: bytes, schema: str, details: list[Detail]) -> None:
    try:
        doc = _load(path, data)
    except InvalidPlugin as exc:
        details.extend(exc.details)
        return
    details.extend(violation_details(path, validate(schema, doc)))


def _check_markdown(
    path: str, data: bytes, schema: str, expected_name: str, details: list[Detail]
) -> str | None:
    """Check a frontmatter file; returns the body when the frontmatter is usable."""
    try:
        frontmatter, body = split_frontmatter(data.decode("utf-8"))
    except UnicodeDecodeError:
        details.append({"path": path, "message": "not valid UTF-8"})
        return None
    except RecursionError:
        details.append({"path": path, "message": "frontmatter is nested too deeply"})
        return None
    except Exception as exc:  # ValueError, yaml.YAMLError or anything else the parser raises
        details.append({"path": path, "message": f"bad frontmatter: {exc}"})
        return None
    problems = violation_details(path, validate(schema, frontmatter))
    details.extend(problems)
    if not problems and frontmatter["name"] != expected_name:
        details.append(
            {
                "path": f"{path}:/name",
                "message": f"must equal the {'folder' if path.endswith('SKILL.md') else 'file'} "
                f"name {expected_name!r}, got {frontmatter['name']!r}",
            }
        )
    return body


def _layout_problem(path: str) -> bool:
    """True when `path` is outside the archive.md section 3a allowlist."""
    if path in _ROOT_FILES or path in INTEGRATION_FILES:
        return False
    if path.startswith(_INTEGRATION_TREES):
        return False
    if _RULE_PATH.match(path) or _KNOWLEDGE_PATH.match(path):
        return False
    m = _SKILL_PATH.match(path)
    if m:
        rest = m[2]
        return not (rest in ("SKILL.md", "tasks.yaml") or rest.startswith(_SKILL_SUBTREES))
    return True


def check_layout_paths(files: Mapping[str, FileEntry]) -> None:
    """Only the allowlist part of `validate_layout`: every path must be part of the plugin layout.

    For a tree that is not compiled (a draft) but must not carry files a plugin cannot, such as a
    CI definition. Raises one InvalidPlugin listing every path outside the layout.
    """
    details: list[Detail] = [
        {"path": path, "message": "E_PATH_NOT_ALLOWED: not part of the plugin layout"}
        for path in files
        if _layout_problem(path)
    ]
    if details:
        raise InvalidPlugin(summarize("plugin layout is invalid", details), details)


def validate_layout(files: Mapping[str, FileEntry]) -> None:
    """Check the allowlist, the rule budget and every frontmatter/tasks file of a normalised tree.

    Raises one InvalidPlugin listing every problem found.
    """
    details: list[Detail] = []
    skills: set[str] = set()
    rules: list[str] = []
    for path in files:
        if _layout_problem(path):
            details.append(
                {"path": path, "message": "E_PATH_NOT_ALLOWED: not part of the plugin layout"}
            )
            continue
        if m := _SKILL_PATH.match(path):
            skills.add(m[1])
        elif _RULE_PATH.match(path):
            rules.append(path)

    for skill in sorted(skills):
        base = f"skills/{skill}"
        skill_md = files.get(f"{base}/SKILL.md")
        if skill_md is None:
            details.append({"path": f"{base}/SKILL.md", "message": "required file is missing"})
        else:
            _check_markdown(f"{base}/SKILL.md", skill_md.data, "skill-frontmatter", skill, details)
        tasks = files.get(f"{base}/tasks.yaml")
        if tasks is not None:
            _check_yaml(f"{base}/tasks.yaml", tasks.data, "tasks", details)

    if len(rules) > MAX_RULES:
        details.append(
            {"path": "rules", "message": f"at most {MAX_RULES} rules per plugin, got {len(rules)}"}
        )
    for path in sorted(rules):
        stem = _RULE_PATH.match(path)[1]  # type: ignore[index]
        body = _check_markdown(path, files[path].data, "rule-frontmatter", stem, details)
        if body is not None and len(body.split()) > MAX_RULE_WORDS:
            details.append(
                {
                    "path": path,
                    "message": f"rule body is {len(body.split())} words, over {MAX_RULE_WORDS}",
                }
            )
    for path in sorted(p for p in files if _KNOWLEDGE_PATH.match(p)):
        stem = _KNOWLEDGE_PATH.match(path)[1]  # type: ignore[index]
        _check_markdown(path, files[path].data, "knowledge-frontmatter", stem, details)

    if details:
        raise InvalidPlugin(summarize("plugin layout is invalid", details), details)
