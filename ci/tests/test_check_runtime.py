import hashlib
import json
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from check_runtime import (  # noqa: E402
    PLACEHOLDER_DIGEST,
    check_compare,
    check_tag,
    compare_digests,
    digests_from_index,
    format_digests,
    load_runtime,
    main,
    parse_tag,
)

D_INDEX = "sha256:" + "a" * 64
D_AMD = "sha256:" + "b" * 64
D_ARM = "sha256:" + "c" * 64
D_ATT = "sha256:" + "d" * 64


def spec(**over) -> dict:
    doc = {
        "name": "kubernetes",
        "version": "1.0.0",
        "image": "ghcr.io/runwhen-contrib/runwhen-plugins-runtime-kubernetes",
        "digests": {"index": D_INDEX, "linux/amd64": D_AMD, "linux/arm64": D_ARM},
        "tools": {"kubectl": "1.37.1", "jq": "1.7.1"},
        "description": "x",
    }
    doc.update(over)
    return {k: v for k, v in doc.items() if v is not None}


def write(tmp_path: Path, doc) -> Path:
    d = tmp_path / "runtimes" / "kubernetes"
    d.mkdir(parents=True, exist_ok=True)
    (d / "runtime.yaml").write_text(yaml.safe_dump(doc))
    return d


def index_bytes(*platforms: tuple[str, str, str | None, str]) -> bytes:
    """An OCI index with the given (os, arch, variant, digest) manifests, plus an attestation."""
    manifests = []
    for os_, arch, variant, digest in platforms:
        platform = {"os": os_, "architecture": arch}
        if variant:
            platform["variant"] = variant
        manifests.append({"digest": digest, "platform": platform})
    manifests.append({"digest": D_ATT, "platform": {"os": "unknown", "architecture": "unknown"}})
    return json.dumps({"schemaVersion": 2, "manifests": manifests}).encode()


RAW = index_bytes(("linux", "amd64", None, D_AMD), ("linux", "arm64", None, D_ARM))
RAW_DIGEST = "sha256:" + hashlib.sha256(RAW).hexdigest()


def test_valid(tmp_path):
    d = write(tmp_path, spec())
    assert load_runtime(d / "runtime.yaml")["name"] == "kubernetes"


def test_shipped_runtime_yaml_is_valid_apart_from_the_placeholder():
    d = Path(__file__).resolve().parents[2] / "runtimes" / "kubernetes"
    doc = load_runtime(d / "runtime.yaml", allow_placeholder=True)
    assert parse_tag(f"runtimes/{doc['name']}/v{doc['version']}") == ("kubernetes", doc["version"])


def test_placeholder_refused_unless_allowed(tmp_path):
    d = write(tmp_path, spec(digests={"index": PLACEHOLDER_DIGEST}))
    with pytest.raises(ValueError, match="placeholder"):
        load_runtime(d / "runtime.yaml")
    load_runtime(d / "runtime.yaml", allow_placeholder=True)


@pytest.mark.parametrize(
    "over,match",
    [
        ({"extra": 1}, "extra key"),
        ({"name": "Kube"}, "name"),
        ({"version": "1.0"}, "version"),
        ({"version": 1.0}, "version"),
        ({"image": "ghcr.io/x/y:1.0"}, "image"),
        ({"image": "ghcr.io/x/y@" + D_INDEX}, "image"),
        ({"image": "https://ghcr.io/x/y"}, "image"),
        ({"digests": {}}, "digests"),
        ({"digests": {"linux/s390x": D_AMD}}, "digests key"),
        ({"digests": {"index": "sha256:" + "A" * 64}}, "digests.index"),
        ({"digests": {"index": "sha256:abc"}}, "digests.index"),
        ({"tools": {"kubectl": 1.37}}, "tools"),
        ({"tools": ["kubectl"]}, "tools"),
        ({"description": 3}, "description"),
    ],
)
def test_invalid_fields(tmp_path, over, match):
    d = write(tmp_path, spec(**over))
    with pytest.raises(ValueError, match=match):
        load_runtime(d / "runtime.yaml")


@pytest.mark.parametrize("missing", ["name", "version", "image", "digests", "tools"])
def test_required_keys(tmp_path, missing):
    doc = spec()
    del doc[missing]
    d = write(tmp_path, doc)
    with pytest.raises(ValueError, match=f"{missing}: required"):
        load_runtime(d / "runtime.yaml")


def test_description_optional(tmp_path):
    d = write(tmp_path, spec(description=None))
    load_runtime(d / "runtime.yaml")


def test_not_a_mapping(tmp_path):
    d = write(tmp_path, ["a"])
    with pytest.raises(ValueError, match="mapping"):
        load_runtime(d / "runtime.yaml")


def test_missing_file(tmp_path):
    with pytest.raises(ValueError, match="missing"):
        load_runtime(tmp_path / "runtime.yaml")


def test_parse_tag():
    assert parse_tag("runtimes/kubernetes/v1.2.3") == ("kubernetes", "1.2.3")
    assert parse_tag("runtimes/kubernetes/v1.2.3-rc.1") == ("kubernetes", "1.2.3-rc.1")


