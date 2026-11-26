"""The hardening helper (S-D4): the exact -c flags and environment of every git call."""

import subprocess

import pytest
from conftest import make_source
from fixtures import make_plugin_tree
from plugin_tools import gitrun
from plugin_tools.credentials import (
    DEFAULT_USERNAME,
    CredentialError,
    GitCredential,
    askpass_command,
    auth_env,
    credential_from_env,
)
from plugin_tools.gitrun import GitError, GitRunner, GitTimeout, RunDir, strip_userinfo

HARDENING = [
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "protocol.allow=never",
    "-c",
    "protocol.https.allow=always",
]
TOKEN = "ghs_0123456789abcdefghij"


def test_flags_are_exactly_the_spec(run_dir):
    runner = GitRunner(run_dir)
    assert runner.flags() == HARDENING
    assert runner.command("status") == ["git", *HARDENING, "status"]
    # the remote calls add only the redirect rule; the three base flags stay first and unchanged
    assert runner.flags(remote=True) == [*HARDENING, "-c", "http.followRedirects=false"]
    assert gitrun.HARDENING_FLAGS == tuple(HARDENING)


def test_env_is_exactly_the_hardened_set_plus_an_allow_list(run_dir):
    runner = GitRunner(
        run_dir,
        environ={
            "PATH": "/usr/bin",
            "LANG": "C.UTF-8",
            "HTTPS_PROXY": "http://proxy:3128",
            "GIT_TOKEN": TOKEN,
            "GIT_USERNAME": "bot",
            "GIT_CONFIG_COUNT": "1",
            "GIT_ASKPASS": "/evil",
            "HOME": "/home/runwhen",
            "AWS_SECRET_ACCESS_KEY": "nope",
        },
    )
    assert runner.env() == {
        "PATH": "/usr/bin",
        "LANG": "C.UTF-8",
        "HTTPS_PROXY": "http://proxy:3128",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "HOME": str(run_dir.home),
        "TMPDIR": str(run_dir.tmp),
        "GIT_DIR": str(runner.gitdir),
    }
    # a local call never carries the credential, even when the task's environment holds one
    assert "GIT_TOKEN" not in runner.env()


def test_remote_env_adds_the_askpass_credential_only(run_dir):
    runner = GitRunner(run_dir, credential=GitCredential("bot", TOKEN), environ={"PATH": "/bin"})
    local, remote = runner.env(), runner.env(remote=True)
    assert set(remote) - set(local) == {
        "GIT_ASKPASS",
        "PLUGIN_TOOLS_GIT_USERNAME",
        "PLUGIN_TOOLS_GIT_PASSWORD",
    }
    assert remote["PLUGIN_TOOLS_GIT_PASSWORD"] == TOKEN
    assert TOKEN not in " ".join(local.values())
    assert TOKEN not in remote["GIT_ASKPASS"]  # the program line never holds the secret


def test_test_protocols_override_is_refused_outside_tests(run_dir, monkeypatch):
    monkeypatch.delenv(gitrun.TEST_PROTOCOLS_ENV)
    with pytest.raises(RuntimeError, match="tests only"):
        GitRunner(run_dir, test_allow_protocols=("file",))
    GitRunner(run_dir)  # the default needs no override


def test_test_protocols_override_adds_only_the_named_protocols(run_dir):
    runner = GitRunner(run_dir, test_allow_protocols=("file",))
    assert runner.flags() == [*HARDENING, "-c", "protocol.file.allow=always"]


def test_every_git_call_of_a_run_is_hardened(repo, run_dir, monkeypatch):
    """Drive the whole read side and look at every process it starts."""
    repo.publish("plugins/a", make_plugin_tree("a", "1.0.0"), "a/v1.0.0")
    calls = []
    real_run, real_popen = subprocess.run, subprocess.Popen

    def spy_run(cmd, **kw):
        calls.append((cmd, kw["env"]))
        return real_run(cmd, **kw)

    def spy_popen(cmd, **kw):
        calls.append((cmd, kw["env"]))
        return real_popen(cmd, **kw)

    monkeypatch.setattr(gitrun.subprocess, "run", spy_run)
    monkeypatch.setattr(gitrun.subprocess, "Popen", spy_popen)
    source = make_source(repo.url, run_dir)
    source.runner.credential = GitCredential("bot", TOKEN)
    tags = source.list_tags()
    (tag,) = source.matching_tags(tags)
    source.fetch(tag.tag, tag.commit)
    assert source.read_tree(tag.commit, "plugins/a")
    kinds = {next(a for a in c[1:] if not a.startswith("-") and "=" not in a) for c, _ in calls}
    assert {"init", "ls-remote", "fetch", "ls-tree", "cat-file"} <= kinds
    for cmd, env in calls:
        assert cmd[0] == "git"
        assert cmd[1:7] == HARDENING
        assert TOKEN not in " ".join(cmd)
        assert env["GIT_CONFIG_GLOBAL"] == "/dev/null"
        assert env["GIT_CONFIG_NOSYSTEM"] == "1"
        assert env["GIT_TERMINAL_PROMPT"] == "0"
        assert env["HOME"] == str(run_dir.home)
        remote = any(a in ("ls-remote", "fetch") for a in cmd)
        assert ("GIT_ASKPASS" in env) == remote  # the credential reaches only remote calls
        assert (TOKEN in env.get("PLUGIN_TOOLS_GIT_PASSWORD", "")) == remote


