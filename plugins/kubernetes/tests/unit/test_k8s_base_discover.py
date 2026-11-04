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
SETTINGS = {"namespaces": [], "maxItems": 2000, "maxStringBytes": 4096, "deadline": "480s"}


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


def test_a_named_namespace_that_cannot_be_read_has_an_error_and_no_stub_item():
    cluster = Cluster(
        forbidden=["a"],
        objects={"deployment": [obj("web", "a")]},
        drop=("certificate", "nodepool", "nodeclaim"),
    )
    env, doc, errors = run(cluster, {"namespaces": ["a"]})
    assert env["status"] == "partial"
    assert [(e["code"], e["scope"]) for e in errors] == [("permission_denied", "namespace/a")]
    assert items_of(doc, "namespace") == []  # no stub: the partitions carry the semantics
    assert parts(doc, "namespace")[0]["status"] == "excluded"
    assert [i["identity"]["chain"][-1]["name"] for i in items_of(doc, "deployment")] == ["web"]
    assert validate("rw.discovery.v1", doc) == []


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


def test_configmap_keys_report_size_only_and_one_hash_covers_the_whole_data_map():
    cm = {
        "metadata": {"name": "c"},
        "data": {"app.conf": "x=1", "db_password": "hunter2"},
        "binaryData": {"blob": "AAAA"},
    }
    doc, _ = clean(cm, "ConfigMap")
    assert doc["data"] == {"app.conf": dd.OMITTED, "db_password": dd.OMITTED}
    assert doc["dataSizes"] == {"app.conf": 3, "db_password": 7}
    assert "binaryData" not in doc and doc["binaryDataSizes"] == {"blob": 3}
    assert len(doc["dataHash"]) == 64 and "dataHashes" not in doc  # no per-key hashes
    assert "hunter2" not in json.dumps(doc)
    changed, _ = clean({**cm, "data": {"app.conf": "x=2", "db_password": "hunter2"}}, "ConfigMap")
    assert changed["dataHash"] != doc["dataHash"]  # and it changes when any value does


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


def test_permissions_are_one_group_per_rule_list_per_type_one_get_on_namespaces_and_exclude_secrets():
    for rule in TASK["permissions"]:
        if "apiGroups" in rule:
            assert len(rule["apiGroups"]) == 1 and rule["verbs"] in (["list"], ["get"])
    names = {(g, r) for g, r, _ in declared() if g != "url"}
    assert ("", "secrets") not in names and ("", "pods") not in names
    served = {(t[3], t[2]) for t in TYPES if t[0] != "secret"}
    assert names == served
    gets = {(g, r) for g, r, v in declared() if v == "get" and g != "url"}
    assert gets == {("", "namespaces")}


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
    def __init__(
        self,
        fail=None,
        rejected=None,
        items_body=None,
        commit_body=None,
        retry_after=None,
        raise_on=None,
    ):
        self.retry_after = retry_after
        self.raise_on = raise_on or {}
        self.timeouts = {}
        self.items_body = items_body or {}
        self.commit_body = commit_body or {}
        self.requests = []
        self.fail = fail or {}
        self.rejected = rejected or []

    def __call__(self, request, timeout=None):
        path = request.full_url.split("/workspaces/ws")[1]
        sent = json.loads(request.data)
        self.requests.append((path, request.headers, sent))
        self.timeouts[path] = timeout
        if path in self.raise_on:
            raise self.raise_on[path]
        if path in self.fail:
            raise urllib.error.HTTPError(
                request.full_url,
                self.fail[path],
                "x",
                {"Retry-After": self.retry_after} if self.retry_after else {},
                __import__("io").BytesIO(b'{"detail":"nope"}'),
            )
        body = {}
        if path == "/resource-syncs":
            body = {"syncId": "s1", "leaseExpiresAt": "2030-01-01T00:00:00Z"}
        elif path.endswith("/items"):
            body = {
                "seq": sent["seq"],
                "accepted": len(sent["items"]) - len(self.rejected),
                "rejected": self.rejected,
            }
            body.update(self.items_body)
        elif path.endswith("/commit"):
            body = {"status": "committed", "counts": {}, **self.commit_body}
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
    monkeypatch.setattr(sync, "default_open", http)
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


