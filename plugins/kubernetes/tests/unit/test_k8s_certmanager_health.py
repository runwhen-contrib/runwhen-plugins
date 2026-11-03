"""Unit tests for k8s-certmanager-health's lib/ and scripts against JSON captured from
the kind-rw-plugins scenario (plugins/kubernetes/tests/kind, namespace rw-certs).

The CRDs there are synthetic and no cert-manager controller runs, so the status of
each object was written by 30-k8s-certmanager-health.sh; the acme.cert-manager.io types
are not installed, so the ACME Orders used here are small hand-written objects with the
shape the API returns. The cluster state was captured at one moment; `NOW` is a fixed
point just after it."""

import copy
import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import jsonschema
import pytest
import yaml
from k8s_certmanager_health import cert as c
from k8s_certmanager_health import k8s
from k8s_certmanager_health.errors import RwError
from runwhen_capability.rwerrors import RwCommandError, classify_command_error

SKILL = Path(__file__).parents[2] / "skills" / "k8s-certmanager-health"
DATA = Path(__file__).parent / "data" / "k8s_certmanager_health"
NOW = datetime(2026, 11, 3, 19, 50, tzinfo=timezone.utc)
SCRIPT_OF = {"certificate-status": "certificate_status", "requests": "certificate_requests"}
TASKS = {t["name"]: t for t in yaml.safe_load((SKILL / "tasks.yaml").read_text())["tasks"]}
TYPE = "kubernetes/certificate"


def load(name):
    return json.loads((DATA / f"rw-certs_{name}.json").read_text())


def named(doc, name):
    return next(i for i in doc["items"] if i["metadata"]["name"] == name)


def cert(name):
    return named(load("certificates"), name)


def request(name):
    return named(load("certificaterequests"), name)


def schema_of(task, output):
    ref = TASKS[task]["outputs"][output]["schema"]
    return json.loads((SKILL / ref).read_text())


def check_outputs(task, out):
    for name, value in out.items():
        jsonschema.validate(value, schema_of(task, name))
    assert set(out) == set(TASKS[task]["outputs"])


def order(name, request_obj, state, reason=None, authorizations=1):
    return {
        "metadata": {
            "name": name,
            "ownerReferences": [
                {
                    "kind": "CertificateRequest",
                    "name": request_obj["metadata"]["name"],
                    "uid": request_obj["metadata"]["uid"],
                }
            ],
        },
        "status": {
            "state": state,
            **({"reason": reason} if reason else {}),
            "authorizations": [{} for _ in range(authorizations)],
        },
    }


# ------------------------------------------------------------ helpers ----


def test_trunc_flags_text_cut_at_1024():
    assert c.trunc("short") == ("short", False)
    text, cut = c.trunc("y" * 3000)
    assert len(text) == 1024 and cut is True
    assert c.trunc(None) == (None, False)


def test_parse_time_accepts_the_api_format():
    assert c.parse_time("2026-11-03T19:50:00Z") == NOW
    assert c.parse_time(None) is None and c.parse_time("") is None


# --------------------------------------------------- certificate-status ----


def test_ready_certificate_reports_conditions_window_and_names():
    v = c.certificate_view(cert("ready-cert"), NOW, 14)
    assert v["ready"]["status"] == "True" and v["ready"]["reason"] == "Ready"
    assert v["issuing"] is None
    assert v["notAfter"] and v["notBefore"] and v["renewalTime"]
    assert v["expiresInDays"] > 2900 and v["expired"] is False and v["expiringSoon"] is False
    assert v["renewalTimePassed"] is False
    assert v["dnsNameCount"] == 2 and v["dnsNames"] == ["ready.rw.example", "www.ready.rw.example"]
    assert v["secretName"] == "ready-cert-tls" and v["revision"] == 1


def test_expiring_certificate_is_flagged_against_the_warning_setting():
    v = c.certificate_view(cert("expiring-cert"), NOW, 14)
    assert 4.9 < v["expiresInDays"] <= 5.01
    assert v["ready"]["status"] == "True"  # still Ready: the classic "Ready but expired" case
    assert v["expiringSoon"] is True and v["expired"] is False
    assert v["renewalTimePassed"] is True  # renewal is overdue and nothing is issuing
    assert v["issuing"] is None
    assert v["expiryWarningDays"] == 14.0
    assert c.certificate_view(cert("expiring-cert"), NOW, 3)["expiringSoon"] is False
    assert c.certificate_view(cert("expiring-cert"), NOW, 5.5)["expiringSoon"] is True


def test_expired_certificate_has_negative_days_and_is_expiring_soon_too():
    # The classic summary task reported only certificates past notAfter; here that is `expired`,
    # and the warning window also covers them.
    obj = copy.deepcopy(cert("ready-cert"))
    obj["status"]["notAfter"] = "2026-11-01T19:50:00Z"
    v = c.certificate_view(obj, NOW, 14)
    assert v["expiresInDays"] == -2.0 and v["expired"] is True and v["expiringSoon"] is True
    assert v["ready"]["status"] == "True"


