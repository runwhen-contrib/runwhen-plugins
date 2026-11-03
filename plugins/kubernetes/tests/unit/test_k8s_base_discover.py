"""k8s-base/discover: the type table, the sanitiser, the enumeration against a fake cluster
(everything served, forbidden lists, narrowed scope, unserved APIs), the permission mapping, the
sync client, and every recorded fixture replayed and validated against rw.discovery.v1.
Nothing here calls a cluster."""

import importlib.util
import json
import urllib.error
from pathlib import Path

import pytest
import yaml
from k8s_base import discovery, sync
from k8s_base import discovery_doc as dd
from k8s_base.discovery_types import TYPES
from runwhen_capability.contracts import validate
from runwhen_capability.testing import make_context, run_main

PLUGIN = Path(__file__).parents[2]
SKILL = PLUGIN / "skills" / "k8s-base"
RESOURCE = {
    "path": "kubernetes/clusters/kind",
    "cluster": "kind",
    "type": "kubernetes/cluster",
    "name": "kind",
}
UID = "30be408d-624f-4dc1-90bb-598388f92de6"
SETTINGS = {"namespaces": [], "maxItems": 2000, "maxStringBytes": 4096}


def load_script():
    spec = importlib.util.spec_from_file_location(
        "discover_script", SKILL / "scripts" / "discover.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


SCRIPT = load_script()
TASK = next(
    t
    for t in yaml.safe_load((SKILL / "tasks.yaml").read_text())["tasks"]
    if t["name"] == "discover"
)
BY_TYPE = {t[0]: t for t in TYPES}


# ------------------------------------------------------------ a fake cluster ----


def api_table(served):
    rows = []
    for name, _parent, plural, group, kind in served:
        namespaced = "true" if _parent == "namespace" else "false"
        version = f"{group}/v1" if group else "v1"
        rows.append(f"{plural:<40}{'':<12}{version:<34}{namespaced:<13}{kind:<34}get,list")
    head = f"{'NAME':<40}{'SHORTNAMES':<12}{'APIVERSION':<34}{'NAMESPACED':<13}{'KIND':<34}VERBS"
    return "\n".join([head, *rows]) + "\n"


def obj(name, namespace=None, **extra):
    meta = {"name": name, "uid": f"uid-{name}", "resourceVersion": "9", "managedFields": [{"x": 1}]}
    if namespace:
        meta["namespace"] = namespace
    meta.update(extra.pop("metadata", {}))
    return {"metadata": meta, **extra}


FORBIDDEN = (
    'Error from server (Forbidden): {arg} is forbidden: User "u" cannot list resource "{arg}"'
)


class Cluster:
    """Answers the kubectl calls discover makes; records them in `calls`."""

    def __init__(
        self, namespaces=("a", "b"), objects=None, drop=(), forbidden=(), kube_system=True
    ):
        self.namespaces = list(namespaces)
        self.objects = objects or {}
        self.served = [t for t in TYPES if t[0] not in drop]
        self.forbidden = set(forbidden)
        self.kube_system = kube_system
        self.calls = []

    def __call__(self, argv, check=False, **_):
        self.calls.append(argv)
        rc, out, err = self.answer(argv[1:])
        return rc, out, err

    def answer(self, a):
        if a == ["get", "--raw", "/version"]:
            return (
                0,
                json.dumps(
                    {
                        "gitVersion": "v1.31.2",
                        "major": "1",
                        "minor": "31",
                        "platform": "linux/arm64",
                    }
                ),
                "",
            )
        if a[:2] == ["config", "view"]:
            return 0, "https://user:pw@127.0.0.1:6443", ""
        if a[:4] == ["get", "namespaces", "kube-system", "-o"]:
            if not self.kube_system:
                return (
                    1,
                    "",
                    'Error from server (Forbidden): namespaces "kube-system" is forbidden: cannot get',
                )
            return 0, UID, ""
        if a[0] == "api-resources":
            return 0, api_table(self.served), ""
        if a[:2] == ["get", "namespaces"]:
            if "namespaces" in self.forbidden:
                return 1, "", FORBIDDEN.format(arg="namespaces")
            if a[2] != "-o":  # get namespaces NAME -o json
                if a[2] in self.forbidden:
                    return (
                        1,
                        "",
                        f'Error from server (Forbidden): namespaces "{a[2]}" is forbidden: cannot get',
                    )
                if a[2] not in self.namespaces:
                    return 1, "", f'Error from server (NotFound): namespaces "{a[2]}" not found'
                return 0, json.dumps(obj(a[2])), ""
            return 0, json.dumps({"items": [obj(n) for n in self.namespaces]}), ""
        assert a[0] == "get" and a[-2:] == ["-o", "json"], a
        arg = a[1]
        type_name = next(t[0] for t in TYPES if discovery.resource_arg(t[2], t[3]) == arg)
        assert type_name not in ("secret",), "secrets must never be listed"
        if type_name in self.forbidden or arg in self.forbidden:
            return 1, "", FORBIDDEN.format(arg=arg)
        items = self.objects.get(type_name, [])
        if "--namespace" in a:
            ns = a[a.index("--namespace") + 1]
            items = [o for o in items if o["metadata"].get("namespace") == ns]
        return 0, json.dumps({"items": items}), ""


def run(cluster, settings=None, resource=RESOURCE):
    tc = make_context(resource=resource, settings={**SETTINGS, **(settings or {})})
    real = tc.ctx

    def fake(argv, cwd=None, timeout=None, env=None, *, inherit_env=True, check=None):
        rc, out, err = cluster(argv)
        return real._finish_run(list(argv), rc, out, err, check)

    real.run = fake
    env = run_main(SCRIPT.main, tc)
    artifact = tc.out_dir / "sync"
    doc = json.loads(artifact.read_text()) if artifact.exists() else None
    errors = tc.errors
    tc.close()
    return env, doc, errors


def parts(doc, type_name):
    return [p for p in doc["commit"]["partitions"] if p["type"] == type_name]


def items_of(doc, type_name):
    return [
        i
        for b in doc["batches"]
        for i in b["items"]
        if i["identity"]["chain"][-1]["type"] == type_name
    ]


def basic():
    return Cluster(
        objects={
            "deployment": [obj("web", "a"), obj("api", "b")],
            "node": [obj("n1")],
            "customresourcedefinition": [obj("c1.example.com")],
        },
        drop=("certificate", "nodepool", "nodeclaim"),
    )


# ------------------------------------------------------------ the type table ----


def test_the_table_is_types_yaml_without_ephemeral_types_and_the_root():
    types = yaml.safe_load((PLUGIN / "integration" / "types.yaml").read_text())["types"]
    expected = [
        (n, s["parent"], s["plural"], s["native"]["group"], s["native"]["kind"])
        for n, s in types.items()
        if not s.get("root") and not s.get("ephemeral")
    ]
    assert list(TYPES) == expected


def test_every_type_has_a_parent_discover_understands():
    assert {t[1] for t in TYPES} == {"cluster", "namespace"}


def test_parse_served_reads_scope_and_group_by_column():
    served = discovery.parse_served(api_table([BY_TYPE["deployment"], BY_TYPE["node"]]))
    assert served[("apps", "Deployment")] == ("deployments", True)
    assert served[("", "Node")] == ("nodes", False)


def test_parse_served_rejects_columns_it_does_not_know():
    with pytest.raises(Exception) as exc:
        discovery.parse_served("FOO BAR\nx y\n")
    assert exc.value.code == "internal"


# ------------------------------------------------------------ the enumeration ----


def test_whole_cluster_run_builds_a_valid_sync():
    env, doc, errors = run(basic())
    assert env["status"] == "ok" and errors == []
    assert validate("rw.discovery.v1", doc) == []
    assert validate("rw.discovery.summary.v1", env["outputs"]["summary"]) == []
    chains = [
        [c["type"] for c in i["identity"]["chain"]] for b in doc["batches"] for i in b["items"]
    ]
    assert chains[0] == ["cluster"]
    assert chains[1:3] == [["cluster", "namespace"]] * 2
    summary = env["outputs"]["summary"]
    assert summary["root"] == {"canonicalKey": {"name": "kubeSystemUid", "value": UID}}
    assert summary["counts"]["deployment"] == 2 and summary["counts"]["namespace"] == 2
    assert doc["commit"]["summary"] == summary
    assert doc["open"] == {
        "source": "k8s-base/discover",
        "scopePath": "kubernetes/clusters/kind",
        "mode": "full",
    }


def test_cluster_item_carries_the_server_without_credentials_and_the_version():
    _, doc, _ = run(basic())
    cluster = doc["batches"][0]["items"][0]
    assert cluster["identity"]["chain"] == [{"type": "cluster", "name": "kind"}]
    assert cluster["document"]["server"] == "https://127.0.0.1:6443"
    assert cluster["document"]["version"]["gitVersion"] == "v1.31.2"
    assert cluster["providerUid"] == UID


def test_a_namespaced_type_has_one_complete_partition_per_namespace():
    _, doc, _ = run(basic())
    ns = {p["parentPath"]: p for p in parts(doc, "deployment")}
    assert ns["kubernetes/clusters/kind/namespaces/a"] == {
        "type": "deployment",
        "status": "complete",
        "parentPath": "kubernetes/clusters/kind/namespaces/a",
        "count": 1,
    }
    assert set(ns) == {
        "kubernetes/clusters/kind/namespaces/a",
        "kubernetes/clusters/kind/namespaces/b",
    }
    assert parts(doc, "node") == [
        {"type": "node", "status": "complete", "parentPath": "kubernetes/clusters/kind", "count": 1}
    ]
    deploy = items_of(doc, "deployment")[0]
    assert [c["type"] for c in deploy["identity"]["chain"]] == [
        "cluster",
        "namespace",
        "deployment",
    ]


def test_an_unserved_api_is_excluded_not_an_error():
    env, doc, errors = run(basic())
    assert env["status"] == "ok" and errors == []
    cert = parts(doc, "certificate")
    assert cert == [
        {"type": "certificate", "status": "excluded", "reason": "API not served by this cluster"}
    ]
    assert parts(doc, "nodepool")[0]["status"] == "excluded"


def test_secrets_are_excluded_and_ephemeral_types_are_never_listed():
    cluster = basic()
    env, doc, _ = run(cluster)
    assert parts(doc, "secret")[0]["status"] == "excluded"
    listed = {" ".join(c) for c in cluster.calls}
    assert not any(" secrets " in c + " " for c in listed)
    assert not any(
        c.split()[2] in ("pods", "events", "replicasets")
        for c in listed
        if c.startswith("kubectl get")
    )
    assert "pod" not in env["outputs"]["summary"]["counts"]


def test_a_forbidden_list_is_a_scoped_error_and_the_run_goes_on():
    env, doc, errors = run(
        Cluster(
            objects={"node": [obj("n1")], "deployment": [obj("web", "a")]},
            forbidden=["node"],
            drop=(
                "nodepool",
                "nodeclaim",
            ),
        )
    )
    assert env["status"] == "partial"
    assert [(e["code"], e["scope"]) for e in errors if e["scope"] == "node"] == [
        ("permission_denied", "node")
    ]
    assert parts(doc, "node")[0]["status"] == "forbidden" and "count" not in parts(doc, "node")[0]
    assert {p["status"] for p in parts(doc, "deployment")} == {"complete"}
    assert env["outputs"]["summary"]["partitions"]["forbidden"] == 1
    assert env["outputs"]["summary"]["errors"] == errors


def test_a_list_that_fails_another_way_keeps_its_code_and_is_failed_not_forbidden():
    class Slow(Cluster):
        def answer(self, a):
            if a[:2] == ["get", "services"]:
                return 1, "", "Unable to connect to the server: i/o timeout"
            return super().answer(a)

    env, doc, errors = run(Slow(drop=("certificate", "nodepool", "nodeclaim")))
    assert env["status"] == "partial"
    assert ("unreachable", "service") in [(e["code"], e["scope"]) for e in errors]
    assert parts(doc, "service")[0]["status"] == "failed"


def test_a_list_longer_than_max_items_is_cut_and_failed_so_nothing_is_swept():
    cluster = Cluster(
        objects={"deployment": [obj(f"d{i}", "a") for i in range(5)]},
        drop=("certificate", "nodepool", "nodeclaim"),
    )
    env, doc, errors = run(cluster, {"maxItems": 3})
    assert errors == []
    in_a = next(p for p in parts(doc, "deployment") if p["parentPath"].endswith("/a"))
    assert in_a["status"] == "failed" and "maxItems 3" in in_a["reason"] and "count" not in in_a
    assert len(items_of(doc, "deployment")) == 3


def test_the_namespaces_setting_narrows_the_scope_and_the_namespace_partition_is_never_complete():
    cluster = basic()
    env, doc, errors = run(cluster, {"namespaces": ["b"]})
    assert errors == [] and env["status"] == "ok"
    assert [i["identity"]["chain"][-1]["name"] for i in items_of(doc, "namespace")] == ["b"]
    assert parts(doc, "namespace")[0]["status"] == "excluded"
    assert [p["parentPath"] for p in parts(doc, "deployment")] == [
        "kubernetes/clusters/kind/namespaces/b"
    ]
    assert not any("--all-namespaces" in c for c in cluster.calls)
    assert validate("rw.discovery.v1", doc) == []


def test_a_named_namespace_that_cannot_be_read_gets_a_stub_parent_and_an_error():
    cluster = Cluster(
        forbidden=["a"],
        objects={"deployment": [obj("web", "a")]},
        drop=("certificate", "nodepool", "nodeclaim"),
    )
    env, doc, errors = run(cluster, {"namespaces": ["a"]})
    assert env["status"] == "partial"
    assert [(e["code"], e["scope"]) for e in errors] == [("permission_denied", "namespace/a")]
    stub = items_of(doc, "namespace")[0]
    assert stub["document"]["metadata"]["annotations"] == {dd.STUB_ANNOTATION: "true"}
    assert [i["identity"]["chain"][-1]["name"] for i in items_of(doc, "deployment")] == ["web"]


def test_a_named_namespace_that_does_not_exist_is_an_error_and_is_skipped():
    env, doc, errors = run(basic(), {"namespaces": ["a", "nope"]})
    assert env["status"] == "partial"
    assert [(e["code"], e["scope"]) for e in errors] == [("not_found", "namespace/nope")]
    assert all("nope" not in p.get("parentPath", "") for p in doc["commit"]["partitions"])


def test_without_a_namespace_list_namespaced_types_are_excluded_with_the_reason():
    env, doc, errors = run(
        Cluster(
            forbidden=["namespaces"],
            drop=("certificate", "nodepool", "nodeclaim"),
            objects={"node": [obj("n1")]},
        )
    )
    assert env["status"] == "partial"
    assert ("permission_denied", "namespace") in [(e["code"], e["scope"]) for e in errors]
    assert parts(doc, "namespace")[0]["status"] == "forbidden"
    dep = parts(doc, "deployment")
    assert dep[0]["status"] == "excluded" and "namespaces setting" in dep[0]["reason"]
    assert parts(doc, "node")[0]["status"] == "complete"
    assert validate("rw.discovery.v1", doc) == []


def test_kube_system_unreadable_drops_the_canonical_key_with_a_scoped_error():
    env, doc, errors = run(
        Cluster(kube_system=False, drop=("certificate", "nodepool", "nodeclaim"))
    )
    assert env["status"] == "partial"
    assert [(e["code"], e["scope"]) for e in errors] == [
        ("permission_denied", "namespace/kube-system")
    ]
    assert "root" not in env["outputs"]["summary"]
    assert "providerUid" not in doc["batches"][0]["items"][0]


def test_an_unreachable_cluster_fails_the_run_with_nothing_reported():
    class Down(Cluster):
        def answer(self, a):
            return (
                1,
                "",
                "Unable to connect to the server: dial tcp 127.0.0.1:1: connection refused",
            )

    env, doc, errors = run(Down())
    assert env["status"] == "failed" and [e["code"] for e in errors] == ["unreachable"]
    assert doc is None


def test_the_target_must_be_a_cluster_and_settings_are_checked_before_any_call():
    cluster = basic()
    env, _, errors = run(
        cluster, resource={**RESOURCE, "path": "kubernetes/clusters/kind/namespaces/a"}
    )
    assert env["status"] == "failed" and errors[0]["code"] == "invalid_input"
    env, _, errors = run(cluster, {"maxItems": 0})
    assert errors[0]["code"] == "invalid_input"
    env, _, errors = run(cluster, {"namespaces": ["ok", ""]})
    assert errors[0]["code"] == "invalid_input"
    assert cluster.calls == []


def test_jobs_a_cronjob_spawned_are_not_resources():
    spawned = obj("cj-1", "a", metadata={"ownerReferences": [{"kind": "CronJob", "name": "cj"}]})
    env, doc, _ = run(
        Cluster(
            objects={"job": [obj("once", "a"), spawned]},
            drop=("certificate", "nodepool", "nodeclaim"),
        )
    )
    assert [i["identity"]["chain"][-1]["name"] for i in items_of(doc, "job")] == ["once"]
    assert next(p for p in parts(doc, "job") if p["parentPath"].endswith("/a"))["count"] == 1


def test_many_items_split_into_batches_of_at_most_200_in_order():
    cluster = Cluster(
        namespaces=["a"],
        objects={"configmap": [obj(f"c{i}", "a") for i in range(450)]},
        drop=("certificate", "nodepool", "nodeclaim"),
    )
    _, doc, _ = run(cluster)
    sizes = [len(b["items"]) for b in doc["batches"]]
    assert max(sizes) == 200 and [b["seq"] for b in doc["batches"]] == list(
        range(1, len(sizes) + 1)
    )
    assert validate("rw.discovery.v1", doc) == []


# ------------------------------------------------------------ the sanitiser ----


def clean(o, kind="Deployment", limit=4096):
    return dd.sanitize(o, kind, limit)


def test_status_is_split_off_and_bookkeeping_dropped():
    o = obj("web", "a", status={"readyReplicas": 1}, spec={"replicas": 1})
    o["metadata"]["annotations"] = {dd.LAST_APPLIED: "{...}", "team": "x"}
    doc, status = clean(o)
    assert status == {"readyReplicas": 1} and "status" not in doc
    assert "managedFields" not in doc["metadata"] and "resourceVersion" not in doc["metadata"]
    assert doc["metadata"]["annotations"] == {"team": "x"}


def test_literal_env_values_never_leave_but_references_do():
    pod = {"spec": {"containers": [{"name": "c", "env": [
        {"name": "DB_PASSWORD", "value": "hunter2"},
        {"name": "HOST", "value": "db.local"},
        {"name": "K", "valueFrom": {"secretKeyRef": {"name": "s", "key": "k"}}},
    ]}]}}  # fmt: skip
    doc, _ = clean({"metadata": {"name": "x"}, **pod})
    env = doc["spec"]["containers"][0]["env"]
    assert env == [
        {"name": "DB_PASSWORD"},
        {"name": "HOST"},
        {"name": "K", "valueFrom": {"secretKeyRef": {"name": "s", "key": "k"}}},
    ]
    assert "hunter2" not in json.dumps(doc) and "db.local" not in json.dumps(doc)


def test_exec_probe_keeps_the_first_word_and_a_count():
    o = {
        "metadata": {"name": "x"},
        "spec": {"livenessProbe": {"exec": {"command": ["sh", "-c", "curl -u a:b http://x"]}}},
    }
    doc, _ = clean(o)
    assert doc["spec"]["livenessProbe"]["exec"] == {"command": ["sh"], "argCount": 3}


def test_configmap_values_are_not_synced_only_keys_sizes_and_hashes():
    cm = {
        "metadata": {"name": "c"},
        "data": {"app.conf": "x=1", "db_password": "hunter2"},
        "binaryData": {"blob": "AAAA"},
    }
    doc, _ = clean(cm, "ConfigMap")
    assert doc["data"] == {"app.conf": dd.OMITTED, "db_password": dd.OMITTED}
    assert (
        doc["dataHashes"]["app.conf"]["size"] == 3
        and len(doc["dataHashes"]["app.conf"]["sha256"]) == 64
    )
    assert doc["dataHashes"]["db_password"] == {"size": 7}  # a credential-named key gets no hash
    assert "binaryData" not in doc and doc["binaryDataHashes"]["blob"]["size"] == 3
    assert "hunter2" not in json.dumps(doc)


def test_long_strings_are_cut_and_credential_shapes_scrubbed():
    doc, _ = clean(
        {"metadata": {"name": "x", "annotations": {"a": "z" * 100, "b": "token: abc123def"}}},
        limit=64,
    )
    assert (
        doc["metadata"]["annotations"]["a"].endswith(dd.CUT)
        and len(doc["metadata"]["annotations"]["a"]) < 100
    )
    assert "abc123def" not in doc["metadata"]["annotations"]["b"]


def test_item_shape_follows_the_contract():
    item = dd.build_item(
        [("cluster", "k"), ("namespace", "a")], obj("a", labels=None), "Namespace", 4096
    )
    assert item["identity"] == {
        "platform": "kubernetes",
        "chain": [{"type": "cluster", "name": "k"}, {"type": "namespace", "name": "a"}],
    }
    assert item["providerUid"] == "uid-a" and item["displayName"] == "a" and item["labels"] == {}


# ------------------------------------------------------------ permissions ----


def rbac_needs(argv):
    """The RBAC (group, resource, verb) / ('url', path) a kubectl call needs."""
    a = argv[1:]
    if a[:3] == ["get", "--raw", "/version"]:
        return {("url", "/version", "get")}
    if a[0] == "api-resources":
        return {
            ("url", "/api", "get"),
            ("url", "/apis", "get"),
            ("url", "/api/*", "get"),
            ("url", "/apis/*", "get"),
        }
    if a[0] == "config":
        return set()
    assert a[0] == "get", a
    arg = a[1]
    named = (
        len([x for x in a[2:] if not x.startswith("-") and x not in ("json", "--namespace")]) > 0
    )
    plural, _, group = arg.partition(".")
    if a[1] == "namespaces":
        plural, group = "namespaces", ""
        named = a[2] != "-o" and not a[2].startswith("--")
    named = named and a[1] == "namespaces"
    return {(group, plural, "get" if named else "list")}


def declared():
    out = set()
    for rule in TASK["permissions"]:
        for url in rule.get("nonResourceURLs", []):
            for v in rule["verbs"]:
                out.add(("url", url, v))
        for g in rule.get("apiGroups", []):
            for r in rule.get("resources", []):
                for v in rule["verbs"]:
                    out.add((g, r, v))
    return out


@pytest.mark.parametrize("settings", [{}, {"namespaces": ["a"]}, {"namespaces": ["a", "nope"]}])
def test_declared_permissions_cover_every_kubectl_call(settings):
    cluster = basic()
    run(cluster, settings)
    needed = set()
    for argv in cluster.calls:
        needed |= rbac_needs(argv)
    assert needed and not needed - declared(), sorted(needed - declared())


def test_permissions_are_one_group_per_rule_get_and_list_and_exclude_secrets():
    for rule in TASK["permissions"]:
        if "apiGroups" in rule:
            assert len(rule["apiGroups"]) == 1 and rule["verbs"] == ["get", "list"]
    names = {(g, r) for g, r, _ in declared() if g != "url"}
    assert ("", "secrets") not in names and ("", "pods") not in names
    served = {(t[3], t[2]) for t in TYPES if t[0] != "secret"}
    assert names == served


def test_the_entry_point_is_declared_next_to_the_others():
    eps = yaml.safe_load((PLUGIN / "integration" / "integration.yaml").read_text())["entryPoints"]
    assert eps["discovery"] == {"task": "k8s-base/discover", "schedule": "*/15 * * * *"}
    assert eps["cli"] == {"task": "k8s-base/kubectl"} and "permissionCheck" in eps
    assert (
        TASK["credentials"] == ["kubernetes/kubeconfig", "runwhen/sync"]
        and TASK["readOnly"] is True
    )


# ------------------------------------------------------------ the sync client ----

CRED = {
    "apiBaseUrl": "https://papi.example",
    "token": "tok-SECRET",
    "workspace": "ws",
    "expiresAt": "2030-01-01T00:00:00Z",
    "scopes": ["x"],
}


class Http:
    def __init__(self, fail=None, rejected=None):
        self.requests = []
        self.fail = fail or {}
        self.rejected = rejected or []

    def __call__(self, request, timeout=None):
        path = request.full_url.split("/workspaces/ws")[1]
        self.requests.append((path, request.headers, json.loads(request.data)))
        if path in self.fail:
            raise urllib.error.HTTPError(
                request.full_url,
                self.fail[path],
                "x",
                {},
                __import__("io").BytesIO(b'{"detail":"nope"}'),
            )
        body = {}
        if path == "/resource-syncs":
            body = {"syncId": "s1", "leaseExpiresAt": "2030-01-01T00:00:00Z"}
        elif path.endswith("/items"):
            body = {"seq": 1, "accepted": 1, "rejected": self.rejected}
        return __import__("io").BytesIO(json.dumps(body).encode())


def a_doc():
    _, doc, _ = run(basic())
    return doc


def test_push_opens_sends_batches_in_order_and_commits():
    doc = a_doc()
    http = Http()
    commit, rejected = sync.SyncClient(CRED, opener=http).push(doc)
    paths = [p for p, _, _ in http.requests]
    assert paths[0] == "/resource-syncs" and paths[-1] == "/resource-syncs/s1/commit"
    assert paths[1:-1] == ["/resource-syncs/s1/items"] * len(doc["batches"])
    assert http.requests[0][1]["Authorization"] == "Bearer tok-SECRET"
    assert http.requests[-1][2]["summary"] == doc["commit"]["summary"] and rejected == []


def test_rejected_items_turn_their_partition_failed_before_commit():
    doc = a_doc()
    http = Http(rejected=[{"index": 2, "code": "parent_missing"}])
    sync.SyncClient(CRED, opener=http).push(doc)
    sent = {(p["type"], p.get("parentPath")): p for p in http.requests[-1][2]["partitions"]}
    victim = doc["batches"][0]["items"][2]["identity"]["chain"]
    assert (
        sent[
            (
                victim[-1]["type"],
                "kubernetes/clusters/kind"
                + (f"/namespaces/{victim[1]['name']}" if len(victim) > 2 else ""),
            )
        ]["status"]
        == "failed"
    )


def test_a_failed_push_aborts_the_open_sync_and_never_shows_the_token():
    http = Http(fail={"/resource-syncs/s1/commit": 403})
    with pytest.raises(Exception) as exc:
        sync.SyncClient(CRED, opener=http).push(a_doc())
    assert exc.value.code == "permission_denied" and "tok-SECRET" not in str(exc.value)
    assert http.requests[-1][0] == "/resource-syncs/s1/abort"


def test_server_errors_are_retried_then_reported():
    http = Http(fail={"/resource-syncs": 503})
    with pytest.raises(Exception) as exc:
        sync.SyncClient(CRED, opener=http, sleep=lambda s: None).push(a_doc())
    assert len(http.requests) == sync.ATTEMPTS and exc.value.code == "internal"


def test_an_unreachable_platform_is_unreachable():
    def down(request, timeout=None):
        raise urllib.error.URLError("connection refused")

    with pytest.raises(Exception) as exc:
        sync.SyncClient(CRED, opener=down, sleep=lambda s: None).push(a_doc())
    assert exc.value.code == "unreachable"


def test_the_script_pushes_when_the_lease_minted_a_credential(tmp_path, monkeypatch):
    cred = tmp_path / "cred.json"
    cred.write_text(json.dumps(CRED))
    http = Http()
    monkeypatch.setenv("RW_SYNC_CREDENTIAL", str(cred))
    monkeypatch.setattr("urllib.request.urlopen", http)
    env, doc, errors = run(basic())
    assert env["status"] == "ok" and errors == []
    assert [p for p, _, _ in http.requests][0] == "/resource-syncs"
    assert "tok-SECRET" not in json.dumps(env) and "tok-SECRET" not in json.dumps(doc)


def test_a_bad_credential_file_is_invalid_input(tmp_path, monkeypatch):
    cred = tmp_path / "cred.json"
    cred.write_text("not json")
    monkeypatch.setenv("RW_SYNC_CREDENTIAL", str(cred))
    env, _, errors = run(basic())
    assert env["status"] == "failed" and errors[0]["code"] == "invalid_input"


# ------------------------------------------------------------ recorded fixtures ----


def scenarios():
    return sorted((SKILL / "tests" / "discover").glob("*.yaml"))


@pytest.mark.parametrize("path", scenarios(), ids=lambda p: p.stem)
def test_every_recorded_sync_validates_against_the_contract(path):
    sc = yaml.safe_load(path.read_text())
    if sc["expect"]["status"] == "failed":
        pytest.skip("a failed run writes no sync")
    answers = {
        tuple(c["argv"]): (c.get("rc", 0), c.get("stdout", ""), c.get("stderr", ""))
        for c in sc["calls"]
    }
    tc = make_context(
        resource={**RESOURCE, "path": sc["resource"], "cluster": sc["resource"].split("/")[-1]},
        settings={**SETTINGS, **sc.get("settings", {})},
    )
    real = tc.ctx

    def replay(argv, cwd=None, timeout=None, env=None, *, inherit_env=True, check=None):
        rc, out, err = answers[tuple(argv)]
        return real._finish_run(list(argv), rc, out, err, check)

    real.run = replay
    env = run_main(SCRIPT.main, tc)
    doc = json.loads((tc.out_dir / "sync").read_text())
    tc.close()
    assert env["status"] == sc["expect"]["status"]
    assert validate("rw.discovery.v1", doc) == []
    assert validate("rw.discovery.summary.v1", env["outputs"]["summary"]) == []
    for batch in doc["batches"]:
        assert len(batch["items"]) <= 200
    secrets = [
        i
        for b in doc["batches"]
        for i in b["items"]
        if i["identity"]["chain"][-1]["type"] == "secret"
    ]
    assert secrets == []


def test_a_refused_connection_from_kubectl_is_unreachable():
    from k8s_base.k8s import classify_error

    assert (
        classify_error(
            "The connection to the server 127.0.0.1:1 was refused - did you specify the right host or port?"
        )
        == "unreachable"
    )
