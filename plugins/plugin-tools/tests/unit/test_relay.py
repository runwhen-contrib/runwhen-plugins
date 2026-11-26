"""The upload client against a fake relay (the SDK's real RelayClient on the wire)."""

import hashlib

import pytest
from plugin_tools.relay import (
    E_ARCHIVE_CAP,
    PluginRelay,
    RelayError,
    relay_from_ctx,
)
from relayfake import DISPATCH, REQUEST_ID, SENDS_DISPATCH, TOKEN
from runwhen_capability.rwerrors import RwError

DATA = b"archive bytes " * 100
SHA = "sha256:" + hashlib.sha256(DATA).hexdigest()


def _client(relay) -> PluginRelay:
    return PluginRelay(relay.client())


def test_put_stores_and_answers_the_completed_object(relay):
    out = _client(relay).put_archive(SHA, DATA)
    assert out == {"sha256": SHA, "size": len(DATA), "state": "stored"}
    assert relay.stored == {SHA: DATA}
    (req,) = relay.requests
    assert req["path"] == f"/v1/tasks/{REQUEST_ID}/plugin-archives/{SHA}" and req["size"] == len(
        DATA
    )


def test_put_when_papi_already_stores_it_answers_exists(relay):
    relay.stored[SHA] = DATA
    assert _client(relay).put_archive(SHA, DATA) == {"exists": True}


def test_put_carries_the_sdk_auth_headers(relay):
    seen = {}
    client = relay.client()
    real = client.session.put

    def put(url, **kw):
        seen.update(kw["headers"])
        return real(url, **kw)

    client.session.put = put
    PluginRelay(client).put_archive(SHA, DATA)
    assert seen["Authorization"] == f"Bearer {TOKEN}"
    if SENDS_DISPATCH:
        assert seen["X-RW-Dispatch"] == DISPATCH
    assert seen["Content-Type"] == "application/x-tar"


def test_hash_mismatch_from_the_relay_is_a_coded_error(relay):
    relay.mismatch.add(SHA)
    with pytest.raises(RelayError) as exc:
        _client(relay).put_archive(SHA, DATA)
    assert (exc.value.code, exc.value.status) == ("E_HASH_MISMATCH", 422)


def test_the_per_run_cap_is_its_own_code(relay):
    relay.cap = 0
    with pytest.raises(RelayError) as exc:
        _client(relay).put_archive(SHA, DATA)
    assert exc.value.code == E_ARCHIVE_CAP and exc.value.status == 409


def test_wrong_bytes_for_a_sha_never_leave_the_process(relay):
    with pytest.raises(ValueError, match="do not match"):
        _client(relay).put_archive("sha256:" + "0" * 64, DATA)
    assert relay.requests == []


@pytest.mark.parametrize("sha", ["sha256:abc", "abc", "sha256:" + "A" * 64, "../x"])
def test_a_malformed_sha_is_refused_before_any_request(relay, sha):
    with pytest.raises(ValueError):
        _client(relay).put_archive(sha, DATA)
    assert relay.requests == []


def test_empty_and_oversized_archives_are_refused(relay, monkeypatch):
    with pytest.raises(ValueError):
        _client(relay).put_archive("sha256:" + hashlib.sha256(b"").hexdigest(), b"")
    monkeypatch.setattr("plugin_tools.relay.MAX_ARCHIVE_BYTES", 10)
    with pytest.raises(ValueError):
        _client(relay).put_archive(SHA, DATA)


def test_unauthorized_is_an_http_coded_error(relay):
    client = relay.client()
    client._tokens._token = "bad"
    with pytest.raises(RelayError) as exc:
        PluginRelay(client).put_archive(SHA, DATA)
    assert exc.value.status == 401 and "bad" not in str(exc.value)


def test_unreachable_relay_is_a_transport_error():
    from relayfake import FakeRelay

    fake = FakeRelay()
    fake.server.server_close()  # the port is known, and nothing listens on it
    with pytest.raises(RelayError) as exc:
        PluginRelay(fake.client()).put_archive(SHA, DATA)
    assert exc.value.code == "E_RELAY_TRANSPORT"


def test_read_upload_returns_the_bytes(relay):
    relay.uploads["up_1"] = DATA
    assert _client(relay).read_upload("up_1", 1 << 20) == DATA
    assert relay.requests[0]["path"] == f"/v1/tasks/{REQUEST_ID}/plugin-uploads/up_1"


def test_read_upload_404_and_too_large_and_bad_hash(relay):
    with pytest.raises(RelayError) as exc:
        _client(relay).read_upload("nope", 1 << 20)
    assert exc.value.status == 404
    relay.uploads["up_1"] = DATA
    with pytest.raises(RelayError) as exc:
        _client(relay).read_upload("up_1", 10)
    assert exc.value.code == "E_UPLOAD_TOO_LARGE"
    relay.upload_headers["up_1"] = {"X-RW-Sha256": "sha256:" + "0" * 64}
    # the fake sends two X-RW-Sha256 headers; the first (the real one) wins in requests, so
    # force a mismatch through a relay that lies instead
    relay.uploads["up_2"] = DATA
    relay.upload_headers["up_2"] = {}
    client = relay.client()
    real_get = client.session.get

    def lying_get(url, **kw):
        resp = real_get(url, **kw)
        resp.headers["X-RW-Sha256"] = "sha256:" + "0" * 64
        return resp

    client.session.get = lying_get
    with pytest.raises(RelayError) as exc:
        PluginRelay(client).read_upload("up_2", 1 << 20)
    assert exc.value.code == "E_HASH_MISMATCH"


@pytest.mark.parametrize("upload_id", ["", "../x", "a/b", "a b", "x" * 200])
def test_read_upload_refuses_ids_a_route_cannot_carry(relay, upload_id):
    with pytest.raises(ValueError):
        _client(relay).read_upload(upload_id, 100)
    assert relay.requests == []


def test_relay_from_ctx_without_a_relay_is_a_dependency_error():
    class Ctx:
        relay = None

    with pytest.raises(RwError) as exc:
        relay_from_ctx(Ctx())
    assert exc.value.code == "dependency"


def test_relay_from_ctx_wraps_the_sdk_client(relay):
    class Ctx:
        pass

    ctx = Ctx()
    ctx.relay = relay.client()
    assert isinstance(relay_from_ctx(ctx), PluginRelay)