def test_failed_certificate_reports_ready_false_and_issuing_with_reasons():
    v = c.certificate_view(cert("failed-cert"), NOW, 14)
    assert (v["ready"]["status"], v["ready"]["reason"]) == ("False", "Failed")
    assert (v["issuing"]["status"], v["issuing"]["reason"]) == ("True", "Failed")
    assert "will be retried" in v["issuing"]["message"]
    assert v["lastFailureTime"] and v["revision"] == 0
    assert v["expiresInDays"] is None and v["expired"] is None and v["expiringSoon"] is None


def test_certificate_without_status_is_unknown_not_healthy_or_a_crash():
    # Classic: jq aborted on a Certificate with no status.conditions and later items vanished.
    v = c.certificate_view(cert("new-cert"), NOW, 14)
    assert v["ready"] is None and v["issuing"] is None and v["conditions"] == []
    assert v["notAfter"] is None and v["expiresInDays"] is None
    assert v["expired"] is None and v["expiringSoon"] is None and v["renewalTimePassed"] is None
    assert v["dnsNameCount"] == 1


def test_certificate_text_and_lists_are_bounded():
    obj = copy.deepcopy(cert("failed-cert"))
    obj["status"]["conditions"][0]["message"] = "m" * 5000
    obj["spec"]["dnsNames"] = [f"h{i}.example" for i in range(80)]
    v = c.certificate_view(obj, NOW, 14)
    assert len(v["ready"]["message"]) == 1024 and v["ready"]["truncated"] is True
    assert v["issuing"]["truncated"] is False
    assert v["dnsNameCount"] == 80 and len(v["dnsNames"]) == 50


def test_issuer_ref_defaults_to_a_cert_manager_issuer():
    assert c.issuer_ref(cert("failed-cert")) == ("broken-issuer", "Issuer", "cert-manager.io")
    assert c.issuer_lookup(cert("failed-cert")) == ("issuers", "broken-issuer")
    assert c.issuer_lookup(cert("expiring-cert")) == ("clusterissuers", "rw-cluster-issuer")
    external = copy.deepcopy(cert("ready-cert"))
    external["spec"]["issuerRef"] = {"name": "pca", "kind": "AWSPCAIssuer", "group": "awspca.x.io"}
    assert c.issuer_lookup(external) == (None, "pca")
    v = c.issuer_view(external, None, "external")
    assert v["lookedUp"] is False and v["exists"] is None and v["ready"] is None
    assert v["scope"] is None


def test_issuer_views_ready_not_ready_missing_and_unreadable():
    ready = c.issuer_view(cert("ready-cert"), named(load("issuers"), "ca-ready"), "ok")
    assert (ready["exists"], ready["ready"], ready["scope"]) == (True, True, "namespace")
    broken = c.issuer_view(cert("failed-cert"), named(load("issuers"), "broken-issuer"), "ok")
    assert broken["exists"] is True and broken["ready"] is False
    assert broken["readyCondition"]["reason"] == "ErrGetKeyPair"
    cluster = c.issuer_view(
        cert("expiring-cert"), named(load("clusterissuers"), "rw-cluster-issuer"), "ok"
    )
    assert (cluster["ready"], cluster["scope"]) == (True, "cluster")
    gone = c.issuer_view(cert("orphan-cert"), None, "not_found")
    assert gone["exists"] is False and gone["ready"] is None and gone["lookedUp"] is True
    denied = c.issuer_view(cert("expiring-cert"), None, "denied")
    assert denied["exists"] is None and denied["ready"] is None and denied["lookedUp"] is False


def test_an_issuer_with_no_ready_condition_yet_is_unknown():
    bare = {"metadata": {"name": "x"}, "spec": {}}
    v = c.issuer_view(cert("ready-cert"), bare, "ok")
    assert v["exists"] is True and v["ready"] is None and v["readyCondition"] is None


# ------------------------------------------------------------ requests ----


def test_requests_are_owned_by_uid_and_listed_newest_revision_first():
    rows, owners = c.certificate_requests(
        load("certificaterequests")["items"], cert("failed-cert"), 200
    )
    assert [r["name"] for r in rows] == ["failed-cert-2", "failed-cert-1"]
    assert set(owners) == {"failed-cert-1", "failed-cert-2"}
    assert owners["failed-cert-2"]["revision"] == 2 and owners["failed-cert-2"]["uid"]
    one = rows[1]
    assert one["revision"] == 1 and one["failureTime"]
    assert one["approved"]["status"] == "True"
    assert (one["ready"]["status"], one["ready"]["reason"]) == ("False", "Failed")
    assert one["denied"] is None and one["invalidRequest"] is None
    two = rows[0]
    # a denied request never got an Approved condition (an undecided one has none either)
    assert two["denied"]["status"] == "True" and two["approved"] is None
    assert two["invalidRequest"]["reason"] == "RequestInvalid"
    assert two["issuerRef"] == {
        "name": "broken-issuer",
        "kind": "Issuer",
        "group": "cert-manager.io",
    }