# ------------------------------------------------------------ review fixes ----

import http.server  # noqa: E402
import threading  # noqa: E402

DROP_OPTIONAL = ("certificate", "nodepool", "nodeclaim")


def push_env(monkeypatch, tmp_path, http, inline=False):
    value = json.dumps(CRED)
    if not inline:
        path = tmp_path / "cred.json"
        path.write_text(value)
        value = str(path)
    monkeypatch.setenv("RW_SYNC_CREDENTIAL", value)
    monkeypatch.setattr(sync, "default_open", http)


# --- M1: redaction by key name


@pytest.mark.parametrize(
    "key",
    [
        "secretName",
        "tokenSecretRef",
        "passwordFile",
        "tokenPath",
        "auth-secret-ref",
        "issuerNames",
        "replicas",
        "image",
    ],
)
def test_reference_keys_and_plain_keys_are_kept(key):
    assert dd.sensitive_key(key) is False


@pytest.mark.parametrize(
    "key",
    ["token", "password", "bearerToken", "clientSecret", "apiKey", "api_key", "secretKey",
     "Authorization", "sessionId", "Cookie", "private-key", "accessKey", "pwd", "credentials"],
)  # fmt: skip
def test_credential_named_keys_are_redacted(key):
    assert dd.sensitive_key(key) is True


def test_a_string_under_a_credential_key_is_redacted_whatever_its_text_and_references_stay():
    o = {
        "metadata": {"name": "x"},
        "spec": {
            "password": "plainly-not-a-pattern",
            "token": "abc",
            "bearerToken": "zzz",
            "secretName": "db-creds",
            "tokenSecretRef": {"name": "t", "key": "k"},
            "replicas": 3,
            "tokens": ["a", "b"],
        },
    }
    spec = clean(o)[0]["spec"]
    assert spec["password"] == spec["token"] == spec["bearerToken"] == "[redacted]"
    assert spec["tokens"] == ["[redacted]", "[redacted]"]
    assert spec["secretName"] == "db-creds" and spec["replicas"] == 3
    assert spec["tokenSecretRef"] == {"name": "t", "key": "k"}


POD_SPEC = {
    "containers": [{"name": "c", "command": ["/bin/app", "--token=abc"], "args": ["run", "--password", "pw"]}],
    "initContainers": [{"name": "i", "command": ["sh", "-c", "echo hunter2"]}],
    "ephemeralContainers": [{"name": "e", "args": ["dbg", "x"]}],
}  # fmt: skip


def test_container_command_and_args_keep_the_first_word_and_a_count():
    doc, _ = clean({"metadata": {"name": "p"}, "spec": POD_SPEC}, "Pod")
    c, i, e = (doc["spec"][k][0] for k in ("containers", "initContainers", "ephemeralContainers"))
    assert (c["command"], c["commandCount"], c["args"], c["argsCount"]) == (
        ["/bin/app"], 2, ["run"], 3
    )  # fmt: skip
    assert (i["command"], i["commandCount"]) == (["sh"], 3) and "args" not in i
    assert (e["args"], e["argsCount"]) == (["dbg"], 2)
    assert "hunter2" not in json.dumps(doc) and "pw" not in json.dumps(doc["spec"]["containers"])


def test_the_pod_template_inside_workloads_is_treated_the_same():
    deploy = {"metadata": {"name": "d"}, "spec": {"template": {"spec": POD_SPEC}}}
    cron = {
        "metadata": {"name": "j"},
        "spec": {"jobTemplate": {"spec": {"template": {"spec": POD_SPEC}}}},
    }
    for doc, path in (
        (clean(deploy)[0], ("spec", "template", "spec")),
        (clean(cron, "CronJob")[0], ("spec", "jobTemplate", "spec", "template", "spec")),
    ):
        node = doc
        for part in path:
            node = node[part]
        assert node["containers"][0]["command"] == ["/bin/app"]
        assert node["initContainers"][0]["commandCount"] == 3
        assert "--token=abc" not in json.dumps(doc)


