"""The gcp skills use `gcloud` (and `curl` for the one Monitoring read), nothing else.

`gsutil` and `bq` are not in the runtime image, keep their own credential stores and are not shimmed
by `rwtask test` unless asked (`--shim`), so a skill that called one would work nowhere or, worse,
on a machine with a different login. The stub also guards both (tests/stub/bin/gsutil, bin/bq)."""

import ast
import re
from pathlib import Path

PLUGIN = Path(__file__).parents[2]
SKILLS = PLUGIN / "skills"
OTHER = re.compile(r"(?<![\w.-])(gsutil|bq)(?![\w-])")


def python_files():
    return sorted(p for p in SKILLS.glob("*/*/**/*.py") if "tests" not in p.parts) + sorted(
        SKILLS.glob("*/scripts/*.py")
    )


def literals(path):
    """The string constants of a module that are not docstrings (prose may name the tools)."""
    tree = ast.parse(path.read_text())
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            first = node.body[0] if node.body else None
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                docstrings.add(id(first.value))
    return [
        n.value
        for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docstrings
    ]


def test_there_are_skill_scripts_to_scan():
    names = {p.name for p in python_files()}
    assert {"discover.py", "bucket_size.py", "size.py", "gcloud.py", "monitoring.py"} <= names


def test_no_skill_script_or_library_module_runs_gsutil_or_bq():
    found = {
        f"{p.relative_to(PLUGIN)}: {value!r}"
        for p in python_files()
        for value in literals(p)
        if OTHER.search(value)
    }
    assert not found, found


def test_the_only_programs_the_skills_start_are_gcloud_and_curl():
    started = set()
    for path in python_files():
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            # a list literal that starts with a program name: argv = ["gcloud", ...]
            if isinstance(node, ast.List) and node.elts and isinstance(node.elts[0], ast.Constant):
                head = node.elts[0].value
                if head in (
                    "gcloud",
                    "curl",
                    "gsutil",
                    "bq",
                    "kubectl",
                    "az",
                    "aws",
                    "jq",
                    "sh",
                    "bash",
                ):
                    started.add(head)
    assert started == {"gcloud", "curl"}, started


def test_the_scanner_would_see_a_gsutil_call():
    assert OTHER.search("gsutil") and OTHER.search("gsutil ls") and OTHER.search("bq")
    assert (
        OTHER.search("bq query")
        and not OTHER.search("gcloud storage buckets")
        and not OTHER.search("bqx")
    )
    assert not OTHER.search("sub-bq-thing") and not OTHER.search("a.bq")