def test_a_request_owned_by_another_certificate_is_left_out_even_with_the_same_name():
    items = copy.deepcopy(load("certificaterequests")["items"])
    stranger = copy.deepcopy(named({"items": items}, "failed-cert-1"))
    stranger["metadata"]["name"] = "stranger"
    stranger["metadata"]["ownerReferences"][0]["uid"] = "someone-elses-uid"
    items.append(stranger)
    rows, _ = c.certificate_requests(items, cert("failed-cert"), 200)
    assert "stranger" not in [r["name"] for r in rows]


def test_a_request_without_an_approved_condition_or_failure_is_still_listed():
    # Classic: jq built an object from Approved and silently dropped requests lacking it.
    items = copy.deepcopy(load("certificaterequests")["items"])
    pending = named({"items": items}, "ready-cert-1")
    pending["status"] = {}
    rows, _ = c.certificate_requests(items, cert("ready-cert"), 200)
    assert [r["name"] for r in rows] == ["ready-cert-1"]
    assert (
        rows[0]["approved"] is None and rows[0]["ready"] is None and rows[0]["failureTime"] is None
    )


def test_requests_are_capped_and_sorted_without_a_revision_last():
    items = []
    for i in range(5):
        r = copy.deepcopy(request("failed-cert-1"))
        r["metadata"]["name"] = f"r{i}"
        r["metadata"]["annotations"]["cert-manager.io/certificate-revision"] = str(i)
        items.append(r)
    nameless = copy.deepcopy(items[0])
    nameless["metadata"]["name"] = "no-revision"
    del nameless["metadata"]["annotations"]
    items.append(nameless)
    rows, owners = c.certificate_requests(items, cert("failed-cert"), 3)
    assert [r["name"] for r in rows] == ["r4", "r3", "r2"] and len(owners) == 3
    rows, _ = c.certificate_requests(items, cert("failed-cert"), 200)
    assert rows[-1]["name"] == "no-revision" and rows[-1]["revision"] is None


def test_request_messages_are_bounded():
    r = copy.deepcopy(request("failed-cert-1"))
    r["status"]["conditions"][1]["message"] = "z" * 4000
    view = c.request_view(r)
    assert len(view["ready"]["message"]) == 1024 and view["ready"]["truncated"] is True
    assert view["approved"]["truncated"] is False


def owners_of(*names):
    _rows, owners = c.certificate_requests(
        load("certificaterequests")["items"], cert("failed-cert"), 200
    )
    return {n: owners[n] for n in names}


def test_orders_are_matched_to_the_returned_requests():
    r1, r2 = request("failed-cert-1"), request("failed-cert-2")
    other = order("elsewhere", request("ready-cert-1"), "valid")
    orders = [
        order("failed-cert-1-1", r1, "errored", "acme: urn:ietf:params:acme:error:rateLimited"),
        order("failed-cert-2-1", r2, "pending", authorizations=2),
        other,
    ]
    owners = owners_of("failed-cert-1", "failed-cert-2")
    rows, uids = c.orders_for(orders, owners, 200)
    assert [(o["name"], o["request"], o["state"]) for o in rows] == [
        ("failed-cert-2-1", "failed-cert-2", "pending"),
        ("failed-cert-1-1", "failed-cert-1", "errored"),
    ]
    assert rows[1]["reason"].endswith("rateLimited") and rows[0]["authorizationCount"] == 2
    assert set(uids) == {"failed-cert-2-1", "failed-cert-1-1"}
    assert len(c.orders_for(orders, owners, 1)[0]) == 1


def test_orders_sort_by_request_revision_not_by_name():
    # As strings c-10 < c-2; the newest request (revision 11) must still come first.
    owners, orders = {}, []
    for rev in (2, 9, 10, 11):
        r = copy.deepcopy(request("failed-cert-1"))
        r["metadata"]["name"] = f"c-{rev}"
        r["metadata"]["uid"] = f"uid-{rev}"
        owners[r["metadata"]["name"]] = {
            "uid": r["metadata"]["uid"],
            "revision": rev,
            "creationTimestamp": r["metadata"]["creationTimestamp"],
        }
        orders.append(order(f"o-{rev}", r, "errored"))
    rows, _ = c.orders_for(orders, owners, 200)
    assert [o["request"] for o in rows] == ["c-11", "c-10", "c-9", "c-2"]