def test_http_probe_header_values_are_redacted_names_kept():
    o = {"metadata": {"name": "x"}, "spec": {"readinessProbe": {"httpGet": {
        "path": "/h", "port": 80,
        "httpHeaders": [{"name": "X-Api", "value": "s3cr3t"}, {"name": "Host", "value": "plain"}],
    }}}}  # fmt: skip
    headers = clean(o)[0]["spec"]["readinessProbe"]["httpGet"]["httpHeaders"]
    assert headers == [
        {"name": "X-Api", "value": "[redacted]"},
        {"name": "Host", "value": "[redacted]"},
    ]


def test_env_as_a_map_has_its_values_dropped():
    o = {"metadata": {"name": "x"}, "spec": {"env": {"A": "plain", "B": "hunter2", "C": 5}}}
    env = clean(o)[0]["spec"]["env"]
    assert env == {"A": dd.OMITTED, "B": dd.OMITTED, "C": dd.OMITTED}


def test_helmrelease_values_are_replaced_by_a_hash_of_their_canonical_json():
    values = {"b": 1, "a": {"password": "hunter2"}}
    rel = {
        "metadata": {"name": "r"},
        "spec": {
            "chart": {"spec": {"chart": "x"}},
            "values": values,
            "valuesObject": {"k": "v"},
            "valuesFrom": [{"kind": "Secret", "name": "s", "valuesKey": "v", "inline": "pw: hunter2"}],
        },
    }  # fmt: skip
    doc = clean(rel, "HelmRelease")[0]
    want = dd.canonical_hash(values)
    assert doc["spec"]["values"] == want and set(want) == {"sha256", "sizeBytes"}
    assert dd.canonical_hash({"a": {"password": "hunter2"}, "b": 1}) == want  # key order is moot
    assert set(doc["spec"]["valuesObject"]) == {"sha256", "sizeBytes"}
    ref = doc["spec"]["valuesFrom"][0]
    assert (
        ref["name"] == "s"
        and ref["valuesKey"] == "v"
        and set(ref["inline"]) == {"sha256", "sizeBytes"}
    )
    assert "hunter2" not in json.dumps(doc) and doc["spec"]["chart"] == {"spec": {"chart": "x"}}


def test_argocd_helm_values_and_parameters_are_hashed_for_source_and_sources():
    helm = {
        "values": "pw: x",
        "valuesObject": {"a": 1},
        "parameters": [{"name": "p", "value": "hunter2"}],
    }
    app = {
        "metadata": {"name": "a"},
        "spec": {"source": {"repoURL": "u", "helm": helm}, "sources": [{"repoURL": "v", "helm": helm}]},
    }  # fmt: skip
    doc = clean(app, "Application")[0]
    for source in (doc["spec"]["source"], doc["spec"]["sources"][0]):
        assert set(source["helm"]["values"]) == {"sha256", "sizeBytes"}
        assert set(source["helm"]["valuesObject"]) == {"sha256", "sizeBytes"}
        assert source["helm"]["parameters"][0]["name"] == "p"
        assert set(source["helm"]["parameters"][0]["value"]) == {"sha256", "sizeBytes"}
    assert doc["spec"]["source"]["repoURL"] == "u" and "hunter2" not in json.dumps(doc)


def test_kustomize_patches_are_hashed():
    k = {
        "metadata": {"name": "k"},
        "spec": {"path": "./x", "patches": [{"patch": "value: hunter2"}]},
    }
    doc = clean(k, "Kustomization")[0]
    assert set(doc["spec"]["patches"]) == {"sha256", "sizeBytes"} and doc["spec"]["path"] == "./x"
    assert "hunter2" not in json.dumps(doc)


# --- M2: annotations


def test_bulky_apply_annotations_are_dropped_at_every_depth_and_values_capped():
    deploy = {
        "metadata": {"name": "d", "annotations": {dd.LAST_APPLIED: "{}", dd.KAPP_ORIGINAL: "{}", "n": "1"}},
        "spec": {"template": {"metadata": {"annotations": {dd.LAST_APPLIED: "{}", "big": "z" * 5000}}}},
    }  # fmt: skip
    doc = clean(deploy)[0]
    assert doc["metadata"]["annotations"] == {"n": "1"}
    inner = doc["spec"]["template"]["metadata"]["annotations"]
    assert (
        list(inner) == ["big"]
        and len(inner["big"].encode()) <= 512
        and inner["big"].endswith(dd.CUT)
    )
    assert dd.LAST_APPLIED not in json.dumps(doc) and dd.KAPP_ORIGINAL not in json.dumps(doc)


