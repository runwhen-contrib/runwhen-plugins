"""k8s-base/check-permissions: expansion of rules into checks, the SelfSubjectRulesReview
evaluation (wildcards, resourceNames, subresources, groups), the verdicts, and the whole task
against a fake cluster that answers `kubectl create -f <reviews>` from a small RBAC model.
Nothing here calls a cluster."""

import hashlib
import importlib.util
import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from k8s_base import access as ax
from k8s_base import permissions as perms
from k8s_base.errors import RwError
from k8s_base.k8s import classify_error
from runwhen_capability.contracts import permission_hash, validate

PLUGIN = Path(__file__).parents[2]
SKILL = PLUGIN / "skills" / "k8s-base"


def load_script():
    spec = importlib.util.spec_from_file_location(
        "check_permissions_script", SKILL / "scripts" / "check_permissions.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


SCRIPT = load_script()


def res(group, resource, verbs, names=()):
    return {"apiGroups": [group], "resources": [resource], "verbs": list(verbs),
            **({"resourceNames": list(names)} if names else {})}  # fmt: skip


def check(group="", resource="pods", verb="get", name="", sub=""):
    return (ax.RESOURCE, group, resource, sub, verb, name)


# ------------------------------------------------------------ expansion ----


def test_rule_expands_to_the_cross_product_in_order():
    rule = {"apiGroups": ["", "apps"], "resources": ["pods", "pods/log"], "verbs": ["get", "list"]}
    checks, why = ax.expand_rule(rule)
    assert why is None
    assert len(checks) == 8
    assert checks[0] == check("", "pods", "get")
    assert check("", "pods", "list", sub="log") in checks
    assert check("apps", "pods", "get", sub="log") in checks


def test_resource_names_multiply_and_no_names_asks_without_a_name():
    checks, _ = ax.expand_rule(res("", "pods", ["get"], ["a", "b"]))
    assert checks == [check(name="a"), check(name="b")]
    assert ax.expand_rule(res("", "pods", ["get"]))[0] == [check()]


def test_core_group_is_the_empty_string_and_is_accepted():
    assert ax.expand_rule(res("", "pods", ["get"]))[1] is None


def test_non_resource_rule_expands_per_url_and_verb():
    checks, _ = ax.expand_rule({"nonResourceURLs": ["/version", "/healthz"], "verbs": ["get"]})
    assert checks == [(ax.NONRESOURCE, "/version", "get"), (ax.NONRESOURCE, "/healthz", "get")]


@pytest.mark.parametrize(
    "rule",
    [
        "pods",
        {},
        {"verbs": ["get"]},
        {"apiGroups": [""], "resources": ["pods"]},
        {"apiGroups": [], "resources": ["pods"], "verbs": ["get"]},
        {"apiGroups": [""], "resources": [""], "verbs": ["get"]},
        {"apiGroups": [""], "resources": ["pods"], "verbs": [""]},
        {"apiGroups": [""], "resources": ["pods"], "verbs": ["get"], "extra": 1},
        {"nonResourceURLs": ["/x"], "verbs": ["get"], "resources": ["pods"]},
        {"nonResourceURLs": [], "verbs": ["get"]},
        {"apiGroups": [""], "resources": ["pods"], "resourceNames": [], "verbs": ["get"]},
    ],
)
def test_a_rule_that_cannot_be_evaluated_has_a_reason(rule):
    checks, why = ax.expand_rule(rule)
    assert checks is None
    assert why


def test_a_huge_expansion_is_refused_not_sampled():
    rule = {"apiGroups": [""], "resources": [f"r{i}" for i in range(300)], "verbs": ["get"]}
    checks, why = ax.expand_rule(rule)
    assert checks is None
    assert "300" in why


def test_review_item_shapes():
    cluster = ax.review_item(check("apps", "deployments", "patch", sub="scale", name="web"))
    assert cluster["kind"] == "SelfSubjectAccessReview"
    assert cluster["spec"]["resourceAttributes"] == {
        "verb": "patch",
        "group": "apps",
        "resource": "deployments",
        "subresource": "scale",
        "name": "web",
    }
    in_ns = ax.review_item(check(), "shop")["spec"]["resourceAttributes"]
    assert in_ns == {"verb": "get", "group": "", "resource": "pods", "namespace": "shop"}
    assert ax.review_item((ax.NONRESOURCE, "/version", "get"))["spec"] == {
        "nonResourceAttributes": {"path": "/version", "verb": "get"}
    }
    assert ax.rules_review_item("shop")["spec"] == {"namespace": "shop"}


def test_a_star_is_asked_literally():
    attrs = ax.review_item(check("*", "*", "*"))["spec"]["resourceAttributes"]
    assert (attrs["group"], attrs["resource"], attrs["verb"]) == ("*", "*", "*")


# ------------------------------------------------- SelfSubjectRulesReview ----


def status(*rules, incomplete=False):
    return {"resourceRules": list(rules), "nonResourceRules": [], "incomplete": incomplete}


def rr(groups, resources, verbs, names=()):
    return {
        "apiGroups": groups,
        "resources": resources,
        "verbs": verbs,
        "resourceNames": list(names),
    }


@pytest.mark.parametrize(
    ("rule", "wanted", "expected"),
    [
        # exact
        (rr([""], ["pods"], ["get"]), check(), True),
        (rr([""], ["pods"], ["get"]), check(verb="list"), False),
        (rr([""], ["pods"], ["get"]), check(resource="services"), False),
        # verbs wildcard
        (rr([""], ["pods"], ["*"]), check(verb="delete"), True),
        # group: "" is the core group, not a wildcard; "*" matches every group
        (rr([""], ["pods"], ["get"]), check(group="apps"), False),
        (rr(["apps"], ["pods"], ["get"]), check(group=""), False),
        (rr(["*"], ["pods"], ["get"]), check(group=""), True),
        (rr(["*"], ["pods"], ["get"]), check(group="apps"), True),
        # resources wildcard also covers subresources
        (rr([""], ["*"], ["get"]), check(resource="anything"), True),
        (rr([""], ["*"], ["get"]), check(resource="pods", sub="log"), True),
        # a bare resource does not cover its subresources, nor the reverse
        (rr([""], ["pods"], ["get"]), check(sub="log"), False),
        (rr([""], ["pods/log"], ["get"]), check(sub="log"), True),
        (rr([""], ["pods/log"], ["get"]), check(), False),
        (rr([""], ["pods/log"], ["get"]), check(resource="services", sub="log"), False),
        # */sub covers that subresource of any resource, not other subresources
        (
            rr(["apps"], ["*/scale"], ["patch"]),
            check("apps", "deployments", "patch", sub="scale"),
            True,
        ),
        (rr(["apps"], ["*/scale"], ["patch"]), check("apps", "deployments", "patch"), False),
        (
            rr(["apps"], ["*/scale"], ["patch"]),
            check("apps", "deployments", "patch", sub="status"),
            False,
        ),
        # pods/* is not an RBAC wildcard: it matches only a literal "pods/*"
        (rr([""], ["pods/*"], ["get"]), check(sub="log"), False),
        # resourceNames: a request without a name never matches a named rule
        (rr([""], ["pods"], ["get"], ["web-0"]), check(name="web-0"), True),
        (rr([""], ["pods"], ["get"], ["web-0"]), check(name="web-1"), False),
        (rr([""], ["pods"], ["list"], ["web-0"]), check(verb="list"), False),
        (rr([""], ["pods"], ["get"]), check(name="web-0"), True),
        # a rule with names does not satisfy the unnamed rule {pods get}
        (rr([""], ["pods"], ["get"], ["web-0"]), check(), False),
        # asking for "*" literally: only a rule with "*" covers it
        (rr([""], ["pods"], ["get"]), check("*", "*", "*"), False),
        (rr(["*"], ["*"], ["*"]), check("*", "*", "*"), True),
        (rr(["*"], ["pods"], ["*"]), check("*", "*", "*"), False),
        (rr([""], ["pods"], ["get", "list"]), check("", "*", "get"), False),
    ],
)
def test_rules_review_evaluation(rule, wanted, expected):
    assert ax.allowed_by_rules(status(rule), wanted) is expected


def test_any_one_rule_of_the_review_is_enough_and_an_empty_review_grants_nothing():
    review = status(rr([""], ["services"], ["get"]), rr([""], ["pods"], ["get"]))
    assert ax.allowed_by_rules(review, check()) is True
    assert ax.allowed_by_rules(status(), check()) is False
    assert ax.allowed_by_rules({}, check()) is False


def test_rules_that_each_cover_part_of_a_name_set_do_not_combine_into_nothing():
    review = status(rr([""], ["pods"], ["get"], ["a"]), rr([""], ["pods"], ["get"], ["b"]))
    assert ax.allowed_by_rules(review, check(name="a")) is True
    assert ax.allowed_by_rules(review, check(name="b")) is True
    assert ax.allowed_by_rules(review, check(name="c")) is False


# --------------------------------------------------------------- verdicts ----


def allowed(*checks, on=True):
    return {c: (on, "why") for c in checks}


def test_verdict_granted_needs_every_expansion():
    checks = [check(verb="get"), check(verb="list")]
    assert ax.verdict_for(checks, allowed(*checks), {}, [], True)[0] == "granted"
    mixed = {checks[0]: (True, "ok"), checks[1]: (False, "no")}
    assert ax.verdict_for(checks, mixed, {"a": {checks[1]: False}}, ["a"], True)[0] == "denied"


def test_verdict_partial_scope_is_the_namespaces_where_every_expansion_is_allowed():
    get, lst = check(verb="get"), check(verb="list")
    cluster = allowed(get, lst, on=False)
    per_ns = {
        "a": {get: True, lst: True},
        "b": {get: True, lst: False},  # half a rule is not a grant
        "c": {get: False, lst: False},
    }
    verdict, scope, evidence = ax.verdict_for([get, lst], cluster, per_ns, ["a", "b", "c"], True)
    assert (verdict, scope) == ("partial", ["a"])
    assert "1 of 3" in evidence


def test_verdict_cluster_allowed_expansions_count_in_every_namespace():
    get, lst = check(verb="get"), check(verb="list")
    cluster = {get: (True, "x"), lst: (False, "no")}
    per_ns = {"a": {lst: True}, "b": {lst: False}}
    assert ax.verdict_for([get, lst], cluster, per_ns, ["a", "b"], True)[:2] == ("partial", ["a"])


def test_verdict_denied_only_when_every_namespace_was_probed():
    c = check()
    cluster = allowed(c, on=False)
    per_ns = {"a": {c: False}}
    assert ax.verdict_for([c], cluster, per_ns, ["a"], True)[0] == "denied"
    assert ax.verdict_for([c], cluster, per_ns, ["a"], False, "cut")[0] == "unverified"
    assert ax.verdict_for([c], cluster, {"a": {c: None}}, ["a"], True)[0] == "unverified"
    assert ax.verdict_for([c], cluster, {}, ["a"], True)[0] == "unverified"
    assert ax.verdict_for([c], cluster, {}, [], False, "no list")[0] == "unverified"


def test_verdict_partial_survives_an_unprobed_remainder_but_says_so():
    c = check()
    verdict, scope, evidence = ax.verdict_for(
        [c], allowed(c, on=False), {"a": {c: True}}, ["a", "b"], False
    )
    assert (verdict, scope) == ("partial", ["a"])
    assert "incomplete" in evidence


def test_non_resource_rule_is_granted_or_denied_never_partial():
    c = (ax.NONRESOURCE, "/version", "get")
    assert ax.verdict_for([c], allowed(c), {}, ["a"], True)[0] == "granted"
    assert ax.verdict_for([c], allowed(c, on=False), {}, ["a"], False)[0] == "denied"


def test_evidence_is_bounded():
    c = check()
    long = {c: (True, "x" * 5000)}
    assert len(ax.verdict_for([c], long, {}, [], True)[2]) <= ax.EVIDENCE_LIMIT


def test_documents_reads_concatenated_json():
    text = json.dumps({"a": 1}, indent=2) + "\n" + json.dumps({"b": [1, 2]}) + "\n"
    assert perms.documents(text) == [{"a": 1}, {"b": [1, 2]}]
    assert perms.documents("") == []


# ------------------------------------------------------------ fake cluster ----


class Cluster:
    """An RBAC model behind a fake `ctx.run`. grants: [(namespace or None for cluster-wide,
    rule)]. It answers `kubectl create -f <List of reviews>` from the file in cwd, one document
    per item, as kubectl prints them."""

    def __init__(
        self,
        grants=(),
        namespaces=("a", "b", "c"),
        list_namespaces=True,
        read_uid="uid-123",
        incomplete=(),
        create_error=None,
        nonresource=("/version",),
        create_error_for=None,
        cluster_scoped=(
            "nodes",
            "namespaces",
            "persistentvolumes",
            "storageclasses.storage.k8s.io",
        ),
        api_error=None,
        eval_errors=(),
        rules_eval_errors=(),
    ):
        self.grants = list(grants)
        self.namespaces = list(namespaces)
        self.list_namespaces = list_namespaces
        self.read_uid = read_uid
        self.incomplete = set(incomplete)
        self.create_error = create_error
        self.create_error_for = create_error_for
        self.cluster_scoped = list(cluster_scoped)
        self.api_error = api_error
        self.eval_errors = set(eval_errors)  # resources whose access review has evaluationError
        self.rules_eval_errors = set(rules_eval_errors)  # namespaces whose rules review has it
        self.nonresource = set(nonresource)
        self.calls = []  # (argv, [item kinds])
        self.reviews = []

    # --- the authorizer, written independently of lib/k8s_base/access.py
    def rules_in(self, ns):
        return [r for n, r in self.grants if n is None or n == ns]

    def can(self, ns, a):
        for rule in self.rules_in(ns):
            if "*" not in rule["verbs"] and a["verb"] not in rule["verbs"]:
                continue
            if "*" not in rule["apiGroups"] and a["group"] not in rule["apiGroups"]:
                continue
            combined = a["resource"] + ("/" + a["subresource"] if a.get("subresource") else "")
            ok = any(
                x == "*" or x == combined or x == "*/" + a.get("subresource", "\0")
                for x in rule["resources"]
            )
            if not ok:
                continue
            names = rule.get("resourceNames")
            if names and a.get("name") not in names:
                continue
            return True
        return False

    def answer(self, item):
        if item["kind"] == "SelfSubjectRulesReview":
            ns = item["spec"]["namespace"]
            return {
                **item,
                "status": {
                    "resourceRules": [
                        {"resourceNames": [], **r} for r in self.rules_in(ns) if "apiGroups" in r
                    ],
                    "nonResourceRules": [],
                    "incomplete": ns in self.incomplete,
                    **({"evaluationError": "webhook down"} if ns in self.rules_eval_errors else {}),
                },
            }
        spec = item["spec"]
        if "nonResourceAttributes" in spec:
            path = spec["nonResourceAttributes"]["path"]
            ok = (
                any(
                    "nonResourceURLs" in r
                    and (path in r["nonResourceURLs"] or "*" in r["nonResourceURLs"])
                    for r in self.rules_in(None)
                )
                or path in self.nonresource
            )
            return {**item, "status": {"allowed": ok, "reason": "RBAC: x" if ok else ""}}
        attrs = spec["resourceAttributes"]
        ok = self.can(attrs.get("namespace"), attrs)
        if attrs["resource"] in self.eval_errors and not ok:
            return {**item, "status": {"allowed": False, "evaluationError": "webhook down"}}
        return {**item, "status": {"allowed": ok, "reason": "RBAC: allowed" if ok else ""}}

    # --- ctx
    def run(self, argv, cwd=None, timeout=None, check=True, **kw):
        out, err, rc = "", "", 0
        if argv[:2] == ["kubectl", "create"]:
            name = argv[argv.index("-f") + 1]
            with open(os.path.join(cwd, name)) as f:
                items = json.load(f)["items"]
            self.calls.append((argv, [i["kind"] for i in items]))
            self.reviews += items
            first = items[0]["kind"]
            if self.create_error and (self.create_error_for in (None, first)):
                rc, err = 1, self.create_error
            else:
                out = "".join(json.dumps(self.answer(i), indent=4) + "\n" for i in items)
        elif argv[:3] == ["kubectl", "get", "namespaces"] and "-o" in argv and argv[-1] == "name":
            self.calls.append((argv, []))
            if self.list_namespaces:
                out = "".join(f"namespace/{n}\n" for n in self.namespaces)
            else:
                rc, err = (
                    1,
                    'Error from server (Forbidden): namespaces is forbidden: User "x" cannot list resource "namespaces"',
                )
        elif argv[:2] == ["kubectl", "api-resources"]:
            assert argv[2:] == ["--namespaced=false", "-o", "name"]
            self.calls.append((argv, []))
            if self.api_error:
                rc, err = 1, self.api_error
            else:
                out = "".join(f"{n}\n" for n in self.cluster_scoped)
        elif argv[:4] == ["kubectl", "get", "namespaces", "kube-system"]:
            self.calls.append((argv, []))
            if self.read_uid:
                out = self.read_uid
            else:
                rc, err = (
                    1,
                    'Error from server (Forbidden): namespaces "kube-system" is forbidden: User "x" cannot get resource "namespaces"',
                )
        else:  # pragma: no cover
            raise AssertionError(f"unexpected argv {argv}")
        if rc and check:
            raise subprocess.CalledProcessError(rc, argv, out, err)
        return subprocess.CompletedProcess(argv, rc, out, err)

    def create_calls(self):
        return [kinds for argv, kinds in self.calls if argv[1] == "create"]


class FakeCtx:
    def __init__(self, cluster, **settings):
        self.cluster = cluster
        self.settings = SimpleNamespace(**{"max_reviews": 3000, "max_namespaces": 500, **settings})
        self.errors = []

    def run(self, argv, **kw):
        return self.cluster.run(argv, **kw)

    def now(self):
        return datetime(2026, 11, 4, 9, 0, 0, tzinfo=UTC)

    def error(self, code, message, scope=None, **kw):
        self.errors.append({"code": code, "scope": scope, "message": message})


def run(cluster, rules, namespaces=(), **settings):
    ctx = FakeCtx(cluster, **settings)
    out = SCRIPT.main(ctx, rules=rules, namespaces=namespaces)
    return ctx, out["access"]


def verdicts(access):
    return [r["verdict"] for r in access["rules"]]


CLUSTER_ADMIN = [(None, {"apiGroups": ["*"], "resources": ["*"], "verbs": ["*"]}),
                 (None, {"nonResourceURLs": ["*"], "verbs": ["*"]})]  # fmt: skip
RULES = [
    res("", "pods", ["get", "list"]),
    res("", "secrets", ["get"]),
    res("apps", "deployments", ["create"]),
    {"nonResourceURLs": ["/version"], "verbs": ["get"]},
    res("", "pods", ["get"], ["web-0"]),
    res("", "pods/log", ["get"]),
    res("*", "*", ["*"]),
]


# ------------------------------------------------------------ whole task ----


def test_cluster_admin_grants_everything_in_one_batched_call():
    cluster = Cluster(CLUSTER_ADMIN)
    ctx, access = run(cluster, RULES)
    assert verdicts(access) == ["granted"] * len(RULES)
    assert ctx.errors == []
    assert cluster.create_calls() == [["SelfSubjectAccessReview"] * 8]  # distinct checks, once
    assert all("scope" not in r for r in access["rules"])
    assert access["root"] == {"canonicalKey": {"name": "kubeSystemUid", "value": "uid-123"}}
    assert validate("rw.access.v1", access) == []


def test_entry_per_rule_in_input_order_with_hash_and_echo():
    cluster = Cluster(CLUSTER_ADMIN)
    rules = [RULES[1], {"bogus": True}, RULES[0], RULES[1]]  # a duplicate keeps its own entry
    _, access = run(cluster, rules)
    assert [r["rule"] for r in access["rules"]] == rules
    assert [r["hash"] for r in access["rules"]] == [permission_hash(r) for r in rules]
    assert verdicts(access) == ["granted", "unverified", "granted", "granted"]
    assert "not evaluated" in access["rules"][1]["evidence"]


def test_view_only_in_one_namespace_is_partial_denied_and_granted_as_the_contract_says():
    view = [
        ("a", res("", "pods", ["get", "list", "watch"])),
        ("a", res("", "pods/log", ["get"])),
        ("a", res("", "configmaps", ["get"])),
    ]
    cluster = Cluster(view, namespaces=["a", "b"])
    ctx, access = run(cluster, RULES, ["a"])
    pods_rw = access["rules"][0]
    assert (pods_rw["verdict"], pods_rw["scope"]) == ("partial", ["a"])
    assert access["rules"][1]["verdict"] == "denied"  # secrets
    assert access["rules"][2]["verdict"] == "denied"  # create deployments
    assert access["rules"][3]["verdict"] == "granted"  # /version via system:discovery
    assert access["rules"][4]["verdict"] == "partial"  # a rule with resourceNames; view covers it
    assert access["rules"][5]["scope"] == ["a"]
    assert access["rules"][6]["verdict"] == "denied"  # * * *
    assert validate("rw.access.v1", access) == []
    assert ctx.errors == []


def test_empty_namespaces_probe_every_namespace_the_identity_can_list():
    cluster = Cluster([("b", res("", "pods", ["get"]))], namespaces=["a", "b", "c"])
    _, access = run(cluster, [res("", "pods", ["get"])])
    assert (access["rules"][0]["verdict"], access["rules"][0]["scope"]) == ("partial", ["b"])
    # one cluster-wide batch, one rules review per namespace (batched into one call)
    assert cluster.create_calls() == [["SelfSubjectAccessReview"], ["SelfSubjectRulesReview"] * 3]


def test_named_namespaces_skip_the_namespace_list_and_probe_only_those():
    cluster = Cluster([("b", res("", "pods", ["get"]))], namespaces=["a", "b", "c"])
    _, access = run(cluster, [res("", "pods", ["get"])], ["a", "b"])
    assert access["rules"][0]["scope"] == ["b"]
    assert not any(argv[2:4] == ["namespaces", "-o"] for argv, _ in cluster.calls)
    assert cluster.create_calls()[1] == ["SelfSubjectRulesReview"] * 2


def test_calls_are_bounded_by_distinct_checks_and_namespaces_not_rules_times_namespaces():
    rules = [res("", "pods", ["get", "list"]) for _ in range(40)] + [res("", "secrets", ["get"])]
    cluster = Cluster([], namespaces=[f"ns{i}" for i in range(30)])
    run(cluster, rules)
    kinds = [k for batch in cluster.create_calls() for k in batch]
    assert kinds.count("SelfSubjectAccessReview") == 3  # 3 distinct checks, not 41 rules
    assert kinds.count("SelfSubjectRulesReview") == 30  # one per namespace
    assert len(cluster.create_calls()) == 2


def test_nothing_is_probed_per_namespace_when_every_rule_is_granted_cluster_wide():
    cluster = Cluster(CLUSTER_ADMIN)
    run(cluster, RULES)
    assert not any(argv[1:3] == ["get", "namespaces"] and "kube-system" not in argv
                   for argv, _ in cluster.calls)  # fmt: skip


def test_cluster_wide_grant_of_everything_but_one_verb_is_denied_everywhere_or_partial():
    grants = [(None, res("", "pods", ["get"])), ("a", res("", "pods", ["list"]))]
    cluster = Cluster(grants, namespaces=["a", "b"])
    _, access = run(cluster, [res("", "pods", ["get", "list"])])
    assert (access["rules"][0]["verdict"], access["rules"][0]["scope"]) == ("partial", ["a"])


def test_incomplete_rules_review_falls_back_to_access_reviews_for_the_gaps():
    # the review lists nothing in "a" (incomplete) but the authorizer would allow pods get there
    class Incomplete(Cluster):
        def answer(self, item):
            doc = super().answer(item)
            if item["kind"] == "SelfSubjectRulesReview":
                doc["status"]["resourceRules"] = []  # nothing enumerated
            return doc

    cluster = Incomplete(
        [("a", res("", "pods", ["get"]))], namespaces=["a", "b"], incomplete=["a", "b"]
    )
    _, access = run(cluster, [res("", "pods", ["get"]), res("", "secrets", ["get"])])
    assert (access["rules"][0]["verdict"], access["rules"][0]["scope"]) == ("partial", ["a"])
    assert access["rules"][1]["verdict"] == "denied"
    kinds = cluster.create_calls()
    assert kinds[0] == ["SelfSubjectAccessReview"] * 2
    assert kinds[1] == ["SelfSubjectRulesReview"] * 2
    assert kinds[2:] == [["SelfSubjectAccessReview"] * 2] * 2  # the gaps, per incomplete namespace
    namespaced = [i for i in cluster.reviews[4:] if i["kind"] == "SelfSubjectAccessReview"]
    assert {i["spec"]["resourceAttributes"]["namespace"] for i in namespaced} == {"a", "b"}


def test_a_complete_review_that_grants_nothing_is_not_second_guessed():
    cluster = Cluster([], namespaces=["a"])
    run(cluster, [res("", "pods", ["get"])])
    assert cluster.create_calls() == [["SelfSubjectAccessReview"], ["SelfSubjectRulesReview"]]


def test_identity_that_cannot_list_namespaces_gets_cluster_wide_verdicts_and_a_scoped_error():
    grants = [(None, res("", "pods", ["get"])), ("a", res("", "secrets", ["get"]))]
    cluster = Cluster(grants, list_namespaces=False)
    ctx, access = run(cluster, [res("", "pods", ["get"]), res("", "secrets", ["get"])])
    assert verdicts(access) == ["granted", "unverified"]
    assert "cannot be listed" in access["rules"][1]["evidence"]
    assert [(e["code"], e["scope"]) for e in ctx.errors] == [("permission_denied", "namespaces")]
    assert cluster.create_calls() == [["SelfSubjectAccessReview"] * 2]  # no per-namespace probing
    assert validate("rw.access.v1", access) == []


@pytest.mark.parametrize(("namespaces", "expected"), [([], "unverified"), (["a"], "partial")])
def test_forbidden_namespace_helpers_preserve_honest_verdicts_without_raw_secrets(
    namespaces, expected, capsys
):
    secret = "synthetic-namespace-helper-secret"

    class ForbiddenHelpers(Cluster):
        def run(self, argv, **kwargs):
            result = super().run(argv, **kwargs)
            if result.returncode and argv[1:3] == ["get", "namespaces"]:
                result.stderr += f" token={secret}"
            return result

    cluster = ForbiddenHelpers(
        [(None, res("", "pods", ["get"])), ("a", res("", "secrets", ["get"]))],
        list_namespaces=False,
        read_uid=None,
    )
    ctx, access = run(cluster, [res("", "pods", ["get"]), res("", "secrets", ["get"])], namespaces)
    assert verdicts(access) == ["granted", expected]
    assert "root" not in access
    if namespaces:
        assert access["rules"][1]["scope"] == ["a"]
        assert ctx.errors == []
    else:
        assert "cannot be listed" in access["rules"][1]["evidence"]
        assert [(e["code"], e["scope"]) for e in ctx.errors] == [
            ("permission_denied", "namespaces")
        ]
    assert validate("rw.access.v1", access) == []
    captured = capsys.readouterr()
    output = json.dumps({"access": access, "errors": ctx.errors}) + captured.out + captured.err
    assert secret not in output


def test_named_namespaces_work_even_if_the_identity_cannot_list_them():
    cluster = Cluster([("a", res("", "secrets", ["get"]))], list_namespaces=False)
    ctx, access = run(cluster, [res("", "secrets", ["get"])], ["a"])
    assert access["rules"][0]["scope"] == ["a"]
    assert ctx.errors == []


def test_more_namespaces_than_the_cap_probe_the_first_and_never_claim_denied():
    names = [f"n{i:02d}" for i in range(10)]
    cluster = Cluster([("n01", res("", "pods", ["get"]))], namespaces=names)
    ctx, access = run(
        cluster, [res("", "pods", ["get"]), res("", "secrets", ["get"])], max_namespaces=3
    )
    assert access["rules"][0]["verdict"] == "partial"
    assert access["rules"][0]["scope"] == ["n01"]
    assert (
        access["rules"][1]["verdict"] == "unverified"
    )  # not denied: 7 namespaces were never asked
    assert "first 3" in access["rules"][1]["evidence"]
    assert [e["scope"] for e in ctx.errors] == ["namespaces"]
    assert cluster.create_calls()[1] == ["SelfSubjectRulesReview"] * 3


def test_review_budget_leaves_later_rules_unverified_never_dropped():
    rules = [
        res("", "pods", ["get"]),
        res("", "services", ["get", "list"]),
        res("", "nodes", ["get"]),
    ]
    cluster = Cluster(CLUSTER_ADMIN)
    ctx, access = run(cluster, rules, max_reviews=2)
    # reviews are spent in order: pods get and services get; the rest are not made
    assert verdicts(access) == ["granted", "unverified", "unverified"]
    assert len(access["rules"]) == 3
    assert "maxReviews" in access["rules"][1]["evidence"]
    assert [k for b in cluster.create_calls() for k in b] == ["SelfSubjectAccessReview"] * 2
    assert [e["code"] for e in ctx.errors] == ["invalid_input"]


def test_unreadable_kube_system_leaves_root_out_and_is_not_an_error():
    cluster = Cluster(CLUSTER_ADMIN, read_uid=None)
    ctx, access = run(cluster, RULES[:1])
    assert "root" not in access
    assert ctx.errors == []


def test_kube_system_read_that_fails_otherwise_propagates():
    class Down(Cluster):
        def run(self, argv, **kw):
            if argv[:4] == ["kubectl", "get", "namespaces", "kube-system"]:
                return subprocess.CompletedProcess(
                    argv, 1, "", "Unable to connect to the server: dial tcp: i/o timeout"
                )
            return super().run(argv, **kw)

    with pytest.raises(RwError) as exc:
        run(Down(CLUSTER_ADMIN), RULES[:1])
    assert exc.value.code == "unreachable"


def test_refused_review_creation_leaves_every_rule_unverified_and_reports_it():
    err = 'Error from server (Forbidden): selfsubjectaccessreviews.authorization.k8s.io is forbidden: User "x" cannot create resource "selfsubjectaccessreviews"'
    ctx, access = run(Cluster([], create_error=err), RULES)
    assert verdicts(access) == ["unverified"] * len(RULES)
    assert [(e["code"], e["scope"]) for e in ctx.errors][0] == ("permission_denied", "reviews")
    assert validate("rw.access.v1", access) == []


def test_failed_rules_review_is_reported_and_leaves_the_rule_unverified():
    err = 'Error from server (Forbidden): selfsubjectrulesreviews is forbidden: cannot create resource "selfsubjectrulesreviews"'
    cluster = Cluster([], create_error=err, create_error_for="SelfSubjectRulesReview")
    ctx, access = run(cluster, [res("", "pods", ["get"])])
    assert verdicts(access) == ["unverified"]
    assert [(e["code"], e["scope"]) for e in ctx.errors] == [("permission_denied", "namespaces")]


def test_rules_review_failure_in_one_batch_keeps_other_batches():
    class Flaky(Cluster):
        def run(self, argv, cwd=None, **kw):
            if argv[:2] == ["kubectl", "create"]:
                with open(os.path.join(cwd, argv[argv.index("-f") + 1])) as f:
                    items = json.load(f)["items"]
                if (
                    items[0]["kind"] == "SelfSubjectRulesReview"
                    and items[0]["spec"]["namespace"] == "n00"
                ):
                    return subprocess.CompletedProcess(
                        argv, 1, "", "Error from server (Timeout): request timed out"
                    )
            return super().run(argv, cwd=cwd, **kw)

    names = [f"n{i:02d}" for i in range(60)]  # two batches of 50 and 10
    cluster = Flaky([("n55", res("", "pods", ["get"]))], namespaces=names)
    ctx, access = run(cluster, [res("", "pods", ["get"])])
    assert (access["rules"][0]["verdict"], access["rules"][0]["scope"]) == ("partial", ["n55"])
    assert "scope may be incomplete" in access["rules"][0]["evidence"]
    assert [e["code"] for e in ctx.errors] == ["timeout"]


def test_too_many_rules_or_bad_inputs_are_invalid_input():
    cluster = Cluster(CLUSTER_ADMIN)
    for rules, namespaces in (
        ([res("", "pods", ["get"])] * (ax.MAX_RULES + 1), []),
        (["pods"], []),
        ([res("", "pods", ["get"])], ["Not A Namespace"]),
        ([res("", "pods", ["get"])], [""]),
    ):
        with pytest.raises(RwError) as exc:
            run(cluster, rules, namespaces)
        assert exc.value.code == "invalid_input"
    assert cluster.calls == []  # rejected before any kubectl call


def test_exactly_the_maximum_rules_is_accepted():
    cluster = Cluster(CLUSTER_ADMIN)
    _, access = run(cluster, [res("", "pods", ["get"])] * ax.MAX_RULES)
    assert len(access["rules"]) == ax.MAX_RULES


def test_sdk_attrview_rules_are_accepted_and_echoed_as_plain_json():
    from runwhen_capability.context import AttrView

    rules = [AttrView(RULES[0]), AttrView(RULES[3])]
    _, access = run(Cluster(CLUSTER_ADMIN), rules)
    assert access["rules"][0]["rule"] == RULES[0]
    json.dumps(access)


def test_empty_rules_is_an_empty_report():
    cluster = Cluster(CLUSTER_ADMIN)
    _, access = run(cluster, [])
    assert access["rules"] == []
    assert cluster.create_calls() == []


def test_kubectl_failure_classes_map_to_the_d64_codes():
    assert classify_error("Error from server (Forbidden): x", 1) == "permission_denied"
    assert classify_error("Unable to connect to the server", 1) == "unreachable"


def test_review_files_are_written_under_rw_out(tmp_path, monkeypatch):
    monkeypatch.setenv("RW_OUT", str(tmp_path))
    cluster = Cluster(CLUSTER_ADMIN)
    run(cluster, RULES[:1])
    assert (tmp_path / "review-0.json").exists()
    assert cluster.calls[0][0][-4:] == ["-f", "review-0.json", "-o", "json"]


# --------------------------------------------- declarations and the contract ----


def task():
    doc = yaml.safe_load((SKILL / "tasks.yaml").read_text())
    return next(t for t in doc["tasks"] if t["name"] == "check-permissions")


def test_task_shape_matches_the_permission_check_contract():
    t = task()
    assert t["readOnly"] is True
    assert t["timeout"] == "300s"
    assert t["inputs"]["rules"]["type"] == "object[]"
    assert "default" not in t["inputs"]["rules"]  # required
    assert t["inputs"]["namespaces"] == {
        "type": "string[]",
        "default": [],
        "description": t["inputs"]["namespaces"]["description"],
    }
    assert t["outputs"]["access"]["kind"] == "rw.access.v1"


def test_integration_names_the_task_as_its_permission_check():
    doc = yaml.safe_load((PLUGIN / "integration" / "integration.yaml").read_text())
    assert doc["entryPoints"]["permissionCheck"] == {"task": "k8s-base/check-permissions"}
    assert doc["entryPoints"]["cli"] == {"task": "k8s-base/kubectl"}


def test_skill_role_is_cli_permission_check_and_discovery():
    text = (SKILL / "SKILL.md").read_text()
    front = yaml.safe_load(text.split("---")[1])
    assert front["runwhen"]["role"] == ["cli", "permission-check", "discovery"]


def needs(argv, items):
    """The RBAC rules a kubectl argv needs (the helper the other skills' tests use)."""
    if argv[1] == "create":
        return {("authorization.k8s.io", f"{k.lower()}s", "create") for k in items}
    if argv[1] == "api-resources":
        return {("nonResource", "/api", "get"), ("nonResource", "/apis", "get")}
    if argv[1:3] == ["get", "namespaces"]:
        return {("", "namespaces", "get" if len(argv) > 4 and argv[3] == "kube-system" else "list")}
    raise AssertionError(argv)


def test_declared_permissions_cover_required_probes_without_optional_namespace_reads():
    cluster = Cluster([("a", res("", "pods", ["get"]))], incomplete=["a"])
    run(cluster, [res("", "pods", ["get"])], [])
    needed = set()
    for argv, kinds in cluster.calls:
        needed |= needs(argv, kinds)
    declared = {
        (g, r, v)
        for rule in task()["permissions"]
        for g in rule.get("apiGroups", [])
        for r in rule.get("resources", [])
        for v in rule["verbs"]
    } | {
        ("nonResource", url, v)
        for rule in task()["permissions"]
        for url in rule.get("nonResourceURLs", [])
        for v in rule["verbs"]
    }
    optional = {("", "namespaces", "get"), ("", "namespaces", "list")}
    assert needed - optional == declared
    assert not declared & optional
    assert len(task()["permissions"]) == 2


def test_compiled_checker_prerequisites_are_minimal_and_discovery_permissions_unchanged():
    from runwhen_capability.plugin.compile import compile_tree
    from runwhen_capability.plugin.tree import load_dir

    compiled = compile_tree(load_dir(PLUGIN))
    tasks = {t["name"]: t for t in compiled["tasks"] if t["skill"] == "k8s-base"}
    expected = [
        res("authorization.k8s.io", "selfsubjectaccessreviews", ["create"]),
    ]
    expected[0]["resources"].append("selfsubjectrulesreviews")
    expected.append({"nonResourceURLs": ["/api", "/apis"], "verbs": ["get"]})
    assert tasks["check-permissions"]["permissions"] == [
        {"hash": permission_hash(rule), "rule": rule} for rule in expected
    ]
    discovery = [entry["rule"] for entry in tasks["discover"]["permissions"]]
    assert len(discovery) == 25
    # The complete discovery prerequisite set from the M4 0.4.0 package.
    assert hashlib.sha256(json.dumps(discovery, sort_keys=True).encode()).hexdigest() == (
        "70866c205d414e3202e0c09e81b08d07fb4b7c1f19c0ed7996eeb8ff063d8ca5"
    )


def test_declared_permissions_validate_against_the_rbac_rule_schema():
    from jsonschema import Draft202012Validator

    schema = json.loads((PLUGIN / "integration" / "schemas" / "rbac-rule.json").read_text())
    validator = Draft202012Validator(schema)
    for rule in task()["permissions"]:
        assert list(validator.iter_errors(rule)) == []


def test_the_rules_input_schema_is_permissive_so_a_bad_rule_reaches_the_script():
    item_schema = task()["inputs"]["rules"]["schema"]
    assert item_schema == {"type": "object", "minProperties": 1}


# ------------------------------------------- review fixes: cluster-scoped, errors ----

VIEW_A = [
    ("a", res("", "pods", ["get", "list"])),
    ("a", res("", "namespaces", ["list"])),  # a RoleBinding; the API server answers yes in "a"
    ("a", res("", "nodes", ["get", "list"])),
]


@pytest.mark.parametrize(
    "rule",
    [
        res("", "namespaces", ["list"]),
        res("", "nodes", ["get", "list"]),
        res("", "nodes/proxy", ["get"]),  # a subresource inherits its base's scope
        res("storage.k8s.io", "storageclasses", ["get"]),
    ],
)
def test_cluster_scoped_resource_is_never_partial_from_a_namespaced_grant(rule):
    cluster = Cluster(VIEW_A + [("a", res("storage.k8s.io", "storageclasses", ["get"]))])
    cluster.grants.append(("a", res("", "nodes/proxy", ["get"])))
    ctx, access = run(cluster, [rule], ["a"])
    assert access["rules"][0]["verdict"] == "denied"
    assert "scope" not in access["rules"][0]
    assert ctx.errors == []
    # nothing is probed per namespace and the namespaces are not even listed
    assert [k for b in cluster.create_calls() for k in b] == ["SelfSubjectAccessReview"] * len(
        ax.expand_rule(rule)[0]
    )


def test_cluster_scoped_rules_are_decided_even_when_namespaces_cannot_be_listed():
    cluster = Cluster(
        [(None, res("", "nodes", ["get"]))], list_namespaces=False
    )  # granted cluster-wide
    ctx, access = run(
        cluster,
        [res("", "nodes", ["get"]), res("", "nodes", ["list"]), res("", "namespaces", ["list"])],
    )
    assert verdicts(access) == ["granted", "denied", "denied"]
    assert ctx.errors == []  # nothing needed the namespace list
    assert not any(argv[1:4] == ["get", "namespaces", "-o"] for argv, _ in cluster.calls)


def test_a_cluster_scoped_refusal_denies_a_mixed_rule_without_probing():
    rule = {"apiGroups": [""], "resources": ["pods", "nodes"], "verbs": ["list"]}
    cluster = Cluster([("a", res("", "pods", ["list"])), ("a", res("", "nodes", ["list"]))])
    ctx, access = run(cluster, [rule], ["a"])
    assert access["rules"][0]["verdict"] == "denied"
    assert "nodes" in access["rules"][0]["evidence"]
    assert [k for b in cluster.create_calls() for k in b] == ["SelfSubjectAccessReview"] * 2


def test_namespaced_kinds_still_get_partial():
    cluster = Cluster([("a", res("", "pods", ["list"]))])
    _, access = run(cluster, [res("", "pods", ["list"])], ["a"])
    assert (access["rules"][0]["verdict"], access["rules"][0]["scope"]) == ("partial", ["a"])


def test_discovery_decides_kinds_the_static_table_does_not_know():
    crd = res("cert-manager.io", "clusterissuers", ["get"])
    grants = [("a", crd)]
    ok = Cluster(grants, cluster_scoped=["clusterissuers.cert-manager.io"])
    assert run(ok, [crd], ["a"])[1]["rules"][0]["verdict"] == "denied"
    namespaced = Cluster(grants, cluster_scoped=["nodes"])
    assert run(namespaced, [crd], ["a"])[1]["rules"][0]["verdict"] == "partial"
    assert any(argv[1] == "api-resources" for argv, _ in ok.calls)


def test_api_resources_is_asked_once_and_only_when_something_is_refused():
    cluster = Cluster(CLUSTER_ADMIN)
    run(cluster, RULES)
    assert not any(argv[1] == "api-resources" for argv, _ in cluster.calls)
    cluster = Cluster([])
    run(cluster, [res("", "pods", ["get"]), res("", "nodes", ["get"])], ["a"])
    assert [argv[1] for argv, _ in cluster.calls].count("api-resources") == 1


def test_unknown_scope_never_becomes_partial_and_the_failure_is_reported():
    widget = res("x.io", "widgets", ["get"])
    cluster = Cluster([("a", widget)], api_error="Error from server (Timeout): request timed out")
    ctx, access = run(cluster, [widget], ["a"])
    assert access["rules"][0]["verdict"] == "unverified"
    assert "cluster-scoped" in access["rules"][0]["evidence"]
    assert [(e["code"], e["scope"]) for e in ctx.errors] == [("timeout", "discovery")]
    # a kind nobody can see in either source, allowed nowhere, is simply denied
    _, access = run(Cluster([], api_error="Error from server (Timeout): x"), [widget], ["a"])
    assert access["rules"][0]["verdict"] == "denied"


def test_static_table_answers_when_api_resources_fails_and_no_error_is_needed():
    cluster = Cluster(
        [("a", res("", "nodes", ["get"]))], api_error="Error from server (Timeout): x"
    )
    ctx, access = run(cluster, [res("", "nodes", ["get"])], ["a"])
    assert access["rules"][0]["verdict"] == "denied"
    assert ctx.errors == []


def test_get_of_a_named_namespace_inside_the_probed_namespaces_keeps_the_namespaced_path():
    rule = res("", "namespaces", ["get"], ["a"])
    cluster = Cluster([("a", rule)])
    _, access = run(cluster, [rule], ["a", "b"])
    assert (access["rules"][0]["verdict"], access["rules"][0]["scope"]) == ("partial", ["a"])
    # the name must be one of the probed namespaces, else it is cluster-scoped and denied
    _, access = run(Cluster([("a", rule)]), [rule], ["b"])
    assert access["rules"][0]["verdict"] == "denied"
    # list, or an unnamed get, is never helped by the exception
    other = res("", "namespaces", ["list"])
    assert run(Cluster([("a", other)]), [other], ["a"])[1]["rules"][0]["verdict"] == "denied"


def test_named_namespace_get_lists_namespaces_when_none_are_given():
    rule = res("", "namespaces", ["get"], ["kube-system"])
    cluster = Cluster([("kube-system", rule)], namespaces=["a", "kube-system"])
    _, access = run(cluster, [rule])
    assert (access["rules"][0]["verdict"], access["rules"][0]["scope"]) == (
        "partial",
        ["kube-system"],
    )


def test_access_review_evaluation_error_is_unverified_not_denied():
    cluster = Cluster([], eval_errors=["pods"])
    _, access = run(cluster, [res("", "pods", ["get"]), res("", "secrets", ["get"])], ["a"])
    assert access["rules"][0]["verdict"] == "unverified"
    assert "evaluate" in access["rules"][0]["evidence"]
    assert access["rules"][1]["verdict"] == "denied"


def test_evaluation_error_on_a_cluster_scoped_check_and_on_a_non_resource_url():
    cluster = Cluster([], eval_errors=["nodes"])
    _, access = run(cluster, [res("", "nodes", ["get"])], ["a"])
    assert access["rules"][0]["verdict"] == "unverified"

    class NonResource(Cluster):
        def answer(self, item):
            doc = super().answer(item)
            if "nonResourceAttributes" in item["spec"]:
                doc["status"] = {"allowed": False, "evaluationError": "webhook down"}
            return doc

    _, access = run(
        NonResource([], nonresource=()), [{"nonResourceURLs": ["/x"], "verbs": ["get"]}]
    )
    assert access["rules"][0]["verdict"] == "unverified"


def test_an_allowed_review_stands_even_with_an_evaluation_error():
    assert perms._answer({"allowed": True, "evaluationError": "x", "reason": "r"}, "") == (
        True,
        "r",
    )
    assert perms._answer({"allowed": False, "evaluationError": "x"}, "")[0] is None
    assert perms._answer({"allowed": False}, "")[0] is False
    assert perms._answer(None, "budget") == (None, "budget")


def test_rules_review_evaluation_error_is_treated_like_incomplete():
    class Empty(Cluster):
        def answer(self, item):
            doc = super().answer(item)
            if item["kind"] == "SelfSubjectRulesReview":
                doc["status"]["resourceRules"] = []
            return doc

    cluster = Empty([("a", res("", "pods", ["get"]))], rules_eval_errors=["a"])
    _, access = run(cluster, [res("", "pods", ["get"])], ["a"])
    assert (access["rules"][0]["verdict"], access["rules"][0]["scope"]) == ("partial", ["a"])
    assert cluster.create_calls()[-1] == ["SelfSubjectAccessReview"]  # the gap, asked directly


def test_schema_invalid_rules_are_unverified_entries_not_a_failed_run():
    bad = [
        {"apiGroups": "", "resources": ["pods"], "verbs": ["get"]},
        {"verbs": ["get"], "unknownField": 1},
        {"apiGroups": [""], "resources": ["pods"], "verbs": [{"a": 1}]},
        {"apiGroups": [""], "resources": ["pods"], "verbs": []},
    ]
    rules = [bad[0], res("", "pods", ["get"]), *bad[1:]]
    _, access = run(Cluster(CLUSTER_ADMIN), rules)
    assert verdicts(access) == ["unverified", "granted", "unverified", "unverified", "unverified"]
    assert [r["rule"] for r in access["rules"]] == rules
    assert validate("rw.access.v1", access) == []


def test_non_objects_and_empty_objects_cannot_be_echoed_and_are_invalid_input():
    for rules in (["pods"], [{}], [None]):
        with pytest.raises(RwError) as exc:
            run(Cluster(CLUSTER_ADMIN), rules)
        assert exc.value.code == "invalid_input"


def test_one_failed_review_batch_leaves_only_its_rules_unverified():
    class FirstBatchFails(Cluster):
        def run(self, argv, cwd=None, **kw):
            if argv[:2] == ["kubectl", "create"] and argv[4] == "review-0.json":
                self.calls.append((argv, ["failed"]))
                return subprocess.CompletedProcess(
                    argv, 1, "", "Error from server (Timeout): request timed out"
                )
            return super().run(argv, cwd=cwd, **kw)

    # 150 distinct checks: the first 100 land in the failing batch
    rules = [res("", f"kind{i}", ["get"]) for i in range(150)]
    ctx, access = run(FirstBatchFails(CLUSTER_ADMIN), rules)
    got = verdicts(access)
    assert got[:100] == ["unverified"] * 100
    assert got[100:] == ["granted"] * 50
    assert "failed" in access["rules"][0]["evidence"]
    assert [(e["code"], e["scope"]) for e in ctx.errors] == [("timeout", "reviews")]


def test_reviewer_stops_starting_reviews_after_its_deadline_and_the_run_fits_240s():
    ticks = iter([0, 0, 500, 500, 500])
    ctx = FakeCtx(Cluster(CLUSTER_ADMIN))
    items = [ax.review_item(check(resource=f"r{i}")) for i in range(150)]
    reviewer = perms.Reviewer(ctx, ".", 3000, deadline=100, clock=lambda: next(ticks))
    # (the second batch starts after the deadline)
    import tempfile

    with tempfile.TemporaryDirectory() as scratch:
        reviewer.scratch = scratch
        answers = reviewer.ask(items, "reviews")
    assert [s is None for s, _ in answers] == [False] * 100 + [True] * 50
    assert [e["code"] for e in ctx.errors] == ["timeout"]
    assert perms.REVIEW_DEADLINE + 3 * perms.SIDE_TIMEOUT <= 240


def test_scope_is_capped_at_500_namespaces_and_says_so():
    names = [f"n{i:03d}" for i in range(600)]
    cluster = Cluster([(n, res("", "pods", ["get"])) for n in names], namespaces=names)
    _, access = run(cluster, [res("", "pods", ["get"])], max_namespaces=1000)
    entry = access["rules"][0]
    assert len(entry["scope"]) == ax.MAX_SCOPE
    assert entry["scope"] == names[: ax.MAX_SCOPE]
    assert "first 500 of 600" in entry["evidence"]
    assert validate("rw.access.v1", access) == []