def test_a_hooks_path_in_the_repo_config_does_not_run(repo, run_dir, tmp_path):
    repo.publish("plugins/a", make_plugin_tree("a", "1.0.0"), "a/v1.0.0")
    hooks, marker = tmp_path / "hooks", tmp_path / "ran"
    hooks.mkdir()
    hook = hooks / "reference-transaction"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
    hook.chmod(0o755)
    source = make_source(repo.url, run_dir)
    source.runner.ensure_repo()
    with open(source.gitdir / "config", "a") as cfg:
        cfg.write(f"[core]\n\thooksPath = {hooks}\n")
    (tag,) = source.matching_tags(source.list_tags())
    source.fetch(tag.tag, tag.commit)
    assert not marker.exists()
    # control: the same hook does run when the hardening flag is absent
    subprocess.run(
        ["git", "update-ref", "refs/x", tag.commit],
        env={
            "GIT_DIR": str(source.gitdir),
            "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
        },
        check=True,
    )
    assert marker.exists()


def test_only_https_is_allowed(run_dir, repo):
    runner = GitRunner(run_dir)  # no test override: the production protocol set
    for url in (
        repo.url,
        "http://127.0.0.1:9/x.git",
        "ssh://git@127.0.0.1/x.git",
        "git://127.0.0.1/x",
    ):
        with pytest.raises(GitError, match="not allowed"):
            runner.git("ls-remote", "--tags", "--", url, remote=True)


def test_timeout_is_a_git_timeout(run_dir, monkeypatch):
    def boom(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 1)

    monkeypatch.setattr(gitrun.subprocess, "run", boom)
    runner = GitRunner(run_dir, timeout=1)
    runner.gitdir.mkdir()
    with pytest.raises(GitTimeout, match="timed out after 1s"):
        runner.git("status")


def test_run_dir_is_private_and_removed(tmp_path):
    with RunDir(tmp_path) as rd:
        assert rd.home.parent == rd.work.parent == rd.tmp.parent == rd.root
        assert rd.root.parent == tmp_path
        (rd.work / "x").write_text("y")
        root = rd.root
    assert not root.exists()


def test_run_dir_is_removed_when_the_run_fails(tmp_path):
    with pytest.raises(ZeroDivisionError), RunDir(tmp_path) as rd:
        root = rd.root
        1 / 0  # noqa: B018
    assert not root.exists()


# -- scrubbing -------------------------------------------------------------------------------


def test_strip_userinfo():
    assert strip_userinfo("https://u:p@h/x") == "https://h/x"
    assert strip_userinfo("https://h/x") == "https://h/x"
    assert strip_userinfo("git@h:x") == "git@h:x"


def test_scrub_replaces_exact_secrets_not_short_substrings(run_dir):
    runner = GitRunner(run_dir, credential=GitCredential("x-access-token", TOKEN))
    assert (
        runner.scrub(f"fatal: bad {TOKEN} at x-access-token") == "fatal: bad *** at x-access-token"
    )
    short = GitRunner(run_dir, credential=GitCredential("u", "abc"))
    assert short.scrub("abc and abcd and xabc") == "*** and abcd and xabc"


def test_scrub_removes_userinfo_of_the_url(run_dir):
    runner = GitRunner(run_dir, url="https://user:hunter2hunter2@h.example/r.git")
    text = runner.scrub("fatal: unable to access 'https://user:hunter2hunter2@h.example/r.git/'")
    assert "hunter2" not in text and "h.example/r.git" in text


def test_a_failing_git_call_never_shows_the_token(run_dir, repo):
    runner = GitRunner(
        run_dir, credential=GitCredential("x-access-token", TOKEN), test_allow_protocols=("file",)
    )
    with pytest.raises(GitError) as exc:
        runner.git("fetch", "--", f"{repo.url}-missing/{TOKEN}", "refs/heads/x", remote=True)
    assert TOKEN not in str(exc.value) and "***" in str(exc.value)


# -- the credential and askpass ----------------------------------------------------------------


def test_credential_from_env_defaults_the_username():
    cred = credential_from_env({"GIT_TOKEN": TOKEN})
    assert (cred.username, cred.password) == (DEFAULT_USERNAME, TOKEN)
    cred = credential_from_env({"GIT_TOKEN": TOKEN, "GIT_USERNAME": "bot"})
    assert cred.username == "bot"
    assert credential_from_env({}) is None and credential_from_env({"GIT_TOKEN": ""}) is None


def test_credential_repr_hides_the_secret():
    assert TOKEN not in repr(GitCredential("u", TOKEN))


@pytest.mark.parametrize("env", [{"GIT_TOKEN": "a\nb"}, {"GIT_TOKEN": "a", "GIT_USERNAME": "b\0"}])
def test_a_multi_line_credential_is_refused_without_echoing_it(env):
    with pytest.raises(CredentialError) as exc:
        credential_from_env(env)
    assert "a\nb" not in str(exc.value)


def test_askpass_runs_the_way_git_runs_it():
    """git runs GIT_ASKPASS through a shell with the prompt as its argument."""
    env = auth_env(GitCredential("bot", TOKEN))
    base = {"PATH": "/usr/bin:/bin"}
    for prompt, want in (
        ("Username for 'https://h': ", "bot"),
        ("Password for 'https://bot@h': ", TOKEN),
    ):
        out = subprocess.run(
            f'{env["GIT_ASKPASS"]} "{prompt}"',
            shell=True,
            env={**base, **env},
            capture_output=True,
            text=True,
            check=True,
        )
        assert out.stdout == want + "\n"


def test_askpass_without_credential_environment_fails_quietly():
    out = subprocess.run(
        f'{askpass_command()} "Password for x: "',
        shell=True,
        env={"PATH": "/bin"},
        capture_output=True,
        text=True,
    )
    assert out.returncode == 1 and out.stdout == ""


def test_auth_env_is_empty_without_a_credential():
    assert auth_env(None) == {}