def test_an_object_whose_only_annotation_was_dropped_loses_the_key():
    doc = clean({"metadata": {"name": "d", "annotations": {dd.KAPP_ORIGINAL: "{}"}}})[0]
    assert "annotations" not in doc["metadata"]


# --- M3: a broken aggregated API group


STDERR_GROUP = (
    "error: unable to retrieve the complete list of server APIs: "
    "cert-manager.io/v1: stale GroupVersion discovery: cert-manager.io/v1\n"
)


class PartialApi(Cluster):
    def __init__(self, stderr=STDERR_GROUP, table=True, **kw):
        super().__init__(**kw)
        self.stderr, self.table = stderr, table

    def answer(self, a):
        if a[0] == "api-resources":
            return 1, api_table(self.served) if self.table else "", self.stderr
        return super().answer(a)


def partial_api(**kw):
    cluster = PartialApi(
        drop=("certificate", "issuer", "clusterissuer", "nodepool", "nodeclaim"), **kw
    )
    return cluster


def test_a_broken_api_group_fails_its_types_and_the_run_is_partial_not_failed():
    env, doc, errors = run(partial_api())
    assert env["status"] == "partial"
    assert [(e["code"], e["scope"]) for e in errors] == [("dependency", "cert-manager.io")]
    for type_name in ("certificate", "issuer", "clusterissuer"):
        (p,) = parts(doc, type_name)
        assert p["status"] == "failed" and "cert-manager.io" in p["reason"]
    # a type of another group that really is not served is still excluded, not failed
    assert parts(doc, "nodepool")[0]["status"] == "excluded"
    assert parts(doc, "deployment")[0]["status"] == "complete"
    assert validate("rw.discovery.v1", doc) == []


def test_a_failed_api_discovery_without_a_table_fails_the_run_with_its_code():
    env, doc, errors = run(partial_api(table=False))
    assert env["status"] == "failed" and doc is None
    assert [e["code"] for e in errors] == ["dependency"]


def test_unable_to_retrieve_the_complete_list_is_a_dependency_not_invalid_input():
    from k8s_base.k8s import classify_error

    assert classify_error(STDERR_GROUP, 1) == "dependency"


def test_a_failure_naming_no_group_is_one_error_and_the_table_is_still_used():
    env, doc, errors = run(partial_api(stderr="error: something odd\n"))
    assert env["status"] == "partial"
    assert [(e["code"], e["scope"]) for e in errors] == [("invalid_input", "api-resources")]
    assert parts(doc, "deployment")[0]["status"] == "complete"


# --- M4: held commits and push errors in the summary


def test_a_held_commit_is_an_internal_error_scoped_sync_and_the_run_is_partial(
    tmp_path, monkeypatch
):
    http = Http(commit_body={"status": "held", "heldReason": "would delete 40% of the scope"})
    push_env(monkeypatch, tmp_path, http)
    env, doc, errors = run(basic())
    assert env["status"] == "partial"
    assert [(e["code"], e["scope"]) for e in errors] == [("internal", "sync")]
    assert "would delete 40%" in errors[0]["message"]
    summary = env["outputs"]["summary"]
    assert [e["scope"] for e in summary["errors"]] == ["sync"]  # built after the push
    assert doc["commit"]["summary"].get("errors") is None  # the commit was sent before it


def test_rejected_items_appear_in_the_summary_and_their_partitions_in_its_totals(
    tmp_path, monkeypatch
):
    push_env(monkeypatch, tmp_path, Http(rejected=[{"index": 2, "code": "parent_missing"}]))
    env, _, errors = run(basic())
    summary = env["outputs"]["summary"]
    assert env["status"] == "partial" and [e["scope"] for e in summary["errors"]] == ["sync"]
    assert summary["partitions"]["failed"] >= 1


