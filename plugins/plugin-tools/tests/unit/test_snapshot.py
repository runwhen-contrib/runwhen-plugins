"""The snapshot exporter and `python -m plugin_tools.cli snapshot export` (archive.md section 6)."""

import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
from datetime import UTC, datetime

import pytest
from conftest import LIB, PLUGIN
from fixtures import make_plugin_tree
from gitrepo import GitRepo
from plugin_tools import cli
from plugin_tools.bundle import prescan, read_bundle
from plugin_tools.prepare import prepare
from plugin_tools.runtimes import RuntimeSpec
from plugin_tools.snapshot import (
    SnapshotVersion,
    UstarWriter,
    current_runtimes,
    export_snapshot,
)
from runwhen_capability.contracts import archive as sdk_archive

NOW = datetime(2026, 11, 5, 12, 0, 0, tzinfo=UTC)
CREATED_AT = "2026-11-05T12:00:00Z"
REPO_URL = "https://github.com/runwhen-contrib/runwhen-plugins"


def _spec(name="universal", version="1.0.0", b="b"):
    return RuntimeSpec(
        name=name,
        version=version,
        image="ghcr.io/runwhen-contrib/runwhen-plugins-runtime-universal",
        digests={"index": "sha256:" + "a" * 64, "linux/amd64": "sha256:" + b * 64},
        tools={"git": "2.47"},
    )


def _version(name, version):
    return SnapshotVersion(
        prepare(make_plugin_tree(name, version)),
        {
            "kind": "public",
            "id": "runwhen-plugins",
            "repo": REPO_URL,
            "path": f"plugins/{name}",
            "tag": f"{name}/v{version}",
            "commit": "c" * 40,
        },
    )


def _members(body: bytes) -> list[tuple[str, bytes]]:
    with tarfile.open(fileobj=io.BytesIO(body), mode="r:") as tf:
        return [(m.name, tf.extractfile(m).read()) for m in tf]


def _export(versions, runtimes=(), release="2026.11.1"):
    out = io.BytesIO()
    sha = export_snapshot(out, versions, runtimes, release=release, now=NOW)
    return out.getvalue(), sha


# -- UstarWriter (ported from the registry's tests) --------------------------------------------


