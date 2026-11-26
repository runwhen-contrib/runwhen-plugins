"""integration/auth/https-token.py: the `token` auth method of the git integration."""

import importlib.util

import pytest
from conftest import PLUGIN

SPEC = importlib.util.spec_from_file_location(
    "git_auth_token", PLUGIN / "integration" / "auth" / "https-token.py"
)
auth = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(auth)

TOKEN = "ghp_secretsecretsecret"


def _env(tmp_path, files, **inputs):
    ref = tmp_path / "ref"
    ref.mkdir()
    for name, text in files.items():
        (ref / name).write_text(text)
    env = {
        "RW_AUTH_REF_DIR": str(ref),
        "RW_AUTH_OUT_ENV": str(tmp_path / "out.env"),
        "RW_AUTH_OUT_ERROR": str(tmp_path / "err.json"),
    }
    env.update({f"RW_AUTH_INPUT_{k.upper()}": v for k, v in inputs.items()})
    return env


@pytest.mark.parametrize("key", ["token", "GIT_TOKEN", "value"])
def test_token_from_any_accepted_key(tmp_path, key):
    env = _env(tmp_path, {key: TOKEN + "\n"})
    assert auth.main(env) == 0
    assert (tmp_path / "out.env").read_text() == f"GIT_USERNAME=x-access-token\nGIT_TOKEN={TOKEN}\n"


def test_username_input(tmp_path):
    env = _env(tmp_path, {"token": TOKEN}, username="deploy-bot")
    assert auth.main(env) == 0
    assert "GIT_USERNAME=deploy-bot\n" in (tmp_path / "out.env").read_text()


@pytest.mark.parametrize(
    "files",
    [{}, {"token": ""}, {"token": TOKEN, "value": TOKEN}, {"token": "a\nb"}],
)
def test_bad_reference_is_invalid_input_and_never_echoes_the_token(tmp_path, files, capsys):
    env = _env(tmp_path, files)
    assert auth.main(env) == 1
    err = (tmp_path / "err.json").read_text()
    assert '"code": "invalid_input"' in err and TOKEN not in err
    assert TOKEN not in capsys.readouterr().err
    assert not (tmp_path / "out.env").exists()


def test_multiline_username_is_refused(tmp_path):
    env = _env(tmp_path, {"token": TOKEN}, username="a\nGIT_TOKEN=evil")
    assert auth.main(env) == 1