# --- M5: CRDs, batches, streaming, deadline


def test_a_crd_keeps_names_group_scope_versions_and_a_hash_of_the_schema_only():
    schema = {"openAPIV3Schema": {"type": "object", "properties": {"spec": {"type": "string"}}}}
    crd = {
        "metadata": {"name": "foos.example.com"},
        "spec": {
            "group": "example.com", "scope": "Namespaced",
            "names": {"kind": "Foo", "plural": "foos"},
            "versions": [{"name": "v1", "served": True, "storage": True, "schema": schema, "subresources": {"status": {}}}],
            "conversion": {"strategy": "None"},
        },
        "status": {"storedVersions": ["v1"]},
    }  # fmt: skip
    doc, status = clean(crd, "CustomResourceDefinition")
    assert doc["spec"]["versions"] == [{"name": "v1", "served": True, "storage": True}]
    assert (doc["spec"]["group"], doc["spec"]["scope"]) == ("example.com", "Namespaced")
    assert doc["spec"]["names"] == {"kind": "Foo", "plural": "foos"}
    assert len(doc["spec"]["schemaSha256"]) == 64 and "openAPIV3Schema" not in json.dumps(doc)
    assert status == {"storedVersions": ["v1"]}
    other = json.loads(json.dumps(crd))
    other["spec"]["versions"][0]["schema"]["openAPIV3Schema"]["type"] = "string"
    assert (
        clean(other, "CustomResourceDefinition")[0]["spec"]["schemaSha256"]
        != doc["spec"]["schemaSha256"]
    )


def test_batches_split_by_serialized_bytes_as_well_as_by_count():
    items = [{"identity": {"chain": []}, "document": {"x": "y" * 1000}} for _ in range(10)]
    out = dd.batches(items, size=200, max_bytes=3500)
    assert [len(b["items"]) for b in out] == [3, 3, 3, 1] and [b["seq"] for b in out] == [
        1,
        2,
        3,
        4,
    ]
    assert [i for b in out for i in b["items"]] == items
    big = dd.batches([{"document": "z" * 5000}, {"document": "a"}], size=200, max_bytes=1000)
    assert [len(b["items"]) for b in big] == [1, 1]  # an item over the limit travels alone
    assert dd.BATCH_BYTES == 4 * 1024 * 1024


def test_the_sync_artifact_is_streamed_never_one_giant_string(monkeypatch):
    real = json.dumps

    def guarded(value, *a, **kw):
        assert not (isinstance(value, dict) and "batches" in value), (
            "whole document dumped to a string"
        )
        return real(value, *a, **kw)

    monkeypatch.setattr(SCRIPT.json, "dumps", guarded)
    env, doc, _ = run(basic())
    assert env["status"] == "ok" and validate("rw.discovery.v1", doc) == []


class Slow(Cluster):
    """Every list takes 100 s of the fake clock."""

    def __init__(self, clock, **kw):
        super().__init__(**kw)
        self.clock = clock

    def answer(self, a):
        if a[0] == "get" and a[-2:] == ["-o", "json"] and "--raw" not in a:
            self.clock[0] += 100
        return super().answer(a)


def test_past_the_deadline_no_list_starts_remaining_types_fail_with_timeout_and_the_sync_is_pushed(
    monkeypatch, tmp_path
):
    clock = [0.0]
    monkeypatch.setattr(discovery, "_now", lambda: clock[0])
    http = Http()
    push_env(monkeypatch, tmp_path, http)
    cluster = Slow(clock, drop=DROP_OPTIONAL)
    env, doc, errors = run(cluster, {"deadline": "350s"})
    assert env["status"] == "partial"
    timeouts = [e for e in errors if e["code"] == "timeout"]
    assert timeouts and all(e["scope"] for e in timeouts)
    listed = [c for c in cluster.calls if c[1] == "get" and c[-2:] == ["-o", "json"]]
    assert len(listed) <= 5  # namespaces + 3 more at 100 s each, then the deadline stops the lists
    failed = [p for p in doc["commit"]["partitions"] if p["status"] == "failed"]
    assert failed and all("deadline" in p["reason"] for p in failed)
    served = {t[0] for t in TYPES if t[0] not in ("secret", "namespace", *DROP_OPTIONAL)}
    assert served <= {p["type"] for p in doc["commit"]["partitions"]}  # every type has a partition
    assert [p for p, _, _ in http.requests][-1] == "/resource-syncs/s1/commit"
    assert validate("rw.discovery.v1", doc) == []


