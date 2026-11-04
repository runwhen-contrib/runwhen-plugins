"""Repo guard for security finding S17: no skill reaches a backend through `kubectl ... --raw` on a
pod or Service proxy path.

`kubectl get --raw /api/v1/namespaces/<ns>/{pods,services}/<name>:<port>/proxy/...` follows a
backend's 301/302/307 and, for an absolute Location on another host, re-sends the runner's bearer
token there. The write verbs leak the same way: lane A (kind) found that `kubectl replace|create|
delete --raw` follow a 302 as a GET carrying the bearer token, and `delete --raw` also follows a 307
as a DELETE carrying it. Skills read a proxied backend with `ctx.k8s_proxy_get` and write to one with
`ctx.k8s_proxy_request`, which never follow a redirect. There is no exception.

The check reads the string constants of every skill's `lib/` and `scripts/` Python, as one set per
skill (a skill builds the path in one module and passes it to `--raw` in another); docstrings and
comments are not code and are skipped, and an f-string counts as one string. A skill is a violation
when it has any of
  - a proxy path (`/proxy/`, or `/namespaces/` ... `/proxy`) among its strings,
  - `--raw` and a proxy path among them (the original S17 rule), or
  - a list literal holding `--raw` and one of the verbs replace, create, delete or patch.
`--raw /version`-style paths of the API server itself (k8s-base) have no proxy path and pass.
"""

import ast
from pathlib import Path

import pytest

PLUGIN = Path(__file__).parents[2]
SKILLS = PLUGIN / "skills"
# the kubectl verbs that must never carry `--raw` (they follow a redirect with the bearer token)
WRITE_VERBS = {"replace", "create", "delete", "patch"}
KUBECTL_VERBS = {"get", "apply", *WRITE_VERBS}


def skill_files():
    """{skill name: its lib/ and scripts/ Python files}."""
    found = {}
    for skill in sorted(SKILLS.iterdir()):
        files = sorted(
            [*skill.glob("lib/**/*.py"), *skill.glob("scripts/*.py")],
        )
        files = [f for f in files if "__pycache__" not in f.parts]
        if files:
            found[skill.name] = files
    return found


def docstring_nodes(tree):
    skip = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                skip.add(id(body[0].value))
    return skip


def strings(tree):
    """The strings of the code: each constant, and each f-string as one text with `{}` for its
    expressions. Docstrings are not code."""
    skip = docstring_nodes(tree)
    parts = {
        id(child)
        for node in ast.walk(tree)
        if isinstance(node, ast.JoinedStr)
        for child in ast.walk(node)
        if child is not node
    }
    found = []
    for node in ast.walk(tree):
        if id(node) in skip or id(node) in parts:
            continue
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            found.append(node.value)
        elif isinstance(node, ast.JoinedStr):
            found.append(
                "".join(v.value if isinstance(v, ast.Constant) else "{}" for v in node.values)
            )
    return found


def raw_verbs(tree):
    """The kubectl verbs (get, replace, create, delete, patch, apply) of each list literal that holds
    `--raw`, however the rest of the argv is spelled."""
    verbs = []
    for node in ast.walk(tree):
        if isinstance(node, ast.List):
            items = [e.value if isinstance(e, ast.Constant) else None for e in node.elts]
            if "--raw" in items:
                verbs += [i for i in items if i in KUBECTL_VERBS]
    return verbs


def is_proxy_path(text):
    return "/proxy/" in text or ("/namespaces/" in text and "/proxy" in text)


def violations(*sources):
    """(has `--raw`, has a proxy path, the verbs of its --raw argv lists) over the sources."""
    found, verbs = [], []
    for source in sources:
        tree = ast.parse(source)
        found += strings(tree)
        verbs += raw_verbs(tree)
    return "--raw" in found, any(is_proxy_path(t) for t in found), verbs


def offending(files):
    """Why the skill's files violate S17 (a list of reasons), or None."""
    has_raw, has_proxy, verbs = violations(*(f.read_text() for f in files))
    reasons = []
    if has_proxy:
        reasons.append("a proxy path")
    if set(verbs) & WRITE_VERBS:
        reasons.append(f"`kubectl {'|'.join(sorted(set(verbs) & WRITE_VERBS))} --raw`")
    return reasons or None


def test_there_are_skills_to_scan():
    assert len(skill_files()) > 20


@pytest.mark.parametrize("skill", sorted(skill_files()))
def test_no_skill_reaches_a_backend_through_kubectl_raw_or_a_proxy_path(skill):
    reasons = offending(skill_files()[skill])
    assert reasons is None, (
        f"{skill} has {', '.join(reasons or [])}: read a backend with ctx.k8s_proxy_get and write to "
        "one with ctx.k8s_proxy_request, which never follow a redirect (S17)"
    )


def test_s3_check_no_longer_needs_an_exception():
    files = skill_files()["k8s-seaweedfs-health"]
    assert violations(*(f.read_text() for f in files))[:2] == (False, False)
    assert any("k8s_proxy_request" in f.read_text() for f in files)


def test_the_api_server_paths_of_k8s_base_are_allowed():
    source = 'argv = ["kubectl", "get", "--raw", "/version"]'
    assert violations(source) == (True, False, ["get"])
    files = skill_files()["k8s-base"]
    assert any('"--raw"' in f.read_text() for f in files)  # it does use --raw, for /version
    assert offending(files) is None


@pytest.mark.parametrize(
    "sources",
    [
        ['argv = ["kubectl", "get", "--raw", "/api/v1/namespaces/n/pods/p:80/proxy/x"]'],
        ['argv = ["kubectl", "get", "--raw", f"/api/v1/namespaces/{ns}/services/{s}:{p}/proxy/x"]'],
        ['argv = ["kubectl", "get", "--raw", f"/api/v1/namespaces/{ns}/pods/{p}:80/proxy{path}"]'],
        # the path is built in one module and passed to --raw in another
        ['PATH = f"/api/v1/namespaces/{ns}/pods/{p}:80" f"/proxy/v1/x"', 'a = ["get", "--raw", p]'],
    ],
)
def test_the_guard_catches_a_raw_proxy_path(sources):
    has_raw, has_proxy, _ = violations(*sources)
    assert has_raw and has_proxy


@pytest.mark.parametrize("verb", sorted(WRITE_VERBS))
def test_the_guard_catches_a_write_verb_with_raw_even_without_a_visible_path(verb):
    """Lane A's kind finding: `kubectl replace|create|delete --raw` follow a 302 as a GET carrying the
    bearer token, and `delete --raw` a 307 as a DELETE carrying it."""
    for source in (
        f'argv = ["kubectl", "{verb}", "--raw", path, "-f", "body"]',
        f'argv = ["kubectl", "-n", ns, "{verb}", "--raw", path]',
    ):
        verbs = violations(source)[2]
        assert verb in verbs and set(verbs) & WRITE_VERBS


def test_the_guard_catches_any_proxy_path_even_without_raw():
    for text in ("/api/v1/namespaces/n/pods/p:80/proxy/x", "/api/v1/namespaces/n/pods/p/proxy"):
        assert violations(f'p = "{text}"')[:2] == (False, True)
    assert violations('p = "/proxy/x"')[:2] == (False, True)


def test_docstrings_and_comments_that_mention_it_are_not_code():
    source = (
        '"""Not `kubectl replace --raw` on /namespaces/n/pods/p/proxy/ paths."""\n'
        "# --raw /namespaces/n/pods/p/proxy/\n"
        "x = 1\n"
    )
    assert violations(source) == (False, False, [])
