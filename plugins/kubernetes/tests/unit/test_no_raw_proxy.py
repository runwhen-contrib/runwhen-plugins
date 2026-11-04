"""Repo guard for security finding S17: no skill reads a backend through `kubectl get --raw` on a
pod or Service proxy path.

`kubectl get --raw /api/v1/namespaces/<ns>/{pods,services}/<name>:<port>/proxy/...` follows a
backend's 301/302/307 and, for an absolute Location on another host, re-sends the runner's bearer
token there. Skills read a proxied backend with `ctx.k8s_proxy_get`, which never follows a redirect.

The check reads the string constants of every skill's `lib/` and `scripts/` Python, as one set per
skill (a skill builds the path in one module and passes it to `--raw` in another); docstrings and
comments are not code and are skipped, and an f-string counts as one string. A skill that has
`--raw` and a proxy path (`/namespaces/` ... `/proxy`) among them is a violation. `--raw /version`-
style paths of the API server itself (k8s-base) have no proxy path and pass. One exception is
listed, because the helper only reads: k8s-seaweedfs-health's s3-check PUTs and DELETEs through
`kubectl replace|delete --raw`; the guard holds that skill to those two verbs.
"""

import ast
from pathlib import Path

import pytest

PLUGIN = Path(__file__).parents[2]
SKILLS = PLUGIN / "skills"
# skill -> the kubectl verbs it may pass with `--raw` and a proxy path (writes: the helper reads only)
WRITE_ONLY = {"k8s-seaweedfs-health": {"replace", "delete"}}


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
    """The verb (the constant after `kubectl`) of each list literal that holds `--raw`."""
    verbs = []
    for node in ast.walk(tree):
        if isinstance(node, ast.List):
            items = [e.value if isinstance(e, ast.Constant) else None for e in node.elts]
            if "--raw" in items:
                verbs.append(items[1] if len(items) > 1 and items[0] == "kubectl" else None)
    return verbs


def is_proxy_path(text):
    return "/namespaces/" in text and "/proxy" in text


def violations(*sources):
    """(has `--raw`, has a proxy path, the verbs of its --raw argv lists) over the sources."""
    found, verbs = [], []
    for source in sources:
        tree = ast.parse(source)
        found += strings(tree)
        verbs += raw_verbs(tree)
    return "--raw" in found, any(is_proxy_path(t) for t in found), verbs


def offending(skill, files):
    has_raw, has_proxy, verbs = violations(*(f.read_text() for f in files))
    if not (has_raw and has_proxy):
        return None
    if skill in WRITE_ONLY and verbs and set(verbs) <= WRITE_ONLY[skill]:
        return None
    return verbs


def test_there_are_skills_to_scan():
    assert len(skill_files()) > 20


@pytest.mark.parametrize("skill", sorted(skill_files()))
def test_no_skill_reads_a_backend_through_kubectl_get_raw_on_a_proxy_path(skill):
    verbs = offending(skill, skill_files()[skill])
    assert verbs is None, (
        f"{skill} passes `--raw` with a proxy path (verbs {verbs}): read the backend with "
        "ctx.k8s_proxy_get, which never follows a redirect (S17)"
    )


def test_the_api_server_paths_of_k8s_base_are_allowed():
    source = 'argv = ["kubectl", "get", "--raw", "/version"]'
    assert violations(source) == (True, False, ["get"])
    files = skill_files()["k8s-base"]
    assert any('"--raw"' in f.read_text() for f in files)  # it does use --raw, for /version
    assert offending("k8s-base", files) is None


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


def test_docstrings_and_comments_that_mention_it_are_not_code():
    source = (
        '"""Not `kubectl get --raw` on /namespaces/n/pods/p/proxy/ paths."""\n'
        "# --raw /namespaces/n/pods/p/proxy/\n"
        "x = 1\n"
    )
    assert violations(source)[:2] == (False, False)


def test_the_write_exception_is_still_needed_and_is_only_replace_and_delete():
    for skill, allowed in WRITE_ONLY.items():
        has_raw, has_proxy, verbs = violations(*(f.read_text() for f in skill_files()[skill]))
        assert has_raw and has_proxy, f"{skill} no longer needs its exception: remove it"
        assert verbs and set(verbs) <= allowed, verbs
        assert "get" not in verbs