def test_the_deadline_is_a_duration_between_1s_and_570s():
    for bad in ("soon", "0s", "600s", "10"):
        env, _, errors = run(basic(), {"deadline": bad})
        assert env["status"] == "failed" and errors[0]["code"] == "invalid_input", bad
    assert run(basic(), {"deadline": "1m30s"})[0]["status"] == "ok"


def test_the_deadline_is_declared_with_the_default_below_the_task_timeout():
    spec = TASK["settings"]["deadline"]
    assert spec["type"] == "duration" and spec["default"] == "480s" and TASK["timeout"] == "600s"


# --- m1: a missing items list is not an empty list


def test_a_list_reply_without_items_is_an_internal_failure_never_an_empty_partition():
    class NoItems(Cluster):
        def answer(self, a):
            if a[:2] == ["get", "deployments.apps"] or a[1:2] == ["deployments.apps"]:
                return 0, json.dumps({"kind": "Status"}), ""
            return super().answer(a)

    env, doc, errors = run(NoItems(drop=DROP_OPTIONAL))
    assert env["status"] == "partial"
    assert ("internal", "deployment") in [(e["code"], e["scope"]) for e in errors]
    assert parts(doc, "deployment") == [
        {"type": "deployment", "status": "failed", "reason": parts(doc, "deployment")[0]["reason"]}
    ]


def test_list_items_requires_a_list():
    class Ctx:
        def run(self, argv, **kw):
            import types

            return types.SimpleNamespace(returncode=0, stdout=self.out, stderr="")

    ctx = Ctx()
    ctx.out = '{"items": null}'
    with pytest.raises(Exception) as exc:
        discovery.list_items(ctx, ["get", "x", "-o", "json"])
    assert exc.value.code == "internal"
    ctx.out = '{"items": []}'
    assert discovery.list_items(ctx, ["get", "x", "-o", "json"]) == []


# --- m2: https only, no redirects


def test_a_non_https_api_base_url_is_refused_unless_the_test_switch_is_set(monkeypatch):
    http_cred = {**CRED, "apiBaseUrl": "http://papi.example"}
    monkeypatch.delenv("RW_SYNC_ALLOW_HTTP", raising=False)
    with pytest.raises(Exception) as exc:
        sync.SyncClient(http_cred)
    assert exc.value.code == "invalid_input" and "https" in str(exc.value)
    monkeypatch.setenv("RW_SYNC_ALLOW_HTTP", "1")
    sync.SyncClient(http_cred)


def test_a_redirect_is_an_error_and_is_never_followed(monkeypatch):
    seen = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            seen.append(self.path)
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1:1/elsewhere")
            self.end_headers()

        def log_message(self, *a):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("RW_SYNC_ALLOW_HTTP", "1")
    try:
        client = sync.SyncClient({**CRED, "apiBaseUrl": f"http://127.0.0.1:{server.server_port}"})
        with pytest.raises(Exception) as exc:
            client.push(a_doc())
    finally:
        server.shutdown()
    assert exc.value.code == "internal" and "redirect" in str(exc.value)
    assert seen == ["/api/v4/workspaces/ws/resource-syncs"] and "tok-SECRET" not in str(exc.value)


# --- m3: HTTP handling


def test_status_codes_map_to_d64_codes():
    def code_of(status):
        http = Http(fail={"/resource-syncs": status})
        with pytest.raises(Exception) as exc:
            sync.SyncClient(CRED, opener=http, sleep=lambda s: None).push(a_doc())
        return exc.value.code, str(exc.value), len(http.requests)

    code, text, calls = code_of(409)
    assert code == "internal" and "conflict" in text and calls == 1
    assert code_of(413)[0] == "invalid_input" and code_of(413)[2] == 1
    assert code_of(429)[0] == "rate_limited" and code_of(429)[2] == sync.ATTEMPTS