def challenge(name, order_obj, state, reason=None, ctype="http-01", dns="x.example"):
    ref = {
        "kind": "Order",
        "name": order_obj["metadata"]["name"],
        "uid": order_obj["metadata"]["uid"],
    }
    return {
        "metadata": {"name": name, "ownerReferences": [ref]},
        "spec": {"type": ctype, "dnsName": dns, "wildcard": False},
        "status": {
            "state": state,
            "presented": False,
            "processing": True,
            **({"reason": reason} if reason else {}),
        },
    }


def test_challenges_are_matched_to_the_returned_orders_and_report_their_reason():
    r1 = request("failed-cert-1")
    o1 = order("o-1", r1, "pending")
    o1["metadata"]["uid"] = "order-uid-1"
    _rows, uids = c.orders_for([o1], owners_of("failed-cert-1"), 200)
    assert uids == {"o-1": "order-uid-1"}
    other = order("o-2", r1, "pending")
    other["metadata"]["uid"] = "order-uid-2"
    reason = "Waiting for HTTP-01 challenge propagation: wrong status code '404'"
    items = [challenge("ch-a", o1, "pending", reason), challenge("ch-b", other, "valid")]
    rows = c.challenges_for(items, uids, 200)
    assert [(x["name"], x["order"], x["state"], x["type"], x["dnsName"]) for x in rows] == [
        ("ch-a", "o-1", "pending", "http-01", "x.example")
    ]
    assert rows[0]["reason"].startswith("Waiting for HTTP-01") and rows[0]["processing"] is True
    assert rows[0]["presented"] is False and rows[0]["truncated"] is False
    items[0]["status"]["reason"] = "r" * 3000
    cut = c.challenges_for(items, uids, 200)[0]
    assert len(cut["reason"]) == 1024 and cut["truncated"] is True


def test_san_count_adds_every_subject_alternative_name_kind():
    obj = copy.deepcopy(cert("ready-cert"))
    obj["spec"].update(
        ipAddresses=["10.0.0.1"], uris=["spiffe://a", "spiffe://b"], emailAddresses=["a@b.c"]
    )
    v = c.certificate_view(obj, NOW, 14)
    assert v["dnsNameCount"] == 2 and v["sanCount"] == 6
    assert c.certificate_view(cert("new-cert"), NOW, 14)["sanCount"] == 1


# --------------------------------------------------------------- scripts ----


class FakeCtx:
    """Replays captured JSON for kubectl calls, raising like the SDK's ctx.run on a non-zero exit.

    `fail` maps a kubectl noun to the stderr kubectl would print. `orders` / `challenges` are the
    Order / Challenge lists (None: the acme.cert-manager.io types are not installed; challenges
    defaults to an empty list whenever orders is a list).
    """

    def __init__(
        self,
        ns,
        name,
        fail=None,
        orders=None,
        challenges=None,
        max_items=200,
        warning_days=14,
        rtype=TYPE,
    ):
        self.resource = SimpleNamespace(namespace=ns, name=name, type=rtype)
        self.settings = SimpleNamespace(max_items=max_items, expiry_warning_days=warning_days)
        self.fail = fail or {}
        self.orders = orders
        self.challenges = (
            challenges if challenges is not None else ([] if orders is not None else None)
        )
        self.errors = []
        self.messages = []
        self.calls = []

    def error(self, code, message, scope=None, hint=None, details=None):
        self.errors.append({"code": code, "scope": scope})
        self.messages.append(message)

    def run(self, argv, check=True):
        self.calls.append(argv)
        words, it = [], iter(argv[1:])
        for a in it:
            if a in ("-n", "-o"):
                next(it)
            elif not a.startswith("-"):
                words.append(a)
        _verb, noun, *rest = words
        missing = None
        for key, stderr in self.fail.items():
            if key == noun:
                return self._done(argv, 1, "", stderr, check)
        if noun == "certificates.cert-manager.io":
            doc, missing = load("certificates"), rest[0]
        elif noun == "issuers.cert-manager.io":
            doc, missing = load("issuers"), rest[0]
        elif noun == "clusterissuers.cert-manager.io":
            doc, missing = load("clusterissuers"), rest[0]
        elif noun == "certificaterequests.cert-manager.io":
            doc = load("certificaterequests")
        elif noun == "orders.acme.cert-manager.io":
            if self.orders is None:
                return self._done(
                    argv, 1, "", 'error: the server doesn\'t have a resource type "orders"', check
                )
            doc = {"items": self.orders}
        elif noun == "challenges.acme.cert-manager.io":
            if self.challenges is None:
                return self._done(
                    argv,
                    1,
                    "",
                    'error: the server doesn\'t have a resource type "challenges"',
                    check,
                )
            doc = {"items": self.challenges}
        else:
            raise AssertionError(argv)
        if missing is not None:
            match = [i for i in doc["items"] if i["metadata"]["name"] == missing]
            if not match:
                err = f'Error from server (NotFound): {noun.split(".")[0]} "{missing}" not found'
                return self._done(argv, 1, "", err, check)
            doc = match[0]
        return self._done(argv, 0, json.dumps(doc), "", check)

    @staticmethod
    def _done(argv, rc, out, err, check):
        if rc and check:
            raise RwCommandError(
                classify_command_error(argv, rc, err),
                f"{argv[0]} exited {rc}: {err}",
                argv=argv,
                returncode=rc,
                stderr=err,
            )
        return SimpleNamespace(returncode=rc, stdout=out, stderr=err)