@pytest.mark.parametrize(
    "tag",
    [
        "kubernetes/v1.0.0",
        "runtimes/kubernetes/1.0.0",
        "runtimes/kubernetes/v1.0",
        "runtimes/K/v1.0.0",
    ],
)
def test_parse_tag_rejects_malformed(tag):
    with pytest.raises(ValueError):
        parse_tag(tag)


def test_check_tag_ok(tmp_path):
    d = write(tmp_path, spec())
    check_tag("runtimes/kubernetes/v1.0.0", d)


def test_check_tag_version_mismatch(tmp_path):
    d = write(tmp_path, spec())
    with pytest.raises(ValueError, match="1.0.1"):
        check_tag("runtimes/kubernetes/v1.0.1", d)


def test_check_tag_name_mismatch(tmp_path):
    d = write(tmp_path, spec())
    with pytest.raises(ValueError, match="postgres"):
        check_tag("runtimes/postgres/v1.0.0", d)


def test_check_tag_refuses_placeholder(tmp_path):
    d = write(tmp_path, spec(digests={"index": PLACEHOLDER_DIGEST}))
    with pytest.raises(ValueError, match="placeholder"):
        check_tag("runtimes/kubernetes/v1.0.0", d)


def test_digests_from_index_skips_attestations():
    assert digests_from_index(RAW) == {
        "index": RAW_DIGEST,
        "linux/amd64": D_AMD,
        "linux/arm64": D_ARM,
    }


def test_digests_from_index_variant_and_unknown_platforms():
    raw = index_bytes(
        ("linux", "arm", "v7", D_ARM),
        ("linux", "s390x", None, D_AMD),
        ("windows", "amd64", None, D_AMD),
    )
    assert set(digests_from_index(raw)) == {"index", "linux/arm/v7"}


def test_digests_from_index_rejects_single_manifest():
    with pytest.raises(ValueError, match="single manifest"):
        digests_from_index(json.dumps({"schemaVersion": 2, "layers": []}).encode())


def test_digests_from_index_rejects_non_json():
    with pytest.raises(ValueError, match="JSON"):
        digests_from_index(b"<html>")


def test_compare_digests():
    compare_digests({"index": D_INDEX}, {"index": D_INDEX})
    with pytest.raises(ValueError, match="linux/arm64"):
        compare_digests({"index": D_INDEX}, {"index": D_INDEX, "linux/arm64": D_ARM})
    with pytest.raises(ValueError, match="index"):
        compare_digests({"index": D_INDEX}, {"index": D_ARM})


def test_check_compare_ok(tmp_path):
    pinned = {"index": RAW_DIGEST, "linux/amd64": D_AMD, "linux/arm64": D_ARM}
    d = write(tmp_path, spec(digests=pinned))
    seen = []

    def inspect(ref):
        seen.append(ref)
        return RAW

    check_compare(d, inspect)
    assert seen == [f"ghcr.io/runwhen-contrib/runwhen-plugins-runtime-kubernetes@{RAW_DIGEST}"]


def test_check_compare_missing_platform_digest(tmp_path):
    d = write(tmp_path, spec(digests={"index": RAW_DIGEST, "linux/amd64": D_AMD}))
    with pytest.raises(ValueError, match="linux/arm64"):
        check_compare(d, lambda ref: RAW)


def test_check_compare_wrong_platform_digest(tmp_path):
    d = write(
        tmp_path,
        spec(digests={"index": RAW_DIGEST, "linux/amd64": D_ARM, "linux/arm64": D_ARM}),
    )
    with pytest.raises(ValueError, match="linux/amd64"):
        check_compare(d, lambda ref: RAW)


def test_check_compare_registry_returns_other_bytes(tmp_path):
    # The index digest runtime.yaml pins is not the hash of what the registry served.
    d = write(tmp_path, spec(digests={"index": D_INDEX}))
    with pytest.raises(ValueError, match="index"):
        check_compare(d, lambda ref: RAW)


def test_check_compare_needs_index(tmp_path):
    d = write(tmp_path, spec(digests={"linux/amd64": D_AMD}))
    with pytest.raises(ValueError, match="index"):
        check_compare(d, lambda ref: RAW)


def test_format_digests_round_trips_through_yaml():
    out = format_digests({"linux/arm64": D_ARM, "index": D_INDEX})
    assert yaml.safe_load(out) == {"digests": {"index": D_INDEX, "linux/arm64": D_ARM}}


def test_main_validate_and_tag(tmp_path, capsys):
    d = write(tmp_path, spec())
    assert main(["x", "validate", "--runtime-dir", str(d)]) == 0
    assert main(["x", "tag", "runtimes/kubernetes/v1.0.0", "--repo-root", str(tmp_path)]) == 0
    assert main(["x", "tag", "runtimes/kubernetes/v2.0.0", "--repo-root", str(tmp_path)]) == 1
    assert "2.0.0" in capsys.readouterr().err