def test_backoff_is_1_4_10_seconds_and_a_429_honours_retry_after():
    sleeps = []
    with pytest.raises(Exception):
        sync.SyncClient(CRED, opener=Http(fail={"/resource-syncs": 503}), sleep=sleeps.append).push(
            a_doc()
        )
    assert sleeps == [1, 4, 10]
    sleeps.clear()
    http = Http(fail={"/resource-syncs": 429}, retry_after="7")
    with pytest.raises(Exception):
        sync.SyncClient(CRED, opener=http, sleep=sleeps.append).push(a_doc())
    assert sleeps == [7, 7, 10]  # the larger of the backoff and Retry-After


def test_commit_gets_300s_and_is_not_retried_after_a_read_timeout_nor_aborted():
    http = Http(raise_on={"/resource-syncs/s1/commit": TimeoutError("read timed out")})
    with pytest.raises(Exception) as exc:
        sync.SyncClient(CRED, opener=http, sleep=lambda s: None).push(a_doc())
    assert exc.value.code == "timeout"
    paths = [p for p, _, _ in http.requests]
    assert paths.count("/resource-syncs/s1/commit") == 1  # not retried
    assert not any(p.endswith("/abort") for p in paths)  # it may have landed
    assert http.timeouts["/resource-syncs/s1/commit"] == sync.COMMIT_TIMEOUT == 300
    assert http.timeouts["/resource-syncs"] == sync.TIMEOUT


def test_any_exception_after_open_aborts_and_the_original_is_the_one_raised():
    http = Http(raise_on={"/resource-syncs/s1/items": RuntimeError("boom")})
    with pytest.raises(RuntimeError, match="boom"):
        sync.SyncClient(CRED, opener=http, sleep=lambda s: None).push(a_doc())
    assert http.requests[-1][0] == "/resource-syncs/s1/abort"


# --- m4: items the platform did not account for


def test_a_batch_the_platform_did_not_account_for_fails_its_partitions():
    doc = a_doc()
    http = Http(items_body={"accepted": 0})
    sync.SyncClient(CRED, opener=http).push(doc)
    sent = http.requests[-1][2]["partitions"]
    touched = {(i["identity"]["chain"][-1]["type"]) for b in doc["batches"] for i in b["items"]}
    assert all(
        p["status"] == "failed" for p in sent if p["type"] in touched and p["status"] != "excluded"
    )
    assert any(p["reason"] == "batch not accounted for" for p in sent)


def test_accepted_plus_rejected_equal_to_the_batch_changes_nothing():
    doc = a_doc()
    http = Http(rejected=[{"index": 1, "code": "bad_name"}])
    sync.SyncClient(CRED, opener=http).push(doc)
    reasons = [p.get("reason") for p in http.requests[-1][2]["partitions"]]
    assert "batch not accounted for" not in reasons


# --- m10 / m11


def test_the_server_url_keeps_ipv6_brackets_and_drops_credentials():
    assert discovery.server_url("https://[::1]:6443") == "https://[::1]:6443"
    assert (
        discovery.server_url("https://u:pw@[fe80::1]/k8s/clusters/c")
        == "https://[fe80::1]/k8s/clusters/c"
    )
    assert discovery.server_url("https://Host.example:443/") == "https://Host.example:443"


@pytest.mark.parametrize("bad", ["https://host:notaport", "https://host:99999", "host-only", ""])
def test_a_bad_server_url_or_port_is_invalid_input(bad):
    with pytest.raises(Exception) as exc:
        discovery.server_url(bad)
    assert exc.value.code == "invalid_input" and "pw" not in str(exc.value)


# --- m9: SKILL.md


def test_skill_md_lists_every_ephemeral_type_and_explains_held_commits():
    text = (SKILL / "SKILL.md").read_text()
    types = yaml.safe_load((PLUGIN / "integration" / "types.yaml").read_text())["types"]
    ephemeral = [n for n, t in types.items() if t.get("ephemeral")]
    assert ephemeral and all(f"`{n}`" in text for n in ephemeral), [
        n for n in ephemeral if f"`{n}`" not in text
    ]
    for word in ("held", "rejected", "type-wide", "CronJob"):
        assert word in text