def test_ustar_writer_matches_sdk_archive_bytes():
    deep = "/".join(["seg" * 10] * 7)  # several valid prefix splits
    assert 100 < len(f"{deep}/file.json".encode()) <= 255
    entries = [
        ("a.txt", b"hello"),
        ("dir/b.bin", bytes(range(256)) * 3),
        ("empty", b""),
        (f"{deep}/file.json", b"{}"),
        ("x" * 99 + "/" + "y" * 100, b"boundary"),
        ("big", b"0123456789abcdef" * (1024 * 1024 // 16 + 5000)),  # more than one 1 MiB chunk
    ]
    entries.sort(key=lambda e: e[0].encode())
    expected = sdk_archive.build([sdk_archive.TreeEntry(p, d) for p, d in entries])
    out = io.BytesIO()
    writer = UstarWriter(out)
    for i, (path, data) in enumerate(entries):
        writer.add(path, data if i % 2 else io.BytesIO(data), len(data))  # bytes and streams
    writer.close()
    assert len(out.getvalue()) % 10240 == 0
    assert out.getvalue() == expected


def test_ustar_writer_rejects_wrong_stream_length_and_unsplittable_path():
    writer = UstarWriter(io.BytesIO())
    with pytest.raises(ValueError):
        writer.add("short", io.BytesIO(b"abc"), 5)
    with pytest.raises(ValueError):
        writer.add("long", io.BytesIO(b"abcdef"), 3)
    with pytest.raises(ValueError):
        writer.add("z" * 101, b"", 0)


# -- the format --------------------------------------------------------------------------------


def test_layout_and_order():
    body, sha = _export(
        [_version("pg", "1.0.0"), _version("kube", "0.10.0"), _version("kube", "0.9.0")], [_spec()]
    )
    assert sha == hashlib.sha256(body).hexdigest() and len(body) % 10240 == 0
    names = [n for n, _ in _members(body)]
    blobs = [n for n in names if n.startswith("blobs/sha256/")]
    assert names[: len(names) - len(blobs)] == [
        "snapshot.json",
        "index/kube/0.9.0.json",
        "index/kube/0.10.0.json",  # semver order, not string order
        "index/pg/1.0.0.json",
        "runtimes.json",
    ]
    assert blobs == sorted(blobs) and len(blobs) == 3
    prescan(body)  # plain ustar, as the importers' prescan requires

    members = dict(_members(body))
    manifest = json.loads(members["snapshot.json"])
    assert manifest["apiVersion"] == "registry.runwhen.com/snapshot/v1"
    assert manifest["createdAt"] == "2026-11-05T12:00:00Z" and manifest["release"] == "2026.11.1"
    assert manifest["source"] == "public"
    assert [(p["name"], p["version"]) for p in manifest["plugins"]] == [
        ("kube", "0.9.0"),
        ("kube", "0.10.0"),
        ("pg", "1.0.0"),
    ]
    for p in manifest["plugins"]:
        assert len(p["sha256"]) == 64 and members[f"blobs/sha256/{p['sha256']}"]
        assert len(members[f"blobs/sha256/{p['sha256']}"]) == p["size"]
    assert manifest["runtimes"] == [
        {
            "name": "universal",
            "major": 1,
            "digests": {"index": "sha256:" + "a" * 64, "linux/amd64": "sha256:" + "b" * 64},
        }
    ]


def test_index_entry_shape():
    v = _version("kube", "0.4.0")
    body, _ = _export([v])
    entry = json.loads(dict(_members(body))["index/kube/0.4.0.json"])
    assert set(entry) == {
        "name",
        "version",
        "archive",
        "source",
        "requires",
        "runtimes",
        "ingestedAt",
        "withdrawn",
        "compiled",
        "sdkVersion",
    }
    assert entry["archive"] == {
        "sha256": v.prepared.sha256_ref,
        "size": v.prepared.size,
        "files": v.prepared.files,
    }
    assert entry["source"]["repo"] == REPO_URL and entry["source"]["tag"] == "kube/v0.4.0"
    assert entry["ingestedAt"] == "2026-11-05T12:00:00Z" and entry["withdrawn"] is None
    assert entry["sdkVersion"].startswith("runwhen-capability/")
    assert entry["compiled"] == v.prepared.compiled
    # canonical JSON: sorted keys, no spaces
    raw = dict(_members(body))["index/kube/0.4.0.json"]
    assert raw == json.dumps(entry, sort_keys=True, separators=(",", ":")).encode()


def test_runtimes_json_is_the_upstream_item_shape():
    body, _ = _export([_version("kube", "0.4.0")], [_spec()])
    (item,) = json.loads(dict(_members(body))["runtimes.json"])["items"]
    assert item == {
        "name": "universal",
        "major": 1,
        "version": "1.0.0",
        "image": "ghcr.io/runwhen-contrib/runwhen-plugins-runtime-universal",
        "upstreamImage": "ghcr.io/runwhen-contrib/runwhen-plugins-runtime-universal",
        "digests": {"index": "sha256:" + "a" * 64, "linux/amd64": "sha256:" + "b" * 64},
        "tools": {"git": "2.47"},
        "mirrored": False,
    }


def test_current_runtime_per_major_prefers_the_newest_release():
    chosen = current_runtimes(
        [
            _spec(version="1.0.0"),
            _spec(version="1.2.0"),
            _spec(version="1.3.0-rc.1"),
            _spec(version="2.0.0-rc.1"),
        ]
    )
    assert [(r.name, r.version) for r in chosen] == [
        ("universal", "1.2.0"),
        ("universal", "2.0.0-rc.1"),
    ]


def test_export_is_deterministic_and_order_independent():
    a = [_version("kube", "0.4.0"), _version("pg", "1.0.0")]
    first, _ = _export(a, [_spec()])
    second, _ = _export(list(reversed(a)), [_spec()])
    assert first == second


def test_a_version_twice_is_refused():
    with pytest.raises(ValueError, match="twice"):
        _export([_version("kube", "0.4.0"), _version("kube", "0.4.0")])


def test_the_exported_snapshot_is_readable_by_the_bundle_reader():
    body, _ = _export([_version("kube", "0.4.0"), _version("pg", "1.0.0")])
    listed, blobs = read_bundle(body)
    assert [(e.name, e.version) for e in listed] == [("kube", "0.4.0"), ("pg", "1.0.0")]
    assert set(blobs) == {e.sha256 for e in listed}


# -- the CLI -----------------------------------------------------------------------------------


@pytest.fixture
def plugins_repo(tmp_path):
    r = GitRepo(tmp_path / "plugins-repo")
    r.publish("plugins/alpha", make_plugin_tree("alpha", "1.0.0"), "alpha/v1.0.0")
    r.publish("plugins/alpha", make_plugin_tree("alpha", "1.1.0"), "alpha/v1.1.0", annotated=True)
    r.publish("plugins/beta", make_plugin_tree("beta", "0.1.0", skills=("b",)), "beta/v0.1.0")
    return r


def _run(repo, out, *extra) -> int:
    return cli.main(
        [
            "snapshot",
            "export",
            "--repo",
            str(repo.path),
            "-o",
            str(out),
            "--created-at",
            CREATED_AT,
            *extra,
        ]
    )


def _names(path) -> list[str]:
    with tarfile.open(path, "r:") as tf:
        return tf.getnames()


def test_cli_all_tags(plugins_repo, tmp_path, capsys):
    out = tmp_path / "snapshot.tar"
    assert _run(plugins_repo, out, "--tags", "all", "--release", "2026.11.1") == 0
    printed = capsys.readouterr().out.strip()
    assert printed == f"sha256:{hashlib.sha256(out.read_bytes()).hexdigest()}  {out}"
    names = _names(out)
    assert [n for n in names if n.startswith("index/")] == [
        "index/alpha/1.0.0.json",
        "index/alpha/1.1.0.json",
        "index/beta/0.1.0.json",
    ]
    entry = json.loads(dict(_members(out.read_bytes()))["index/alpha/1.1.0.json"])
    # the recorded repo is the public URL, never the local clone's path; the commit is the peeled one
    assert entry["source"] == {
        "kind": "public",
        "id": "runwhen-plugins",
        "repo": REPO_URL,
        "path": "plugins/alpha",
        "tag": "alpha/v1.1.0",
        "commit": plugins_repo.git("rev-parse", "alpha/v1.1.0^{commit}"),
    }
    assert str(plugins_repo.path) not in out.read_bytes().decode("latin1")


def test_cli_selected_tags_only(plugins_repo, tmp_path):
    out = tmp_path / "s.tar"
    assert _run(plugins_repo, out, "--tags", "alpha/v1.0.0, beta/v0.1.0") == 0
    assert [n for n in _names(out) if n.startswith("index/")] == [
        "index/alpha/1.0.0.json",
        "index/beta/0.1.0.json",
    ]


def test_cli_is_reproducible_with_created_at(plugins_repo, tmp_path):
    a, b = tmp_path / "a.tar", tmp_path / "b.tar"
    assert (
        _run(plugins_repo, a, "--tags", "all") == 0 and _run(plugins_repo, b, "--tags", "all") == 0
    )
    assert a.read_bytes() == b.read_bytes()
    manifest = json.loads(dict(_members(a.read_bytes()))["snapshot.json"])
    assert manifest["createdAt"] == CREATED_AT and manifest["release"] is None


def test_cli_without_created_at_uses_now(plugins_repo, tmp_path):
    out = tmp_path / "s.tar"
    assert (
        cli.main(
            [
                "snapshot",
                "export",
                "--repo",
                str(plugins_repo.path),
                "-o",
                str(out),
                "--tags",
                "all",
            ]
        )
        == 0
    )
    created = json.loads(dict(_members(out.read_bytes()))["snapshot.json"])["createdAt"]
    assert abs((datetime.now(UTC) - datetime.fromisoformat(created)).total_seconds()) < 120


def test_cli_includes_runtime_tags(plugins_repo, tmp_path):
    plugins_repo.write(
        "runtimes/universal/runtime.yaml",
        (
            "name: universal\nversion: 1.0.0\nimage: ghcr.io/runwhen-contrib/x\n"
            f"digests: {{index: 'sha256:{'a' * 64}'}}\ntools: {{git: '2.47'}}\n"
        ).encode(),
    )
    plugins_repo.commit()
    plugins_repo.tag("runtimes/universal/v1.0.0")
    out = tmp_path / "s.tar"
    assert _run(plugins_repo, out, "--tags", "all") == 0
    items = json.loads(dict(_members(out.read_bytes()))["runtimes.json"])["items"]
    assert [(i["name"], i["version"]) for i in items] == [("universal", "1.0.0")]


def test_cli_a_rejected_tag_fails_without_output(plugins_repo, tmp_path, capsys):
    plugins_repo.publish(
        "plugins/alpha", make_plugin_tree("alpha", "9.9.9"), "alpha/v2.0.0"
    )  # mismatch
    out = tmp_path / "s.tar"
    assert _run(plugins_repo, out, "--tags", "all") == 1
    err = capsys.readouterr().err
    assert "alpha/v2.0.0" in err and "E_PLUGIN_INVALID" in err and not out.exists()
    # selecting only the good tags still works: the bad one is not in the snapshot
    assert _run(plugins_repo, out, "--tags", "alpha/v1.0.0") == 0


def test_cli_usage_errors_are_exit_2(plugins_repo, tmp_path, capsys):
    out = tmp_path / "s.tar"
    assert (
        _run(plugins_repo, out, "--tags", "alpha/v9.9.9") == 2
        and "no such tag" in capsys.readouterr().err
    )
    plugins_repo.tag("not-a-plugin-tag")
    assert _run(plugins_repo, out, "--tags", "not-a-plugin-tag") == 2
    assert _run(plugins_repo, out, "--tags", " , ") == 2
    assert _run(plugins_repo, tmp_path / "missing-dir" / "s.tar", "--tags", "all") == 2
    assert _run(plugins_repo, out, "--tags", "all", "--created-at", "yesterday") == 2
    not_repo = tmp_path / "plain"
    not_repo.mkdir()
    assert (
        cli.main(["snapshot", "export", "--repo", str(not_repo), "--tags", "all", "-o", str(out)])
        == 2
    )
    assert not out.exists()


def test_cli_runs_as_a_module_without_the_network(plugins_repo, tmp_path):
    out = tmp_path / "s.tar"
    env = {**os.environ, "PYTHONPATH": str(LIB)}
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "plugin_tools.cli",
            "snapshot",
            "export",
            "--tags",
            "all",
            "--repo",
            str(plugins_repo.path),
            "--created-at",
            CREATED_AT,
            "-o",
            str(out),
        ],
        env=env,
        capture_output=True,
        text=True,
        cwd=tmp_path,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("sha256:") and out.exists()
    help_ = subprocess.run(
        [sys.executable, "-m", "plugin_tools.cli", "snapshot", "export", "--help"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert help_.returncode == 0 and "--tags" in help_.stdout


def test_the_plugin_itself_exports(tmp_path):
    """The plugin-tools directory of this repository is a valid input of its own exporter."""
    repo = GitRepo(tmp_path / "self")
    tree = {
        str(p.relative_to(PLUGIN)): p.read_bytes()
        for p in PLUGIN.rglob("*")
        if p.is_file() and "__pycache__" not in p.parts and ".pytest_cache" not in p.parts
    }
    for rel, data in tree.items():
        repo.write(f"plugins/plugin-tools/{rel}", data)
    repo.commit()
    repo.tag("plugin-tools/v1.0.0")
    out = tmp_path / "self.tar"
    assert _run(repo, out, "--tags", "all") == 0
    assert "index/plugin-tools/1.0.0.json" in _names(out)