class LegacyCtx(FakeCtx):
    """A ctx.run that returns the CompletedProcess without raising (the stand-in SDK)."""

    def run(self, argv, check=True):
        return super().run(argv, check=False)


def script(name):
    spec = importlib.util.spec_from_file_location(f"cm_{name}", SKILL / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.main


FORBIDDEN = (
    'Error from server (Forbidden): %s is forbidden: User "system:serviceaccount:rw-certs:viewer" '
    "cannot get resource"
)


def test_status_script_on_a_ready_certificate():
    ctx = FakeCtx("rw-certs", "ready-cert")
    out = script("certificate_status")(ctx)
    check_outputs("certificate-status", out)
    assert out["certificate"]["ready"]["status"] == "True"
    assert out["issuer"]["exists"] is True and out["issuer"]["ready"] is True
    assert ctx.errors == []


def test_status_script_on_the_failed_certificate_shows_the_issuer_that_is_not_ready():
    ctx = FakeCtx("rw-certs", "failed-cert")
    out = script("certificate_status")(ctx)
    check_outputs("certificate-status", out)
    assert out["certificate"]["ready"]["status"] == "False"
    assert out["issuer"]["ready"] is False
    assert out["issuer"]["readyCondition"]["reason"] == "ErrGetKeyPair"


def test_status_script_reads_a_cluster_issuer_without_a_namespace_scope_problem():
    ctx = FakeCtx("rw-certs", "expiring-cert")
    out = script("certificate_status")(ctx)
    check_outputs("certificate-status", out)
    assert out["certificate"]["expiringSoon"] is True
    assert out["issuer"]["scope"] == "cluster" and out["issuer"]["ready"] is True
    assert any("clusterissuers.cert-manager.io" in call for call in ctx.calls)


def test_external_issuer_is_reported_as_not_looked_up():
    ctx = FakeCtx("rw-certs", "external-cert")
    out = script("certificate_status")(ctx)
    check_outputs("certificate-status", out)
    issuer = out["issuer"]
    assert (issuer["kind"], issuer["group"]) == ("AWSPCAIssuer", "awspca.cert-manager.aws.com")
    assert issuer["lookedUp"] is False and issuer["exists"] is None and issuer["ready"] is None
    assert ctx.errors == [] and len(ctx.calls) == 1  # only the Certificate was read
    assert out["certificate"]["sanCount"] == 2


def test_status_script_honours_the_warning_setting():
    out = script("certificate_status")(FakeCtx("rw-certs", "expiring-cert", warning_days=1))
    assert out["certificate"]["expiringSoon"] is False


def test_missing_issuer_is_a_fact_not_an_error():
    ctx = FakeCtx("rw-certs", "orphan-cert")
    out = script("certificate_status")(ctx)
    check_outputs("certificate-status", out)
    assert out["issuer"]["exists"] is False and out["issuer"]["ready"] is None
    assert ctx.errors == []


def test_certificate_with_no_status_runs_clean():
    out = script("certificate_status")(FakeCtx("rw-certs", "new-cert"))
    check_outputs("certificate-status", out)
    assert out["certificate"]["ready"] is None and out["certificate"]["expiringSoon"] is None
    assert out["issuer"]["ready"] is True


def test_forbidden_cluster_issuer_keeps_the_certificate_and_reports_the_scope():
    ctx = FakeCtx(
        "rw-certs",
        "expiring-cert",
        fail={"clusterissuers.cert-manager.io": FORBIDDEN % "clusterissuers"},
    )
    out = script("certificate_status")(ctx)
    check_outputs("certificate-status", out)
    assert out["certificate"]["expiringSoon"] is True
    assert out["issuer"]["exists"] is None and out["issuer"]["ready"] is None
    assert [(e["code"], e["scope"]) for e in ctx.errors] == [
        ("permission_denied", "clusterissuers")
    ]


def test_forbidden_issuer_read_is_reported_as_scope_issuers():
    ctx = FakeCtx("rw-certs", "ready-cert", fail={"issuers.cert-manager.io": FORBIDDEN % "issuers"})
    out = script("certificate_status")(ctx)
    assert out["issuer"]["ready"] is None and out["certificate"]["ready"]["status"] == "True"
    assert [(e["code"], e["scope"]) for e in ctx.errors] == [("permission_denied", "issuers")]


def test_forbidden_certificate_fails_instead_of_returning_empty():
    for noun in ("certificates.cert-manager.io",):
        ctx = FakeCtx("rw-certs", "ready-cert", fail={noun: FORBIDDEN % "certificates"})
        with pytest.raises(RwError) as exc:
            script("certificate_status")(ctx)
        assert exc.value.code == "permission_denied"


def test_missing_certificate_is_not_found():
    with pytest.raises(RwError) as exc:
        script("certificate_status")(FakeCtx("rw-certs", "nope"))
    assert exc.value.code == "not_found"
    with pytest.raises(RwError) as exc:
        script("certificate_requests")(FakeCtx("rw-certs", "nope"))
    assert exc.value.code == "not_found"


def test_unreachable_api_server_raises_and_is_not_swallowed_by_the_scoped_reads():
    err = "Unable to connect to the server: dial tcp 10.0.0.1:443: i/o timeout"
    for name, fail in (
        ("certificate_status", {"certificates.cert-manager.io": err}),
        ("certificate_status", {"issuers.cert-manager.io": err}),
        ("certificate_requests", {"certificaterequests.cert-manager.io": err}),
        ("certificate_requests", {"orders.acme.cert-manager.io": err}),
        ("certificate_requests", {"challenges.acme.cert-manager.io": err}),
    ):
        with pytest.raises(RwError) as exc:
            script(name)(FakeCtx("rw-certs", "ready-cert", fail=fail, orders=[]))
        assert exc.value.code == "unreachable"


def test_a_ctx_run_that_returns_nonzero_is_checked_too():
    ctx = LegacyCtx(
        "rw-certs", "ready-cert", fail={"certificates.cert-manager.io": FORBIDDEN % "x"}
    )
    with pytest.raises(RwError) as exc:
        script("certificate_status")(ctx)
    assert exc.value.code == "permission_denied"
    with pytest.raises(RwError) as exc:
        script("certificate_status")(LegacyCtx("rw-certs", "nope"))
    assert exc.value.code == "not_found"
    out = script("certificate_requests")(LegacyCtx("rw-certs", "failed-cert"))
    assert out["orders"]["applicable"] is False


def test_other_resource_types_are_refused_before_any_kubectl_call():
    for name in ("certificate_status", "certificate_requests"):
        ctx = FakeCtx("rw-certs", "x", rtype="kubernetes/deployment")
        with pytest.raises(RwError) as exc:
            script(name)(ctx)
        assert exc.value.code == "invalid_input" and ctx.calls == []


def test_type_falls_back_to_the_path_plural():
    ctx = FakeCtx("rw-certs", "ready-cert", rtype=None)
    ctx.resource.path = "kubernetes/clusters/kind/namespaces/rw-certs/certificates/ready-cert"
    assert script("certificate_status")(ctx)["certificate"]["name"] == "ready-cert"


def test_requests_script_on_the_failed_certificate():
    ctx = FakeCtx("rw-certs", "failed-cert")
    out = script("certificate_requests")(ctx)
    check_outputs("requests", out)
    assert [r["name"] for r in out["requests"]] == ["failed-cert-2", "failed-cert-1"]
    assert out["requests"][1]["ready"]["status"] == "False"
    assert out["orders"]["applicable"] is False and out["orders"]["items"] == []
    assert out["challenges"]["applicable"] is False and out["challenges"]["items"] == []
    assert ctx.errors == []  # the absent ACME types are not an error


def test_requests_script_on_the_ready_certificate_and_one_with_no_requests():
    out = script("certificate_requests")(FakeCtx("rw-certs", "ready-cert"))
    assert [r["name"] for r in out["requests"]] == ["ready-cert-1"]
    assert out["requests"][0]["ready"]["status"] == "True"
    empty = script("certificate_requests")(FakeCtx("rw-certs", "new-cert"))
    assert empty["requests"] == []


def test_requests_script_reports_orders_where_the_acme_types_exist():
    r1 = request("failed-cert-1")
    orders = [order("failed-cert-1-9", r1, "errored", "challenge failed")]
    out = script("certificate_requests")(FakeCtx("rw-certs", "failed-cert", orders=orders))
    check_outputs("requests", out)
    assert out["orders"]["applicable"] is True
    assert [(o["request"], o["state"]) for o in out["orders"]["items"]] == [
        ("failed-cert-1", "errored")
    ]
    # an installed ACME CRD with no Orders is an empty list, not "not applicable"
    none = script("certificate_requests")(FakeCtx("rw-certs", "failed-cert", orders=[]))
    assert none["orders"] == {"applicable": True, "reason": None, "items": []}


def test_requests_script_reports_the_challenges_of_the_orders():
    r1 = request("failed-cert-1")
    o1 = order("failed-cert-1-9", r1, "pending")
    o1["metadata"]["uid"] = "order-uid-9"
    stranger = order("stranger-1", r1, "pending")
    stranger["metadata"]["uid"] = "order-uid-other"
    reason = "Waiting for HTTP-01 challenge propagation: wrong status code '404'"
    chs = [challenge("ch-1", o1, "pending", reason), challenge("ch-x", stranger, "valid")]
    ctx = FakeCtx("rw-certs", "failed-cert", orders=[o1], challenges=chs)
    out = script("certificate_requests")(ctx)
    check_outputs("requests", out)
    assert out["challenges"]["applicable"] is True
    assert [(x["name"], x["order"], x["state"]) for x in out["challenges"]["items"]] == [
        ("ch-1", "failed-cert-1-9", "pending")
    ]
    assert out["challenges"]["items"][0]["reason"] == reason
    assert ctx.errors == []
    # installed ACME types with nothing pending: applicable, empty
    none = script("certificate_requests")(FakeCtx("rw-certs", "failed-cert", orders=[]))
    assert none["challenges"] == {"applicable": True, "reason": None, "items": []}


def test_forbidden_challenges_list_is_null_with_an_error_and_keeps_orders():
    ctx = FakeCtx(
        "rw-certs",
        "failed-cert",
        orders=[],
        fail={"challenges.acme.cert-manager.io": FORBIDDEN % "challenges"},
    )
    out = script("certificate_requests")(ctx)
    check_outputs("requests", out)
    assert out["challenges"] is None and out["orders"]["applicable"] is True
    assert [(e["code"], e["scope"]) for e in ctx.errors] == [("permission_denied", "challenges")]


def test_challenges_cannot_be_matched_when_orders_are_unreadable():
    ctx = FakeCtx(
        "rw-certs",
        "failed-cert",
        orders=[],
        fail={"orders.acme.cert-manager.io": FORBIDDEN % "orders"},
    )
    out = script("certificate_requests")(ctx)
    check_outputs("requests", out)
    assert out["orders"] is None and out["challenges"] is None
    assert [e["scope"] for e in ctx.errors] == ["orders"]
    assert not any("challenges.acme.cert-manager.io" in call for call in ctx.calls)


def test_permission_hints_name_the_verb_each_scope_needs():
    hints = []
    ctx = FakeCtx(
        "rw-certs",
        "expiring-cert",
        orders=[],
        fail={
            "clusterissuers.cert-manager.io": FORBIDDEN % "clusterissuers",
            "orders.acme.cert-manager.io": FORBIDDEN % "orders",
        },
    )
    ctx.error = lambda code, message, scope=None, hint=None, details=None: hints.append(
        (scope, hint)
    )
    script("certificate_status")(ctx)
    ctx.resource.name = "failed-cert"
    script("certificate_requests")(ctx)
    assert hints == [
        ("clusterissuers", "grant get on clusterissuers through a ClusterRole"),
        ("orders", "grant list on orders through a Role in the certificate's namespace"),
    ]


def test_forbidden_orders_list_is_null_with_an_error_and_keeps_the_requests():
    ctx = FakeCtx(
        "rw-certs",
        "failed-cert",
        orders=[],
        fail={"orders.acme.cert-manager.io": FORBIDDEN % "orders"},
    )
    out = script("certificate_requests")(ctx)
    check_outputs("requests", out)
    assert out["orders"] is None and len(out["requests"]) == 2
    assert [(e["code"], e["scope"]) for e in ctx.errors] == [("permission_denied", "orders")]


def test_forbidden_request_list_fails_the_task_instead_of_returning_empty():
    ctx = FakeCtx(
        "rw-certs",
        "failed-cert",
        fail={"certificaterequests.cert-manager.io": FORBIDDEN % "certificaterequests"},
    )
    with pytest.raises(RwError) as exc:
        script("certificate_requests")(ctx)
    assert exc.value.code == "permission_denied"


def test_requests_list_is_capped_by_the_setting():
    out = script("certificate_requests")(FakeCtx("rw-certs", "failed-cert", max_items=1))
    assert [r["name"] for r in out["requests"]] == ["failed-cert-2"]


def test_no_task_reads_a_secret_or_anything_but_cert_manager_objects():
    for name, targets in (
        ("certificate_status", ("ready-cert", "expiring-cert", "failed-cert")),
        ("certificate_requests", ("failed-cert", "ready-cert")),
    ):
        for target in targets:
            ctx = FakeCtx("rw-certs", target, orders=[])
            script(name)(ctx)
            for argv in ctx.calls:
                assert not any("secret" in a.lower() for a in argv), argv
                assert argv[-2:] == ["-o", "json"]


# ------------------------------------------------------------ permissions ----

# kubectl noun -> (apiGroup, plural)
RESOURCES = {
    "certificates.cert-manager.io": ("cert-manager.io", "certificates"),
    "certificaterequests.cert-manager.io": ("cert-manager.io", "certificaterequests"),
    "issuers.cert-manager.io": ("cert-manager.io", "issuers"),
    "clusterissuers.cert-manager.io": ("cert-manager.io", "clusterissuers"),
    "orders.acme.cert-manager.io": ("acme.cert-manager.io", "orders"),
    "challenges.acme.cert-manager.io": ("acme.cert-manager.io", "challenges"),
}
VALUE_FLAGS = {
    "-n",
    "--namespace",
    "-o",
    "-c",
    "--container",
    "--field-selector",
    "-l",
    "--selector",
}


def rbac_needs(argv):
    """The (apiGroup, resource, verb) triples one kubectl argv needs.

    get <kind> <name> -> get; get <kind> (no name, or -l / --field-selector) -> list;
    logs -> pods/log get; exec -> pods get + pods/exec create. Raises on anything else, so a
    skill that issues a new verb has to extend this mapping.
    """
    flags, positional, it = {}, [], iter(argv[1:])
    for a in it:
        if a == "--":
            break
        if a in VALUE_FLAGS:
            flags[a] = next(it)
        elif not a.startswith("-"):
            positional.append(a)
    verb = positional[0]
    if verb == "get":
        group, plural = RESOURCES[positional[1]]
        named_object = len(positional) > 2 and "-l" not in flags and "--selector" not in flags
        return {(group, plural, "get" if named_object else "list")}
    if verb == "logs":
        return {("", "pods/log", "get")}
    if verb == "exec":
        return {("", "pods", "get"), ("", "pods/exec", "create")}
    raise AssertionError(f"no RBAC mapping for kubectl {verb}: {argv}")


def declared(task):
    return {
        (g, r, v)
        for rule in task["permissions"]
        for g in rule["apiGroups"]
        for r in rule["resources"]
        for v in rule["verbs"]
    }


@pytest.mark.parametrize(
    "task,targets",
    [
        ("certificate-status", ["ready-cert", "expiring-cert", "failed-cert", "orphan-cert"]),
        ("requests", ["failed-cert", "ready-cert", "new-cert"]),
    ],
)
def test_declared_permissions_cover_every_kubectl_call_the_script_makes(task, targets):
    needed = set()
    for target in targets:
        for orders in (None, []):  # without and with the ACME types
            ctx = FakeCtx("rw-certs", target, orders=orders)
            script(SCRIPT_OF[task])(ctx)
            for argv in ctx.calls:
                needed |= rbac_needs(argv)
    missing = needed - declared(TASKS[task])
    assert not missing, f"{task} calls {sorted(missing)} but does not declare them"


def test_each_task_declares_only_what_it_calls():
    # certificate-status never lists requests; requests never reads issuers
    status = declared(TASKS["certificate-status"])
    requests = declared(TASKS["requests"])
    assert not {r for _g, r, _v in status} & {"certificaterequests", "orders", "challenges"}
    assert not {r for _g, r, _v in requests} & {"issuers", "clusterissuers"}


def test_rbac_needs_maps_the_verbs():
    assert rbac_needs(
        ["kubectl", "-n", "x", "get", "certificates.cert-manager.io", "c", "-o", "json"]
    ) == {("cert-manager.io", "certificates", "get")}
    assert rbac_needs(
        ["kubectl", "-n", "x", "get", "orders.acme.cert-manager.io", "-o", "json"]
    ) == {("acme.cert-manager.io", "orders", "list")}
    assert rbac_needs(["kubectl", "get", "clusterissuers.cert-manager.io", "i", "-o", "json"]) == {
        ("cert-manager.io", "clusterissuers", "get")
    }


# ------------------------------------------------------------ k8s.py ----


def test_classify_maps_stderr_to_d64_codes():
    cases = {
        (1, "Error from server (Forbidden): x"): "permission_denied",
        (1, 'Error from server (NotFound): x "y" not found'): "not_found",
        (1, "Unable to connect to the server: connection refused"): "unreachable",
        (1, "context deadline exceeded"): "timeout",
        (1, "Too Many Requests"): "rate_limited",
        (1, 'error: the server doesn\'t have a resource type "orders"'): "dependency",
        (127, ""): "dependency",
        (124, ""): "timeout",
        (1, "something odd"): "internal",
    }
    for (rc, text), code in cases.items():
        assert k8s.classify(rc, text) == code


def test_kubectl_output_that_is_not_json_is_an_internal_error():
    class Ctx:
        def run(self, argv, check=True):
            return SimpleNamespace(returncode=0, stdout="not json", stderr="")

    with pytest.raises(RwError) as exc:
        k8s.kubectl_json(Ctx(), "get", "x")
    assert exc.value.code == "internal"


def test_ctx_error_text_is_bounded():
    ctx = FakeCtx(
        "rw-certs",
        "expiring-cert",
        fail={"clusterissuers.cert-manager.io": FORBIDDEN % ("x" * 4000)},
    )
    script("certificate_status")(ctx)
    assert ctx.errors and all(len(m) <= 1100 for m in ctx.messages)