# --- RW_SYNC_CREDENTIAL: a file path or inline JSON


def test_the_credential_is_read_from_a_file(tmp_path):
    path = tmp_path / "c.json"
    path.write_text(json.dumps(CRED))
    assert sync.load_credential(str(path)) == {
        k: CRED[k] for k in ("apiBaseUrl", "token", "workspace")
    }


@pytest.mark.parametrize("pad", ["", "  ", "\n\t"])
def test_the_credential_may_be_inline_json(pad):
    got = sync.load_credential(pad + json.dumps(CRED) + pad)
    assert got == {k: CRED[k] for k in ("apiBaseUrl", "token", "workspace")}


def test_a_bad_inline_credential_is_invalid_input_and_never_echoed():
    with pytest.raises(Exception) as exc:
        sync.load_credential('{"token": "tok-SECRET", "oops"')
    assert exc.value.code == "invalid_input" and "tok-SECRET" not in str(exc.value)
    with pytest.raises(Exception) as exc:
        sync.load_credential('{"token": "tok-SECRET"}')  # fields missing
    assert exc.value.code == "invalid_input" and "tok-SECRET" not in str(exc.value)


def test_the_script_pushes_with_an_inline_credential_and_scope_path_is_the_target(
    tmp_path, monkeypatch
):
    http = Http()
    push_env(monkeypatch, tmp_path, http, inline=True)
    env, doc, errors = run(basic())
    assert env["status"] == "ok" and errors == []
    assert [p for p, _, _ in http.requests][0] == "/resource-syncs"
    assert http.requests[0][2]["scopePath"] == RESOURCE["path"] == doc["open"]["scopePath"]
    assert [p for p, _, _ in http.requests if "resource-pack" in p] == []


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


def _item(platform, *chain):
    return {"identity": {"platform": platform, "chain": [{"type": t, "name": n} for t, n in chain]}}


@pytest.mark.parametrize(
    "item, parent_path",
    [
        # namespaced kubernetes object: partition per (type, namespace)
        (
            _item("kubernetes", ("cluster", "c"), ("namespace", "ns"), ("deployment", "web")),
            "kubernetes/clusters/c/namespaces/ns",
        ),
        # cluster-scoped kubernetes object: partition under the cluster
        (_item("kubernetes", ("cluster", "c"), ("node", "n1")), "kubernetes/clusters/c"),
        # two levels (postgres instance -> database)
        (_item("postgres", ("instance", "i"), ("database", "orders")), "postgres/instances/i"),
        # three levels with no namespace (aws account -> region -> function)
        (
            _item("aws", ("account", "123"), ("region", "eu-west-1"), ("lambda-function", "f")),
            "aws/accounts/123/regions/eu-west-1",
        ),
    ],
)
def test_a_rejected_item_fails_exactly_its_own_partition_for_any_integration_shape(
    item, parent_path
):
    t = item["identity"]["chain"][-1]["type"]
    other = parent_path.rsplit("/", 1)[0] + "/other"
    partitions = [
        {"type": t, "status": "complete", "parentPath": parent_path, "count": 3},
        {"type": t, "status": "complete", "parentPath": other, "count": 2},
    ]
    out = sync.fail_rejected(partitions, [(*sync._key(item), {"code": "parent_missing"})])
    assert [p["status"] for p in out] == ["failed", "complete"]
    assert "count" not in out[0] and "parent_missing" in out[0]["reason"]
    short = sync.fail_rejected(partitions, [], {sync._key(item): "batch not accounted for"})
    assert [p["status"] for p in short] == ["failed", "complete"]


def test_a_partition_without_parent_path_is_never_matched():
    partitions = [{"type": "deployment", "status": "forbidden"}]
    item = _item("kubernetes", ("cluster", "c"), ("namespace", "ns"), ("deployment", "web"))
    assert sync.fail_rejected(partitions, [(*sync._key(item), {})]) == partitions
